"""P4 quality incident investigation executed in DCR.

How P4 fits the implemented P2/P3 contract (docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md):

- P2 places the quality hold (qualityStatus PENDING, qualityIncidentId) when a delivery
  risk is accepted and sends a durable `DeliveryRiskIncident` handoff to
  P4_INCIDENT_URL. P4 consumes it at POST /integrations/p4/incident: the first alert of
  an incident opens a DCR case (OpenIncident); later alerts become AddAlertEvidence.
- A temperature excursion (POST /integrations/p4/temperature-excursion) places the hold
  through Registry.accept_quality_status(PENDING) before any DCR call, or joins the
  shipment's active incident.
- The DCR engine decides which investigation activities are enabled; this service only
  forwards activities and records their business effects. If DCR is unreachable the
  hold stays and the case is retried.
- A decision is applied with Registry.accept_quality_status for the current incident
  (RELEASE -> RELEASED; REPLACE/RETURN -> REJECTED), which resumes a P3 instance waiting
  on the hold. A decision for a superseded incident does not change the status.
"""

import json
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from dcr_client import DCRUnavailable
from registry import Conflict, Registry, now

INPUT_MODES = {"live", "recorded", "simulated", "manual", "system"}

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


class UnknownIncident(KeyError):
    pass


class Blocked(Conflict):
    """DCR does not currently enable the requested activity."""

    def __init__(self, activity, enabled):
        super().__init__(f"DCR does not enable {activity} in this investigation now")
        self.enabled = enabled


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


def utc(data, field):
    value = text(data, field, limit=40)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 UTC timestamp") from error
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be a UTC timestamp")
    return value


def new_incident(incident_id, shipment_id, dcr):
    return dict(
        incidentId=incident_id,
        shipmentId=shipment_id,
        status="OPENING",
        openedAt=now(),
        alerts=[],
        pendingSystemEvents=["OpenIncident"],
        dcrMode=dcr.mode,
        dcrGraphId=getattr(dcr, "graph_id", None),
        dcrCaseId=None,
        decision=None,
        dispositionApplied=None,
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

    # -- alert intake -------------------------------------------------------
    def _attach_alert(self, db, record, incident_id, alert, opened, dcr):
        """Add an accepted alert to its incident. Returns (incident, effect).

        `dcr` is resolved before the registry transaction: the local engine shares
        the SQLite file and must not be opened inside a write transaction."""
        incident = find_incident(db, incident_id)
        if incident is None:
            if record.get("qualityStatus") != "PENDING":
                # The hold was already decided without a P4 case (e.g. a test stand-in).
                journal(db, record, dict(incidentId=incident_id, activityCount=0),
                        "LateAlertEvidenceRecorded", payload={"alertEventId": alert["eventId"]},
                        event_id=f"P4:{incident_id}:late:{alert['eventId']}",
                        occurred_at=alert["occurredAt"])
                return None, "evidence-recorded"
            incident = new_incident(incident_id, record["shipmentId"], dcr)
            effect = "incident-opened"
        elif incident["status"] == "CLOSED":
            journal(db, record, incident, "LateAlertEvidenceRecorded",
                    payload={"alertEventId": alert["eventId"]},
                    event_id=f"P4:{incident_id}:late:{alert['eventId']}",
                    occurred_at=alert["occurredAt"])
            incident["alerts"].append(alert["eventId"])
            save_incident(db, incident)
            return incident, "evidence-recorded"
        else:
            incident["pendingSystemEvents"].append("AddAlertEvidence")
            effect = "evidence-added"
        incident["alerts"].append(alert["eventId"])
        save_incident(db, incident)
        journal(db, record, incident, "QualityAlertReceived",
                actor=alert.get("source", "system"), payload={
                    "alertEventId": alert["eventId"], "alertType": alert["eventType"],
                    "incidentOpenedByProducer": opened},
                event_id=f"P4:{incident_id}:alert:{alert['eventId']}",
                occurred_at=alert["occurredAt"], input_mode=alert.get("inputMode", "system"),
                source=alert.get("source"))
        return incident, effect

    def accept_handoff(self, data, idempotency_key):
        """Consume P2's durable DeliveryRiskIncident handoff (at-least-once delivery)."""
        event = data.get("event")
        if not isinstance(event, dict):
            raise ValueError("event must be a JSON object")
        delivery_id = text(event, "deliveryId", limit=256)
        if idempotency_key != delivery_id:
            raise ValueError("Idempotency-Key must match event.deliveryId")
        if event.get("eventType") != "DeliveryRiskDetected":
            raise ValueError("eventType must be DeliveryRiskDetected")
        risk = event.get("riskEvent")
        if not isinstance(risk, dict):
            raise ValueError("riskEvent must be a JSON object")
        clean = dict(
            deliveryId=delivery_id,
            shipmentId=text(event, "shipmentId", limit=100),
            journeyId=text(event, "journeyId", limit=100),
            incidentId=text(event, "incidentId", limit=256),
            incidentOpened=bool(event.get("incidentOpened")),
            qualityStatus=text(event, "qualityStatus", limit=20),
            riskEventId=text(risk, "eventId", limit=256),
            occurredAt=utc(risk, "occurredAt"),
        )
        key = f"p4-handoff:{delivery_id}"
        dcr = self.dcr
        with self.registry.transaction() as db:
            ensure_schema(db)
            row = db.execute("SELECT data FROM results WHERE id=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if saved["handoff"] != clean:
                    raise Conflict("deliveryId reused with different content")
                return saved
            record = Registry.read(db, clean["shipmentId"])
            if record["journeyId"] != clean["journeyId"]:
                raise Conflict("handoff journey does not match the shipment")
            if not db.execute(
                "SELECT 1 FROM results WHERE id=?",
                (f"delivery-risk:{clean['shipmentId']}:{clean['riskEventId']}",),
            ).fetchone():
                raise Conflict("delivery risk was not accepted by the P2 integration")
            alert = dict(eventId=clean["riskEventId"], eventType="DeliveryRiskDetected",
                         occurredAt=clean["occurredAt"], source="p2-transport",
                         inputMode="system")
            incident, effect = self._attach_alert(
                db, record, clean["incidentId"], alert, clean["incidentOpened"], dcr)
            accepted = dict(handoff=clean, incidentId=clean["incidentId"], effect=effect)
            db.execute("INSERT INTO results VALUES (?,?)", (key, json.dumps(accepted)))
        if incident is not None:
            self.sync(clean["incidentId"])
        return accepted

    def receive_temperature_alert(self, data):
        """Siddhi temperature excursion: hold first, then open or join the incident."""
        alert = dict(
            eventId=text(data, "eventId", limit=100),
            eventType="TemperatureExcursionDetected",
            shipmentId=text(data, "shipmentId", limit=100),
            occurredAt=utc(data, "occurredAt"),
            source=text(data, "source", limit=100),
            inputMode=text(data, "inputMode", limit=20),
            payload=data.get("payload", {}),
        )
        if alert["inputMode"] not in INPUT_MODES:
            raise ValueError(f"inputMode must be one of {sorted(INPUT_MODES)}")
        if not isinstance(alert["payload"], dict):
            raise ValueError("payload must be a JSON object")
        key = f"p4-temperature:{alert['shipmentId']}:{alert['eventId']}"
        dcr = self.dcr
        with self.registry.transaction() as db:
            ensure_schema(db)
            row = db.execute("SELECT data FROM results WHERE id=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if saved["alert"] != alert:
                    raise Conflict("eventId reused with different alert content")
                return saved
            record = Registry.read(db, alert["shipmentId"])
            if not record.get("p2ProcessInstanceKey"):
                raise Conflict("temperature alerts need a shipment in transport")
            status, current = record.get("qualityStatus", "CLEAR"), record.get(
                "qualityIncidentId")
        if status in ("PENDING", "REJECTED") and current:
            incident_id, opened = current, False
        else:
            incident_id, opened = f"TEMP:{alert['eventId']}", True
            # The hold is established through the shared P3 quality contract before DCR.
            self.registry.accept_quality_status(
                alert["shipmentId"], f"{incident_id}:hold", alert["occurredAt"],
                "PENDING", incident_id)
        with self.registry.transaction() as db:
            row = db.execute("SELECT data FROM results WHERE id=?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
            record = Registry.read(db, alert["shipmentId"])
            incident, effect = self._attach_alert(db, record, incident_id, alert, opened, dcr)
            accepted = dict(alert=alert, incidentId=incident_id, effect=effect)
            db.execute("INSERT INTO results VALUES (?,?)", (key, json.dumps(accepted)))
        if incident is not None:
            self.sync(incident_id)
        return accepted

    # -- DCR synchronisation ------------------------------------------------
    def sync(self, incident_id):
        """Create the DCR case if missing, execute queued integration events and
        apply a recorded decision that has not reached the shared status yet."""
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
            self._apply_disposition(incident_id)
            with self.registry.transaction() as db:
                return read_incident(db, incident_id)

    def retry_unfinished(self):
        """Background retry for cases waiting on DCR or on the shared status."""
        with self.registry.transaction() as db:
            ensure_schema(db)
            waiting = [
                json.loads(r[0])["incidentId"]
                for r in db.execute("SELECT data FROM incidents")
                if json.loads(r[0])["status"] in ("OPENING", "DCR_UNAVAILABLE")
                or json.loads(r[0]).get("dispositionApplied") is False
            ]
        for incident_id in waiting:
            self.sync(incident_id)

    def _apply_disposition(self, incident_id):
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
        decision = incident.get("decision")
        if not decision or incident.get("dispositionApplied") is not False:
            return
        status = "RELEASED" if decision["decision"] == "RELEASE" else "REJECTED"
        try:
            self.registry.accept_quality_status(
                incident["shipmentId"], f"{incident_id}:disposition",
                decision["decidedAt"], status, incident_id)
            effect = "DispositionRecorded"
        except Conflict as error:
            # Superseded incident, or stock was already admitted: keep the record only.
            effect, decision["notApplied"] = "StaleDispositionIgnored", str(error)
        except Exception as error:  # e.g. a locked registry: retried in the background
            with self.registry.transaction() as db:
                incident = read_incident(db, incident_id)
                incident["lastError"] = f"disposition not applied yet: {error}"
                save_incident(db, incident)
            return
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
            record = Registry.read(db, incident["shipmentId"])
            journal(db, record, incident, effect, actor=decision["officer"], payload={
                "decision": decision["decision"], "reason": decision["reason"],
                "qualityStatus": status if effect == "DispositionRecorded" else None,
                "evidence": [f"dcr:{incident.get('dcrCaseId')}"]},
                event_id=f"P4:{incident_id}:{effect}", occurred_at=decision["decidedAt"])
            incident.update(decision=decision, dispositionApplied=True)
            save_incident(db, incident)

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
            case = incident["dcrCaseId"]
            enabled = [e["id"] for e in self.dcr.events(case)["events"] if e["enabled"]]
            if activity not in enabled:
                raise Blocked(activity, enabled)
            self.dcr.execute(case, activity)
            with self.registry.transaction() as db:
                incident = read_incident(db, incident_id)
                record = Registry.read(db, incident["shipmentId"])
                payload = {k: v for k, v in (("reason", reason), ("note", note)) if v}
                event = journal(db, record, incident, activity, actor, payload,
                                event_id=journal_id, input_mode="manual")
                if activity in DECISIONS:
                    incident.update(dispositionApplied=False, decision=dict(
                        decision=DECISIONS[activity][0], officer=actor, reason=reason,
                        decidedAt=event["occurredAt"]))
                if activity == "CloseInvestigation":
                    incident.update(status="CLOSED", closedAt=event["occurredAt"])
                save_incident(db, incident)
            self._apply_disposition(incident_id)
        return incident, event

    # -- views --------------------------------------------------------------
    def view(self, incident_id):
        with self.registry.transaction() as db:
            incident = read_incident(db, incident_id)
        dcr = None
        if incident.get("dcrCaseId") and incident["status"] != "CLOSED":
            try:
                dcr = self.dcr.events(incident["dcrCaseId"])
            except DCRUnavailable as error:
                dcr = {"unavailable": str(error)}
        return dict(incident, dcr=dcr)

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

    @bp.errorhandler(DCRUnavailable)
    def unavailable(error):
        return jsonify(error=str(error)), 503

    @bp.post("/integrations/p4/incident")
    def handoff():
        """Durable P2 DeliveryRiskIncident handoff; internal, unauthenticated demo route."""
        return jsonify(service.accept_handoff(
            body(), request.headers.get("Idempotency-Key"))), 202

    @bp.post("/integrations/p4/temperature-excursion")
    def temperature():
        return jsonify(service.receive_temperature_alert(body())), 202

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
