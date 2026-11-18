"""P4 quality incident investigation executed in DCR (foundation contract v1).

See docs/P4-FOUNDATION-HANDOVER.md and docs/P4-QUALITY-INVESTIGATION.md.

- Intake: the foundation outbox posts `{"event": ...}` to P4_INCIDENT_URL with
  `Idempotency-Key: <event.deliveryId>`. P4 accepts `DeliveryRiskDetected` (legacy, or
  with the v1 `assessment` extension) and `AssessmentRequested` (temperature excursion,
  combined transport risk, telemetry unavailable, evaluation unavailable). The full
  request is persisted before P4 answers `{"deliveryId", "status": accepted|duplicate}`.
  The first message of an incident opens a DCR case (OpenIncident); later messages are
  AddAlertEvidence, which makes a new assessment due. A closed incident only records it.
- DCR decides which investigation activities are enabled. If DCR is unreachable the
  request stays persisted and case opening is retried in the background.
- AssessEvidence records the evidence version the officer actually assessed. A decision
  is applied through Registry.accept_quality_status (the same operation as
  POST /integrations/p4/quality-status) with that version as expectedEvidenceVersion
  BEFORE the DCR decision is executed. A conflict (newer evidence, wrong incident,
  terminal rejection) is recorded as unapplied and requires reassessment; a transient
  failure keeps a durable pending decision that is retried with the same identity.
- The registry stays the authority for quality permission and admission.
"""

import json
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from dcr_client import DCRUnavailable
from registry import Conflict, Registry, now

# DCR event id -> role. Event ids are also the logged activity names.
ROLES = {
    "OpenIncident": "Integration",
    "AddAlertEvidence": "Integration",
    "ReviewTemperatureLog": "QualityOfficer",
    "ReviewWeatherData": "QualityOfficer",
    "ReviewRouteAndETA": "QualityOfficer",
    "RequestCoolingAdjustment": "QualityOfficer",
    "RecordCoolingAcknowledgement": "Integration",
    "RecordCoolingFailure": "Integration",
    "RecordRecoveryEvidence": "Integration",
    "RequestAdditionalCheck": "QualityOfficer",
    "CompleteAdditionalCheck": "Receiver",
    "AssessEvidence": "QualityOfficer",
    "DecideRelease": "QualityOfficer",
    "DecideReplace": "QualityOfficer",
    "DecideReturn": "QualityOfficer",
    "CloseInvestigation": "QualityOfficer",
}
INTEGRATION_ONLY = {"OpenIncident", "AddAlertEvidence"}
DECISIONS = {
    "DecideRelease": ("RELEASE", "RELEASED"),
    "DecideReplace": ("REPLACE", "REJECTED"),
    "DecideReturn": ("RETURN", "REJECTED"),
}
EVENT_TYPES = {"DeliveryRiskDetected", "AssessmentRequested"}
ASSESSMENT_REASONS = {
    "DeliveryRiskDetected": {"delivery-risk"},
    "AssessmentRequested": {"temperature-excursion", "combined-transport-risk",
                            "container-telemetry-unavailable",
                            "monitoring-evaluation-unavailable"},
}


class UnknownIncident(KeyError):
    pass


class Blocked(Conflict):
    """DCR does not currently enable the requested activity."""

    def __init__(self, activity, enabled):
        super().__init__(f"DCR does not enable {activity} in this investigation now")
        self.enabled = enabled


class StaleDecision(Conflict):
    """The registry refused the disposition; the officer must reassess."""

    def __init__(self, message, current_version):
        super().__init__(message)
        self.current_version = current_version


class DecisionPending(RuntimeError):
    """The disposition is durably recorded and will be retried."""


def ensure_schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, "
        "shipment_id TEXT NOT NULL, data TEXT NOT NULL)"
    )


def read_incident(db, incident_id):
    ensure_schema(db)
    row = db.execute("SELECT data FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        raise UnknownIncident(incident_id)
    return json.loads(row[0])


def find_incident(db, incident_id):
    try:
        return read_incident(db, incident_id)
    except UnknownIncident:
        return None


def save_incident(db, incident):
    db.execute(
        "INSERT OR REPLACE INTO incidents VALUES (?,?,?)",
        (incident["incidentId"], incident["shipmentId"], json.dumps(incident)),
    )


def journal(db, record, incident, activity, actor="system", payload=None,
            event_id=None, occurred_at=None, input_mode=None, source=None):
    """Record one P4 activity in the shared journal (idempotent by event id)."""
    if event_id is None:
        incident["activityCount"] += 1
        event_id = f"P4:{incident['incidentId']}:{incident['activityCount']:03d}:{activity}"
    event = Registry.event(
        db, record, activity, actor=actor, process="P4", event_id=event_id,
        payload={"incidentId": incident["incidentId"], **(payload or {})},
        occurred_at=occurred_at, source=source or "quality-investigation",
        input_mode=input_mode,
    )
    # P4 keeps its own role names and links each activity to its DCR case.
    patch = dict(incidentId=incident["incidentId"],
                 role=ROLES.get(activity, event["role"]),
                 processInstanceKey=incident.get("dcrCaseId"))
    if any(event.get(k) != v for k, v in patch.items()):
        event.update(patch)
        db.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(event), event_id))
    return event


def text(data, field, required=True, limit=500):
    value = data.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
    return value.strip()


def utc(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 UTC timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 UTC timestamp") from error
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be a UTC timestamp")
    return value


def parse_handoff(data, idempotency_key):
    """Validate the wire envelope and return a summary; the full body is kept as is."""
    event = data.get("event") if isinstance(data, dict) else None
    if not isinstance(event, dict):
        raise ValueError("send the foundation envelope {\"event\": {...}}")
    delivery_id = text(event, "deliveryId", limit=1000)
    if idempotency_key != delivery_id:
        raise ValueError("Idempotency-Key must match event.deliveryId")
    event_type = event.get("eventType")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"eventType must be one of {sorted(EVENT_TYPES)}")
    if not isinstance(event.get("incidentOpened"), bool):
        raise ValueError("incidentOpened must be a boolean")
    summary = dict(
        deliveryId=delivery_id, eventType=event_type,
        shipmentId=text(event, "shipmentId", limit=100),
        journeyId=text(event, "journeyId", limit=100),
        incidentId=text(event, "incidentId", limit=1000),
        incidentOpened=event["incidentOpened"],
        qualityStatus=text(event, "qualityStatus", limit=20),
        reason="delivery-risk", evidenceVersion=None, priority="normal", occurredAt=None,
    )
    assessment = event.get("assessment")
    if event_type == "AssessmentRequested" and not isinstance(assessment, dict):
        raise ValueError("AssessmentRequested needs the assessment extension")
    if assessment is not None:
        if not isinstance(assessment, dict) or assessment.get("schemaVersion") != 1:
            raise ValueError("assessment.schemaVersion must be 1")
        if assessment.get("reason") not in ASSESSMENT_REASONS[event_type]:
            raise ValueError(f"assessment.reason not valid for {event_type}")
        version = assessment.get("evidenceVersion")
        if type(version) is not int or version < 1:
            raise ValueError("assessment.evidenceVersion must be a positive integer")
        if assessment.get("priority") not in ("normal", "urgent"):
            raise ValueError("assessment.priority must be normal or urgent")
        summary.update(reason=assessment["reason"], evidenceVersion=version,
                       priority=assessment["priority"],
                       occurredAt=utc(assessment.get("occurredAt"), "assessment.occurredAt"))
    if event_type == "DeliveryRiskDetected":
        risk = event.get("riskEvent")
        if not isinstance(risk, dict):
            raise ValueError("DeliveryRiskDetected needs riskEvent")
        text(risk, "eventId", limit=256)
        summary["occurredAt"] = summary["occurredAt"] or utc(risk.get("occurredAt"),
                                                             "riskEvent.occurredAt")
    return summary


def new_incident(incident_id, shipment_id, dcr):
    return dict(
        incidentId=incident_id,
        shipmentId=shipment_id,
        status="OPENING",
        openedAt=now(),
        evidence=[],
        evidenceVersion=None,
        priority="normal",
        assessedEvidenceVersion=None,
        pendingSystemEvents=["OpenIncident"],
        dcrMode=dcr.mode,
        dcrGraphId=getattr(dcr, "graph_id", None),
        dcrCaseId=None,
        pendingDecision=None,
        decision=None,
        unappliedDecisions=[],
        reassessmentRequired=False,
        closedAt=None,
        lastError=None,
        activityCount=0,
    )


class QualityService:
    def __init__(self, get_registry, get_dcr):
        self.get_registry = get_registry
        self.get_dcr = get_dcr
        self.lock = threading.RLock()  # one DCR interaction at a time per service

    @property
    def registry(self):
        return self.get_registry()

    @property
    def dcr(self):
        return self.get_dcr()

    # -- intake -------------------------------------------------------------
    def accept_handoff(self, data, idempotency_key):
        """Durably accept one foundation handoff (at-least-once delivery)."""
        summary = parse_handoff(data, idempotency_key)
        delivery_id, incident_id = summary["deliveryId"], summary["incidentId"]
        key = f"p4-handoff:{delivery_id}"
        dcr = self.dcr  # resolved outside the transaction: LocalDCR shares the SQLite file
        with self.registry.transaction() as db:
            ensure_schema(db)
            row = db.execute("SELECT data FROM results WHERE id=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if saved["request"] != data:
                    raise Conflict("deliveryId reused with different content")
                return dict(deliveryId=delivery_id, status="duplicate",
                            incidentId=saved["incidentId"], effect=saved["effect"])
            produced = db.execute("SELECT data FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if not produced or json.loads(produced[0])["variables"] != data:
                raise Conflict("handoff does not match a foundation outbox message")
            record = Registry.read(db, summary["shipmentId"])
            if record["journeyId"] != summary["journeyId"]:
                raise Conflict("handoff journey does not match the shipment")
            evidence = dict(summary, receivedAt=now(), request=data["event"])
            incident = find_incident(db, incident_id)
            if incident is None and summary["qualityStatus"] != "PENDING":
                # Terminal/decided incident P4 never investigated (e.g. a test stand-in).
                journal(db, record, dict(incidentId=incident_id, activityCount=0),
                        "LateAlertEvidenceRecorded", payload=self._evidence_payload(summary),
                        event_id=f"P4:{incident_id}:late:{delivery_id}",
                        occurred_at=summary["occurredAt"])
                effect = "evidence-recorded"
            else:
                if incident is None:
                    incident, effect = new_incident(incident_id, record["shipmentId"], dcr), \
                        "incident-opened"
                elif incident["status"] == "CLOSED":
                    effect = "evidence-recorded"
                else:
                    incident["pendingSystemEvents"].append("AddAlertEvidence")
                    effect = "evidence-added"
                incident["evidence"].append(evidence)
                if summary["evidenceVersion"] is not None:
                    incident["evidenceVersion"] = max(incident["evidenceVersion"] or 0,
                                                      summary["evidenceVersion"])
                if summary["priority"] == "urgent":
                    incident["priority"] = "urgent"
                journal(db, record, incident,
                        "LateAlertEvidenceRecorded" if effect == "evidence-recorded"
                        else "QualityAlertReceived",
                        payload=self._evidence_payload(summary),
                        event_id=f"P4:{incident_id}:received:{delivery_id}",
                        occurred_at=summary["occurredAt"],
                        input_mode=(data["event"].get("assessment") or {}).get("inputMode",
                                                                              "system"),
                        source="p4-intake")
                save_incident(db, incident)
            db.execute("INSERT INTO results VALUES (?,?)", (key, json.dumps(dict(
                request=data, incidentId=incident_id, effect=effect))))
        if effect != "evidence-recorded" or incident is not None:
            self.sync(incident_id)
        return dict(deliveryId=delivery_id, status="accepted", incidentId=incident_id,
                    effect=effect)

    @staticmethod
    def _evidence_payload(summary):
        return {k: summary[k] for k in ("deliveryId", "eventType", "reason", "evidenceVersion",
                                         "priority", "qualityStatus", "incidentOpened")}

    # -- DCR synchronisation ------------------------------------------------
    def sync(self, incident_id):
        """Create the DCR case if missing, execute queued integration events and
        finish a durably pending decision."""
        with self.lock:
            with self.registry.transaction() as db:
                incident = read_incident(db, incident_id)
            try:
                case = incident.get("dcrCaseId")
                if not case and incident["status"] != "CLOSED":
                    case = self.dcr.create_simulation(title=incident_id)
                    with self.registry.transaction() as db:
                        incident = read_incident(db, incident_id)
                        incident["dcrCaseId"] = case
                        save_incident(db, incident)
                while incident["pendingSystemEvents"]:
                    activity = incident["pendingSystemEvents"][0]
                    enabled = {e["id"] for e in self.dcr.events(case)["events"] if e["enabled"]}
                    if activity in enabled:
                        self.dcr.execute(case, activity)
                    with self.registry.transaction() as db:
                        incident = read_incident(db, incident_id)
                        incident["pendingSystemEvents"].pop(0)
                        record = Registry.read(db, incident["shipmentId"])
                        journal(db, record, incident,
                                activity if activity in enabled else "LateAlertEvidenceRecorded")
                        save_incident(db, incident)
                if incident["status"] in ("OPENING", "DCR_UNAVAILABLE"):
                    with self.registry.transaction() as db:
                        incident = read_incident(db, incident_id)
                        incident.update(status="OPEN", lastError=None)
                        save_incident(db, incident)
            except DCRUnavailable as error:
                with self.registry.transaction() as db:
                    incident = read_incident(db, incident_id)
                    if incident["status"] != "CLOSED":
                        incident["status"] = "DCR_UNAVAILABLE"
                    incident["lastError"] = str(error)
                    save_incident(db, incident)
                return incident
            if incident.get("pendingDecision"):
                try:
                    self._finish_decision(incident_id)
                except (StaleDecision, DecisionPending, Blocked, DCRUnavailable):
                    pass  # recorded on the incident; the officer or the retry loop continues
            with self.registry.transaction() as db:
                return read_incident(db, incident_id)

    def retry_unfinished(self):
        """Background retry for cases waiting on DCR or on a pending decision."""
        with self.registry.transaction() as db:
            ensure_schema(db)
            waiting = [
                incident["incidentId"]
                for incident in (json.loads(r[0]) for r in db.execute("SELECT data FROM incidents"))
                if incident["status"] in ("OPENING", "DCR_UNAVAILABLE")
                or incident.get("pendingDecision")
            ]
        for incident_id in waiting:
            self.sync(incident_id)

    # -- decisions ----------------------------------------------------------
    def _finish_decision(self, incident_id):
        """Apply the pending disposition in the registry, then execute it in DCR."""
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
        pending = incident["pendingDecision"]
        if not pending.get("registryApplied"):
            try:
                self.registry.accept_quality_status(
                    incident["shipmentId"], pending["eventId"], pending["occurredAt"],
                    pending["qualityStatus"], incident_id, pending["expectedEvidenceVersion"])
            except Conflict as error:
                current = self.registry.get(incident["shipmentId"]).get("evidenceVersion")
                with self.registry.transaction() as db:
                    incident = read_incident(db, incident_id)
                    record = Registry.read(db, incident["shipmentId"])
                    refused = dict(pending, reason=str(error), currentEvidenceVersion=current)
                    incident["unappliedDecisions"].append(refused)
                    incident.update(pendingDecision=None, lastError=str(error),
                                    reassessmentRequired=True)
                    journal(db, record, incident, "StaleDispositionRefused",
                            actor=pending["officer"], payload=refused,
                            event_id=f"P4:{incident_id}:refused:{pending['eventId']}",
                            occurred_at=pending["occurredAt"])
                    save_incident(db, incident)
                raise StaleDecision(
                    f"disposition not applied: {error}. Reassess the current evidence "
                    f"(registry evidence version {current}).", current) from error
            except Exception as error:  # e.g. a locked database: same identity is retried
                with self.registry.transaction() as db:
                    incident = read_incident(db, incident_id)
                    incident["lastError"] = f"disposition pending: {error}"
                    save_incident(db, incident)
                raise DecisionPending(str(error)) from error
            with self.registry.transaction() as db:
                incident = read_incident(db, incident_id)
                incident["pendingDecision"]["registryApplied"] = True
                save_incident(db, incident)
                pending = incident["pendingDecision"]
        case = incident["dcrCaseId"]
        enabled = [e["id"] for e in self.dcr.events(case)["events"] if e["enabled"]]
        if pending["activity"] not in enabled:
            raise Blocked(pending["activity"], enabled)
        self.dcr.execute(case, pending["activity"])
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
            record = Registry.read(db, incident["shipmentId"])
            payload = {k: pending[k] for k in ("reason", "qualityStatus",
                                               "expectedEvidenceVersion", "eventId")}
            event = journal(db, record, incident, pending["activity"], pending["officer"],
                            payload, event_id=pending.get("journalId"),
                            occurred_at=pending["occurredAt"], input_mode="manual")
            journal(db, record, incident, "DispositionRecorded", actor=pending["officer"],
                    payload=dict(payload, decision=pending["decision"],
                                 evidence=[e["deliveryId"] for e in incident["evidence"]]),
                    event_id=f"P4:{incident_id}:DispositionRecorded:{pending['eventId']}",
                    occurred_at=pending["occurredAt"])
            incident.update(pendingDecision=None, lastError=None, decision=dict(
                decision=pending["decision"], officer=pending["officer"],
                reason=pending["reason"], decidedAt=pending["occurredAt"],
                qualityStatus=pending["qualityStatus"],
                assessedEvidenceVersion=pending["expectedEvidenceVersion"],
                qualityStatusEventId=pending["eventId"]))
            save_incident(db, incident)
        return incident, event

    # -- investigation activities -------------------------------------------
    def execute(self, incident_id, activity, data):
        if activity not in ROLES:
            raise ValueError(f"unknown investigation activity: {activity}")
        if activity in INTEGRATION_ONLY:
            raise ValueError(f"{activity} is performed by the integration service")
        actor = text(data, "actor", limit=100)
        reason = text(data, "reason", required=activity in DECISIONS)
        note = text(data, "note", required=False)
        external_id = text(data, "eventId", required=False, limit=100)
        journal_id = f"P4:{incident_id}:ext:{external_id}" if external_id else None
        with self.lock:
            with self.registry.transaction() as db:
                incident = read_incident(db, incident_id)
                if journal_id:
                    row = db.execute("SELECT data FROM events WHERE id=?",
                                     (journal_id,)).fetchone()
                    if row:
                        saved = json.loads(row[0])
                        if saved["eventType"] != activity:
                            raise Conflict("eventId reused for a different activity")
                        return incident, saved
            if incident["status"] == "CLOSED":
                raise Conflict("investigation is closed")
            if incident["status"] != "OPEN":
                raise Conflict("DCR case is not open yet; retry POST .../sync first")
            if incident.get("pendingDecision"):
                raise Conflict("a decision is still being applied; retry POST .../sync")
            case = incident["dcrCaseId"]
            enabled = [e["id"] for e in self.dcr.events(case)["events"] if e["enabled"]]
            if activity not in enabled:
                raise Blocked(activity, enabled)
            if activity in DECISIONS:
                return self._decide(incident, activity, actor, reason, journal_id)
            self.dcr.execute(case, activity)
            with self.registry.transaction() as db:
                incident = read_incident(db, incident_id)
                record = Registry.read(db, incident["shipmentId"])
                payload = {k: v for k, v in (("reason", reason), ("note", note)) if v}
                if activity == "AssessEvidence":
                    # The version the officer actually assessed (not read at decision time).
                    incident["assessedEvidenceVersion"] = incident["evidenceVersion"]
                    incident["reassessmentRequired"] = False
                    payload["assessedEvidenceVersion"] = incident["evidenceVersion"]
                event = journal(db, record, incident, activity, actor, payload,
                                event_id=journal_id, input_mode="manual")
                if activity == "CloseInvestigation":
                    incident.update(status="CLOSED", closedAt=event["occurredAt"])
                save_incident(db, incident)
        return incident, event

    def _decide(self, incident, activity, actor, reason, journal_id):
        incident_id = incident["incidentId"]
        if incident.get("reassessmentRequired"):
            raise StaleDecision("an earlier disposition was refused; execute AssessEvidence "
                                "on the current evidence first", incident.get("evidenceVersion"))
        decision, status = DECISIONS[activity]
        attempt = len(incident["unappliedDecisions"]) + 1
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
            incident["pendingDecision"] = dict(
                activity=activity, decision=decision, qualityStatus=status, officer=actor,
                reason=reason, occurredAt=now(), journalId=journal_id,
                eventId=f"P4:{incident_id}:{decision}:{attempt}",
                expectedEvidenceVersion=incident["assessedEvidenceVersion"],
                registryApplied=False)
            save_incident(db, incident)
        return self._finish_decision(incident_id)

    # -- views --------------------------------------------------------------
    def view(self, incident_id):
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
            record = Registry.read(db, incident["shipmentId"])
        dcr = None
        if incident.get("dcrCaseId") and incident["status"] != "CLOSED":
            try:
                dcr = self.dcr.events(incident["dcrCaseId"])
            except DCRUnavailable as error:
                dcr = {"unavailable": str(error)}
        current = record.get("evidenceVersion") if record.get(
            "qualityIncidentId") == incident_id else None
        return dict(incident, dcr=dcr, registry=dict(
            qualityStatus=record.get("qualityStatus", "CLEAR"),
            qualityIncidentId=record.get("qualityIncidentId"),
            evidenceVersion=current,
            reassessmentNeeded=current is not None
            and incident.get("assessedEvidenceVersion") is not None
            and current != incident["assessedEvidenceVersion"]
            and not incident.get("decision")))

    def for_shipment(self, shipment_id):
        with self.registry.transaction() as db:
            record = Registry.read(db, shipment_id)
            ensure_schema(db)
            incidents = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM incidents WHERE shipment_id=? ORDER BY rowid",
                    (shipment_id,),
                )
            ]
        return dict(
            shipmentId=shipment_id,
            qualityStatus=record.get("qualityStatus", "CLEAR"),
            qualityIncidentId=record.get("qualityIncidentId"),
            evidenceVersion=record.get("evidenceVersion"),
            incidents=incidents,
        )


def create_blueprint(service):
    bp = Blueprint("quality", __name__)

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("send a JSON object")
        return data

    @bp.errorhandler(UnknownIncident)
    def unknown(error):
        return jsonify(error="unknown incident"), 404

    @bp.errorhandler(Blocked)
    def blocked(error):
        return jsonify(error=str(error), enabled=error.enabled), 409

    @bp.errorhandler(StaleDecision)
    def stale(error):
        return jsonify(error=str(error), reassessmentRequired=True,
                       currentEvidenceVersion=error.current_version), 409

    @bp.errorhandler(DecisionPending)
    def pending(error):
        return jsonify(error=f"decision recorded, not applied yet: {error}; "
                       "it is retried automatically"), 503

    @bp.errorhandler(DCRUnavailable)
    def unavailable(error):
        return jsonify(error=str(error)), 503

    @bp.post("/integrations/p4/incident")
    def handoff():
        """Foundation outbox consumer; internal, unauthenticated demo route."""
        return jsonify(service.accept_handoff(
            body(), request.headers.get("Idempotency-Key"))), 200

    @bp.get("/quality/incidents/<incident_id>")
    def incident(incident_id):
        return jsonify(service.view(incident_id))

    @bp.post("/quality/incidents/<incident_id>/sync")
    def sync(incident_id):
        return jsonify(service.sync(incident_id))

    @bp.post("/quality/incidents/<incident_id>/activities/<activity>")
    def activity(incident_id, activity):
        incident, event = service.execute(incident_id, activity, body())
        return jsonify(incident=incident, event=event), 200

    @bp.get("/shipments/<shipment_id>/quality")
    def shipment_quality(shipment_id):
        return jsonify(service.for_shipment(shipment_id))

    return bp
