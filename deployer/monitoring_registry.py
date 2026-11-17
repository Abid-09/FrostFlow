"""Durable container sessions, ordered evaluation and future assessment handoffs."""
import json
import math
from datetime import datetime, timezone

from monitoring import SCENARIOS, evaluate, seconds
from weather import eligible


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MonitoringRegistry:
    @staticmethod
    def monitoring_start(record, at):
        if "monitoringProfile" in record and "monitoring" not in record:
            record["monitoring"] = {
                "sessionId": f"CONTAINER:{record['shipmentId']}:{record['journeyId']}",
                "state": "ACTIVE", "startedAt": at, "sequence": 0, "evaluatedThrough": 0,
                "scenario": "normal", "scenarioIndex": 0, "finalization": "OPEN",
                "window": [], "episode": {}, "lastSampleAt": None,
            }

    def monitored_shipments(self):
        with self.transaction() as db:
            return [r for (raw,) in db.execute("SELECT data FROM shipments")
                    if (r := json.loads(raw)).get("monitoring", {}).get("state") in ("ACTIVE", "CLOSED")]

    def temperature_scenario(self, shipment_id, command_id, actor, scenario):
        from registry import Conflict
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {SCENARIOS}")
        with self.transaction() as db:
            record = self.read(db, shipment_id)
            identity = f"temperature-command:{shipment_id}:{command_id}"
            payload = {"commandId": command_id, "actor": actor, "scenario": scenario}
            old = db.execute("SELECT data FROM results WHERE id=?", (identity,)).fetchone()
            if old:
                if json.loads(old[0]) != payload:
                    raise Conflict("commandId reused with different scenario data")
                return payload
            session = record.get("monitoring", {})
            if session.get("state") != "ACTIVE":
                raise Conflict("temperature simulation requires active monitoring")
            session.update(scenario=scenario, scenarioIndex=0)
            self.event(db, record, "TemperatureScenarioChanged", actor=actor, process="MONITORING",
                       event_id=identity, input_mode="simulated", payload=payload)
            db.execute("INSERT INTO results VALUES (?,?)", (identity, json.dumps(payload)))
            self.save(db, record)
            return payload

    def store_weather(self, shipment_id, context):
        if context is None:
            return
        with self.transaction() as db:
            record = self.read(db, shipment_id)
            previous = record.get("weather", {})
            record["weather"] = context
            if context.get("observationId") != previous.get("observationId") or context.get("available") != previous.get("available"):
                self.event(db, record, "OutdoorConditionsUpdated", process="MONITORING",
                           event_id=f"WEATHER:{shipment_id}:{context.get('observationId', context['retrievedAt'])}",
                           source="open-meteo", input_mode=context["inputMode"], payload=context)
            self.save(db, record)

    def assessment_hold(self, db, record, reason, evidence, priority="normal", episode=None):
        """Only new material evidence creates a version; continuous alerts coalesce."""
        shipment_id = record["shipmentId"]
        key = f"{reason}:{episode or evidence['eventId']}"
        if key in record.setdefault("materialAssessmentKeys", []):
            return record.get("qualityIncidentId")
        record["materialAssessmentKeys"].append(key)
        active = record.get("qualityStatus") in ("PENDING", "REJECTED") and record.get("qualityIncidentId")
        opened = not active
        if opened:
            record.update(qualityStatus="PENDING", qualityIncidentId=(f"DELAY:{evidence['eventId']}" if reason == "delivery-risk" else f"ASSESS:{shipment_id}:{evidence['eventId']}"),
                          assessmentReasons=[], assessmentPriority="normal", evidenceVersion=0, assessmentEvidenceKeys=[])
        record["assessmentEvidenceKeys"].append(key)
        record["evidenceVersion"] = record.get("evidenceVersion", 0) + 1
        if reason not in record.setdefault("assessmentReasons", []):
            record["assessmentReasons"].append(reason)
        if priority == "urgent":
            record["assessmentPriority"] = "urgent"
        record.setdefault("assessmentPriority", "normal")
        session = record.get("monitoring", {})
        at = stamp()
        extension = {
            "schemaVersion": 1, "reason": reason, "priority": record["assessmentPriority"],
            "evidenceVersion": record["evidenceVersion"], "sessionId": session.get("sessionId"),
            "profile": record.get("monitoringProfile"), "observationReference": evidence,
            "interval": {"from": evidence.get("from", evidence.get("occurredAt", at)), "to": evidence.get("occurredAt", at)},
            "weather": evidence.get("weather", record.get("weather")), "eta": evidence.get("eta", record.get("etaAssessment")),
            "missingEvidence": evidence.get("missingEvidence", []) +
                ([] if evidence.get("weather", record.get("weather", {})) else ["outdoor conditions"]) +
                ([] if evidence.get("eta", record.get("etaAssessment", {})) else ["ETA context"]),
            "source": "delivery-risk-integration" if reason == "delivery-risk" else "container-monitoring",
            "inputMode": evidence.get("inputMode", "simulated"), "occurredAt": evidence.get("occurredAt", at), "recordedAt": at,
        }
        handoff_id = f"P4:Assessment:{shipment_id}:{record['qualityIncidentId']}:{record['evidenceVersion']}"
        record["lastAssessment"] = extension
        envelope = {"deliveryId": handoff_id, "eventId": handoff_id, "eventType": "AssessmentRequested",
                    "shipmentId": shipment_id, "journeyId": record["journeyId"], "productId": record["productId"],
                    "batchId": record["batchId"], "incidentId": record["qualityIncidentId"], "incidentOpened": opened,
                    "qualityStatus": record["qualityStatus"], "assessment": extension,
                    "occurredAt": extension["occurredAt"], "recordedAt": at}
        if reason == "delivery-risk":
            envelope.update(eventType="DeliveryRiskDetected", riskEvent=evidence)
        self.enqueue(db, handoff_id, "DeliveryRiskIncident" if reason == "delivery-risk" else "AssessmentRequested", shipment_id, {"event": envelope},
                     needs_ack=True, destination="p4-http")
        self.event(db, record, "QualityHoldEstablished" if opened else "AssessmentEvidenceAdded", process="P4",
                   event_id=handoff_id, source="container-monitoring", input_mode="simulated", payload=envelope)
        return record["qualityIncidentId"]

    def sample_container(self, shipment_id, at=None, celsius=None):
        """Persist before delivery. Optional explicit values support recorded replay tests."""
        at = datetime.fromtimestamp(seconds(at or stamp()), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.transaction() as db:
            record = self.read(db, shipment_id)
            session = record.get("monitoring", {})
            if session.get("state") != "ACTIVE":
                return None
            settings = record["monitoringProfile"]
            if session.get("lastSampleAt") and seconds(at) - seconds(session["lastSampleAt"]) < settings["sampleSeconds"]:
                return None
            session["lastSampleAt"] = at
            if session["scenario"] == "missing-telemetry" and celsius is None:
                self.save(db, record)
                return None
            if celsius is None:
                fault = session["scenario"] == "sustained-excursion" or (session["scenario"] == "isolated-spike" and session["scenarioIndex"] == 0)
                celsius = settings["faultC"] if fault else settings["normalC"]
                if session["scenario"] == "sustained-excursion" and session["scenarioIndex"] < 3:
                    celsius = settings["normalC"] + session["scenarioIndex"] * settings["riseC"] / 2
            if isinstance(celsius, bool) or not isinstance(celsius, (int, float)) or not math.isfinite(celsius):
                raise ValueError("celsius must be finite")
            session["scenarioIndex"] += 1
            session["sequence"] += 1
            seq = session["sequence"]
            event_id = f"{session['sessionId']}:{seq}"
            observation = {"eventId": event_id, "sequence": seq, "shipmentId": shipment_id,
                           "journeyId": record["journeyId"], "celsius": celsius, "unit": "°C",
                           "occurredAt": at, "recordedAt": stamp(), "source": "container-simulator",
                           "inputMode": "simulated", "profileId": settings["profileId"], "scenario": session["scenario"]}
            window = (session["window"] + [observation])[-3:]
            session.update(window=window, lastValidAt=at)
            session["episode"].pop("missing", None)
            excursion, rising = evaluate(window, settings, at)
            for label, qualifies in (("excursion", excursion), ("combined", rising)):
                if not qualifies:
                    session["episode"].pop(label, None)
                elif label not in session["episode"]:
                    session["episode"][label] = seq
            context = {"observation": observation, "window": window, "weather": record.get("weather"),
                       "eta": record.get("etaAssessment"), "position": record.get("currentPosition", [48.8566, 2.3522]),
                       "episodes": dict(session["episode"]), "routeRevision": record.get("routeRevision", 0)}
            db.execute("INSERT INTO container_observations VALUES (?,?,?,?)", (shipment_id, seq, json.dumps(context), None))
            payload = {"eventId": event_id, "shipmentId": shipment_id, "journeyId": record["journeyId"], "sequence": seq,
                       "candidateCombined": self.combined_candidate(context, settings, at, True),
                       "count": len(window), "firstC": window[0]["celsius"],
                       "middleC": window[1]["celsius"] if len(window) > 1 else celsius, "lastC": celsius,
                       "middleMillis": int(seconds(window[1]["occurredAt"] if len(window) > 1 else at) * 1000),
                       "thresholdC": settings["thresholdC"], "riseC": settings["riseC"],
                       "spanMillis": int(settings["spanSeconds"] * 1000), "maxGapMillis": int(settings["maxGapSeconds"] * 1000),
                       "oldestMillis": int(seconds(window[0]["occurredAt"]) * 1000),
                       "newestMillis": int(seconds(at) * 1000), "freshMillis": int(settings["freshSeconds"] * 1000),
                       "combinedUntilMillis": int(min(seconds((context.get("eta") or {}).get("observedAt", at)) + 30,
                                                       seconds((context.get("weather") or {}).get("providerTime", at)) + settings["weatherMaxAgeSeconds"]) * 1000)}
            self.enqueue(db, event_id, "ContainerObservation", shipment_id, {"event": payload}, destination="container-siddhi")
            self.save(db, record)
            return observation

    @staticmethod
    def combined_candidate(context, settings, at, rising):
        eta = context.get("eta") or {}
        weather = context.get("weather")
        return bool(rising and eta.get("revision", 0) == context.get("routeRevision", 0) and eta.get("qualified") and eta.get("late")
                    and 0 <= seconds(at) - seconds(eta["observedAt"]) <= 30
                    and eligible(weather, context["position"], settings, at)
                    and weather["conditions"]["temperature_2m"] >= settings["hotC"])

    def acknowledge_container(self, data):
        from registry import Conflict
        required = ("shipmentId", "journeyId", "eventId", "sequence", "evaluatedAtMillis", "excursion", "combined")
        if any(k not in data for k in required):
            raise ValueError("container acknowledgement lacks required fields")
        if type(data["sequence"]) is not int or data["sequence"] < 1 or any(type(data[k]) is not bool for k in ("excursion", "combined")):
            raise ValueError("invalid evaluation sequence or result")
        if type(data["evaluatedAtMillis"]) not in (int, float) or not math.isfinite(data["evaluatedAtMillis"]):
            raise ValueError("invalid evaluation timestamp")
        if not 0 <= data["evaluatedAtMillis"] <= seconds(stamp()) * 1000 + 1000:
            raise ValueError("evaluation timestamp is out of range")
        evaluated_at = datetime.fromtimestamp(data["evaluatedAtMillis"] / 1000, timezone.utc).isoformat()
        with self.transaction() as db:
            record = self.read(db, data["shipmentId"])
            row = db.execute("SELECT data,ack FROM container_observations WHERE shipment_id=? AND sequence=?",
                             (data["shipmentId"], data["sequence"])).fetchone()
            if not row:
                raise Conflict("evaluation references an unknown observation")
            context = json.loads(row[0])
            obs = context["observation"]
            if any(data[k] != obs[k] for k in ("eventId", "shipmentId", "journeyId", "sequence")):
                raise Conflict("evaluation identity does not match stored observation")
            if row[1]:
                previous = json.loads(row[1])
                if any(data[k] != previous[k] for k in ("excursion", "combined")):
                    raise Conflict("conflicting evaluation result")
                return {"status": "accepted", "sequence": data["sequence"]}
            if seconds(evaluated_at) + 0.001 < seconds(obs["occurredAt"]) or seconds(evaluated_at) > seconds(stamp()) + 1:
                raise ValueError("evaluation timestamp is outside the observation interval")
            excursion, rising = evaluate(context["window"], record["monitoringProfile"], evaluated_at)
            combined = self.combined_candidate(context, record["monitoringProfile"], evaluated_at, rising)
            if data["excursion"] != excursion or data["combined"] != combined:
                raise Conflict("evaluation does not match stored evidence")
            db.execute("UPDATE container_observations SET ack=? WHERE shipment_id=? AND sequence=?",
                       (json.dumps(data), data["shipmentId"], data["sequence"]))
            session = record["monitoring"]
            while True:
                next_seq = session["evaluatedThrough"] + 1
                pending = db.execute("SELECT data,ack FROM container_observations WHERE shipment_id=? AND sequence=?",
                                     (record["shipmentId"], next_seq)).fetchone()
                if not pending or not pending[1]:
                    break
                item, ack = json.loads(pending[0]), json.loads(pending[1])
                evidence = {**item["observation"], "weather": item["weather"], "eta": item["eta"],
                            "from": item["window"][0]["occurredAt"]}
                evaluation_age = ack["evaluatedAtMillis"] / 1000 - seconds(item["observation"]["occurredAt"])
                was_excursion, was_rising = evaluate(item["window"], record["monitoringProfile"], item["observation"]["occurredAt"])
                if (evaluation_age > record["monitoringProfile"]["freshSeconds"] or
                    (was_excursion and not ack["excursion"]) or
                    (self.combined_candidate(item, record["monitoringProfile"], item["observation"]["occurredAt"], was_rising) and not ack["combined"])):
                    self.assessment_hold(db, record, "monitoring-evaluation-unavailable",
                                         {**evidence, "missingEvidence": ["fresh Siddhi evaluation"]}, episode="evaluation")
                for flag, reason, priority, label in (("excursion", "temperature-excursion", "normal", "excursion"),
                                                      ("combined", "combined-transport-risk", "urgent", "combined")):
                    applied_key = f"{label}:{item['episodes'].get(label, next_seq)}"
                    if ack[flag] and applied_key not in session.setdefault("appliedEpisodes", []):
                        session["appliedEpisodes"].append(applied_key)
                        self.assessment_hold(db, record, reason, evidence, priority, str(item["episodes"].get(label, next_seq)))
                session["evaluatedThrough"] = next_seq
            self.monitoring_finalize(db, record)
            self.save(db, record)
            return {"status": "accepted", "sequence": data["sequence"]}

    def monitoring_close(self, db, record, at):
        session = record.get("monitoring")
        if session and session["state"] == "ACTIVE":
            # Test the durable silence clock in the same transaction as receipt.
            self.monitoring_watchdog(db, record, at)
            session.update(state="CLOSED", closedAt=at, closingSequence=session["sequence"], finalization="PENDING")
            if not session["sequence"]:
                self.assessment_hold(db, record, "container-telemetry-unavailable",
                                     {"eventId": "empty-session", "occurredAt": at, "missingEvidence": ["container readings"]})
            self.monitoring_finalize(db, record)

    def monitoring_finalize(self, db, record):
        session = record.get("monitoring", {})
        if session.get("state") != "CLOSED" or session.get("finalization") in ("FINALIZED", "HELD"):
            return
        if session["evaluatedThrough"] == session["closingSequence"]:
            session["finalization"] = "FINALIZED"
            self.monitoring_finalized_event(db, record)

    def monitoring_watchdog(self, db, record, at):
        session = record.get("monitoring", {})
        if session.get("state") == "ACTIVE":
            if seconds(at) - seconds(session.get("lastValidAt", session["startedAt"])) >= record["monitoringProfile"]["missingSeconds"]:
                episode = session["episode"].setdefault("missing", f"silence:{session['sequence']}")
                self.assessment_hold(db, record, "container-telemetry-unavailable",
                                     {"eventId": episode, "occurredAt": at, "missingEvidence": ["valid container reading"]}, episode=episode)
        elif session.get("finalization") == "PENDING":
            if seconds(at) - seconds(session["closedAt"]) >= record["monitoringProfile"]["finalizeSeconds"]:
                self.assessment_hold(db, record, "monitoring-evaluation-unavailable",
                                     {"eventId": f"close:{session['closingSequence']}", "occurredAt": at,
                                      "missingEvidence": ["evaluation through closing sequence"]}, episode="evaluation")
                session["finalization"] = "HELD"
                self.monitoring_finalized_event(db, record)

    def tick_monitoring(self, at=None):
        at = at or stamp()
        # Run watchdog before sampling so restart/scheduler gaps cannot be hidden by a new reading.
        with self.transaction() as db:
            for (raw,) in db.execute("SELECT data FROM shipments").fetchall():
                record = json.loads(raw)
                if "monitoring" in record:
                    self.monitoring_watchdog(db, record, at)
                    self.save(db, record)
        for record in self.monitored_shipments():
            if record["monitoring"]["state"] == "ACTIVE":
                self.sample_container(record["shipmentId"], at)

    def finalize_monitoring(self, shipment_id):
        with self.transaction() as db:
            record = self.read(db, shipment_id)
            if "monitoringProfile" not in record:
                return True
            session = record.get("monitoring", {})
            self.monitoring_watchdog(db, record, stamp())
            self.monitoring_finalize(db, record)
            self.save(db, record)
            return session.get("finalization") in ("FINALIZED", "HELD")

    def monitoring_finalized_event(self, db, record):
        session = record["monitoring"]
        self.event(db, record, "MonitoringFinalized", process="P3",
                   event_id=f"P3:{record['shipmentId']}:MonitoringFinalized",
                   payload={"closingSequence": session["closingSequence"],
                            "evaluatedThrough": session["evaluatedThrough"],
                            "finalization": session["finalization"],
                            "incidentId": record.get("qualityIncidentId")})

    def container_observations(self, shipment_id):
        with self.transaction() as db:
            self.read(db, shipment_id)
            return [json.loads(raw) for (raw,) in db.execute(
                "SELECT data FROM container_observations WHERE shipment_id=? ORDER BY sequence", (shipment_id,))]
