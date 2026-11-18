"""Offline P4 checks on monitored shipments against the merged foundation (contract v1).

The P1/P2/P3 and monitoring registry code is real (temporary SQLite); Zeebe calls are
substituted as in test_stage1.py and container evaluations are acknowledged in-process
as in test_monitoring.py. P4 runs on the local DCR engine over
dcr/p4_quality_investigation.xml. These are not dcrgraphs.net repository executions.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "deployer"), str(ROOT / "stage_tests")]
import test_stage1 as stage  # noqa: E402  (shares the app import and fixtures)

import dcr_client  # noqa: E402
import quality  # noqa: E402
from compliance.check import Graph, replay  # noqa: E402
from monitoring import evaluate, seconds  # noqa: E402
from registry import Registry  # noqa: E402
from transport_integrations import validate_p4_acknowledgement  # noqa: E402

service = stage.service
MODEL = ROOT / "dcr/p4_quality_investigation.xml"
START = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


class DownDCR:
    mode, graph_id = "test-outage", None

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise dcr_client.DCRUnavailable("DCR unreachable (test)")
        return fail


class P4Flow(unittest.TestCase):
    """New monitored shipments; no guard is bypassed and monitoringProfile is kept."""

    for _name in ("setUp", "loading_wait", "verify", "start_p2_transport",
                  "risk_at_index", "decide", "check"):
        locals()[_name] = getattr(stage.Preparation, _name)

    def setUp(self):  # noqa: F811
        stage.Preparation.setUp(self)
        self.clock = START
        for target in ("registry.now", "monitoring_registry.stamp"):
            patcher = patch(target, side_effect=lambda: iso(self.clock))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.p3_key = 777

    def create(self, **kwargs):
        response = self.http.post("/shipments", json=stage.sample(**kwargs))
        self.assertEqual(response.status_code, 202, response.json)
        self.assertIn("monitoringProfile", self.db.get("S-test"))

    def tick(self, seconds_=5):
        self.clock += timedelta(seconds=seconds_)

    # -- monitoring (as test_monitoring.py) ---------------------------------
    def readings(self, *values):
        for value in values:
            self.tick()
            observation = self.db.sample_container("S-test", iso(self.clock), value)
            self.assertIsNotNone(observation)
            self.ack(observation["sequence"] if isinstance(observation, dict)
                     and "sequence" in observation else self.db.get("S-test")["monitoring"]["sequence"])

    def ack(self, seq):
        with self.db.transaction() as db:
            item = json.loads(db.execute(
                "SELECT data FROM container_observations WHERE shipment_id=? AND sequence=?",
                ("S-test", seq)).fetchone()[0])
        at = item["observation"]["occurredAt"]
        profile = self.db.get("S-test")["monitoringProfile"]
        excursion, rising = evaluate(item["window"], profile, at)
        combined = self.db.combined_candidate(item, profile, at, rising)
        data = {k: item["observation"][k] for k in ("shipmentId", "journeyId", "eventId", "sequence")}
        data.update(evaluatedAtMillis=int(seconds(at) * 1000), excursion=excursion, combined=combined)
        return self.db.acknowledge_container(data)

    # -- P4 delivery (what the outbox loop does for P4_INCIDENT_URL) --------
    def deliver_p4(self):
        acknowledgements = []
        for message in self.db.pending_messages(destination="p4-http"):
            response = self.http.post("/integrations/p4/incident", json=message["variables"],
                                      headers={"Idempotency-Key": message["messageId"]})
            self.assertEqual(response.status_code, 200, response.json)
            validate_p4_acknowledgement(response.json, message["messageId"])
            self.db.message_result(message["messageId"])
            acknowledgements.append(response.json)
        return acknowledgements

    def incident_id(self):
        return self.db.get("S-test")["qualityIncidentId"]

    def act(self, activity, incident=None, **body):
        incident = incident or self.incident_id()
        return self.http.post(f"/quality/incidents/{incident}/activities/{activity}",
                              json={"actor": "officer-anna", **body})

    def run_path(self, steps, incident=None):
        for step in steps:
            body = {"reason": "evidence reviewed"} if step.startswith("Decide") else {}
            response = self.act(step, incident, **body)
            self.assertEqual(response.status_code, 200, (step, response.json))

    def incident(self, incident=None):
        return self.http.get(f"/quality/incidents/{incident or self.incident_id()}").json

    def trace(self, incident=None):
        incident = incident or self.incident_id()
        return [e["eventType"] for e in self.db.events("S-test")
                if e.get("incidentId") == incident and e["eventType"] in quality.ROLES]

    # -- P2 arrival and P3 receiving (real endpoints and registry steps) ----
    def arrive_and_receive(self):
        self.tick()
        arrival = self.db.queue_transport_event(
            "S-test", "ShipmentArrived", "SIM:J-test:ShipmentArrived", iso(self.clock),
            {"recipientId": "C-01"}, source="p2-simulator", input_mode="simulated")
        self.db.record_arrival("S-test", 999, arrival["event"])
        notification = self.db.pending_messages(destination="p3-http")[0]["variables"]["event"]
        response = self.http.post("/integrations/p3/arrival", json={"event": notification},
                                  headers={"Idempotency-Key": notification["deliveryId"]})
        self.assertEqual(response.status_code, 202, response.json)
        self.db.record_p3_arrival("S-test", self.p3_key, notification)
        self.tick()
        received = self.http.post("/shipments/S-test/receiving/receipt", json={
            "eventId": "receipt-1", "occurredAt": iso(self.clock), "recipientId": "C-01",
            "quantityReceived": 3, "receiver": "cafeteria"})
        self.assertEqual(received.status_code, 202, received.json)
        receipt = next(m["variables"]["receiptEvent"] for m in self.db.pending_messages()
                       if m["name"] == "GoodsReceived")
        self.db.record_p3_receipt("S-test", self.p3_key, receipt)
        p2_receipt = next(m["variables"]["receiptEvent"] for m in self.db.pending_messages()
                          if m["name"] == "ReceiptAcknowledged")
        self.db.release_vehicle("S-test", 999, p2_receipt)
        inspected = self.http.post("/shipments/S-test/receiving/inspection", json={
            "eventId": "inspection-1", "occurredAt": iso(self.clock),
            "inspector": "quality-checker", "passed": True})
        self.assertEqual(inspected.status_code, 202, inspected.json)
        inspection = next(m["variables"]["inspectionEvent"] for m in self.db.pending_messages()
                          if m["name"] == "GoodsInspected")
        self.db.record_p3_inspection("S-test", self.p3_key, inspection)

    def resume_p3(self):
        message = [m for m in self.db.pending_messages(destination="zeebe")
                   if m["name"] == "QualityStatusChanged"][-1]
        self.db.record_p3_quality_status("S-test", self.p3_key,
                                         message["variables"]["qualityStatusEvent"])
        return self.db.admit_p3_stock("S-test", self.p3_key)

    # -- scenarios required by docs/P4-FOUNDATION-HANDOVER.md ---------------
    def test_release_after_receipt_during_hold_then_admission(self):
        self.start_p2_transport()
        self.readings(3, 3, 3)
        self.risk_at_index(2)
        [ack] = self.deliver_p4()
        self.assertEqual((ack["status"], ack["effect"]), ("accepted", "incident-opened"))
        view = self.incident()
        self.assertEqual((view["status"], view["evidenceVersion"]), ("OPEN", 1))
        self.assertEqual(view["evidence"][0]["reason"], "delivery-risk")
        self.assertIn("assessment", view["evidence"][0]["request"])
        self.assertEqual(self.act("CloseInvestigation").status_code, 409)
        self.arrive_and_receive()
        record = self.db.get("S-test")
        self.assertTrue(record["vehicleReleased"])          # receipt released transport
        self.assertEqual(record["qualityStatus"], "PENDING")  # ... but not quality
        self.assertEqual(record["monitoring"]["finalization"], "FINALIZED")
        self.assertEqual(self.db.admit_p3_stock("S-test", self.p3_key),
                         {"admissionOutcome": "HELD"})
        self.run_path(["ReviewTemperatureLog", "AssessEvidence", "DecideRelease"])
        record = self.db.get("S-test")
        self.assertEqual((record["qualityStatus"], record["qualityIncidentId"]),
                         ("RELEASED", view["incidentId"]))
        accepted = next(m for m in self.db.pending_messages(destination="zeebe")
                        if m["name"] == "QualityStatusChanged")
        self.assertEqual(accepted["variables"]["qualityStatusEvent"]["payload"]
                         ["expectedEvidenceVersion"], 1)
        self.assertEqual(self.resume_p3(), {"admissionOutcome": "ADMITTED"})
        self.run_path(["CloseInvestigation"])
        self.assertEqual(self.incident()["decision"]["assessedEvidenceVersion"], 1)
        self.assertTrue(replay(MODEL, self.trace())["accepting"])

    def test_temperature_excursion_replace_and_later_evidence_stays_rejected(self):
        self.start_p2_transport()
        self.readings(10, 10, 10)
        [ack] = self.deliver_p4()
        incident = self.incident_id()
        self.assertTrue(incident.startswith("ASSESS:"))
        evidence = self.incident()["evidence"][0]
        self.assertEqual((evidence["eventType"], evidence["reason"]),
                         ("AssessmentRequested", "temperature-excursion"))
        self.run_path(["ReviewTemperatureLog", "RequestCoolingAdjustment",
                       "RecordCoolingFailure"])
        self.assertEqual(self.act("RecordRecoveryEvidence").status_code, 409)
        self.run_path(["AssessEvidence", "DecideReplace", "CloseInvestigation"])
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "REJECTED")
        self.risk_at_index(2)                      # later delay evidence, same incident
        [late] = self.deliver_p4()
        self.assertEqual((late["incidentId"], late["effect"]), (incident, "evidence-recorded"))
        record = self.db.get("S-test")
        self.assertEqual((record["qualityStatus"], record["qualityIncidentId"]),
                         ("REJECTED", incident))
        self.assertEqual(self.incident(incident)["status"], "CLOSED")
        self.assertIn("LateAlertEvidenceRecorded",
                      [e["eventType"] for e in self.db.events("S-test")])
        self.assertTrue(replay(MODEL, self.trace(incident))["accepting"])

    def test_additional_check_then_return_rejects_goods(self):
        self.start_p2_transport()
        self.readings(10, 10, 10)
        self.deliver_p4()
        self.run_path(["ReviewRouteAndETA", "ReviewTemperatureLog", "RequestAdditionalCheck"])
        for blocked in ("AssessEvidence", "DecideReturn", "CloseInvestigation"):
            self.assertEqual(self.act(blocked, reason="x").status_code, 409, blocked)
        self.run_path(["CompleteAdditionalCheck", "AssessEvidence", "DecideReturn",
                       "CloseInvestigation"])
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "REJECTED")

    def test_newer_evidence_requires_reassessment_and_stale_decision_is_not_applied(self):
        self.start_p2_transport()
        self.readings(3, 3, 3)
        self.risk_at_index(2)
        self.deliver_p4()
        self.run_path(["ReviewTemperatureLog", "AssessEvidence"])     # assessed version 1
        self.readings(10, 10, 10)                                     # v2 produced, not delivered
        self.assertEqual(self.db.get("S-test")["evidenceVersion"], 2)
        stale = self.act("DecideRelease", reason="looks fine")
        self.assertEqual(stale.status_code, 409, stale.json)
        self.assertTrue(stale.json["reassessmentRequired"])
        record = self.db.get("S-test")
        self.assertEqual(record["qualityStatus"], "PENDING")
        view = self.incident()
        self.assertIsNone(view["decision"])
        self.assertEqual(view["unappliedDecisions"][0]["expectedEvidenceVersion"], 1)
        self.assertNotIn("DecideRelease", self.trace())                 # not executed in DCR
        self.assertEqual(self.act("DecideRelease", reason="again").status_code, 409)
        [newer] = self.deliver_p4()
        self.assertEqual(newer["effect"], "evidence-added")
        self.assertEqual(self.incident()["evidenceVersion"], 2)
        blocked = self.act("DecideRelease", reason="x")              # DCR: Assess is pending
        self.assertEqual(blocked.status_code, 409)
        self.assertNotIn("DecideRelease", blocked.json["enabled"])
        self.run_path(["AssessEvidence", "DecideRelease", "CloseInvestigation"])
        record = self.db.get("S-test")
        self.assertEqual(record["qualityStatus"], "RELEASED")
        self.assertEqual(self.incident()["decision"]["assessedEvidenceVersion"], 2)
        self.assertIn("StaleDispositionRefused",
                      [e["eventType"] for e in self.db.events("S-test")])
        self.assertTrue(replay(MODEL, self.trace())["accepting"])

    def test_duplicate_delivery_and_conflicting_reuse(self):
        self.start_p2_transport()
        self.readings(10, 10, 10)
        [message] = self.db.pending_messages(destination="p4-http")
        post = lambda body, key: self.http.post(  # noqa: E731
            "/integrations/p4/incident", json=body, headers={"Idempotency-Key": key})
        first = post(message["variables"], message["messageId"])
        again = post(message["variables"], message["messageId"])
        self.assertEqual((first.json["status"], again.json["status"]), ("accepted", "duplicate"))
        validate_p4_acknowledgement(again.json, message["messageId"])
        self.assertEqual(self.trace(), ["OpenIncident"])
        self.assertEqual(len(self.incident()["evidence"]), 1)
        self.assertEqual(post(message["variables"], "other").status_code, 400)
        changed = json.loads(json.dumps(message["variables"]))
        changed["event"]["assessment"]["priority"] = "urgent"
        self.assertEqual(post(changed, message["messageId"]).status_code, 409)
        forged = json.loads(json.dumps(message["variables"]))
        forged["event"]["deliveryId"] = forged["event"]["eventId"] = "P4:forged"
        self.assertEqual(post(forged, "P4:forged").status_code, 409)
        unknown = json.loads(json.dumps(message["variables"]))
        unknown["event"]["assessment"]["reason"] = "weather"
        self.assertEqual(post(unknown, message["messageId"]).status_code, 400)

    def test_dcr_outage_keeps_request_and_case_opens_on_retry(self):
        self.start_p2_transport()
        self.readings(10, 10, 10)
        with patch.object(service.quality, "get_dcr", DownDCR):
            [ack] = self.deliver_p4()                  # durable acknowledgement anyway
        self.assertEqual(ack["status"], "accepted")
        self.assertEqual(self.db.pending_messages(destination="p4-http"), [])
        self.assertEqual(self.incident()["status"], "DCR_UNAVAILABLE")
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")
        self.assertEqual(self.act("ReviewTemperatureLog").status_code, 409)
        service.quality.retry_unfinished()
        self.assertEqual(self.incident()["status"], "OPEN")
        self.assertEqual(self.trace(), ["OpenIncident"])

    def test_transient_failures_keep_a_durable_decision_with_one_identity(self):
        self.start_p2_transport()
        self.readings(10, 10, 10)
        self.deliver_p4()
        self.run_path(["ReviewTemperatureLog", "AssessEvidence"])
        with patch.object(self.db, "accept_quality_status",
                          side_effect=RuntimeError("database is locked")):
            response = self.act("DecideReplace", reason="too warm")
        self.assertEqual(response.status_code, 503, response.json)
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")
        pending = self.incident()["pendingDecision"]
        self.assertFalse(pending["registryApplied"])
        with patch.object(service.quality, "get_dcr", DownDCR):
            service.quality.retry_unfinished()           # registry applied, DCR still down
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "REJECTED")
        self.assertTrue(self.incident()["pendingDecision"]["registryApplied"])
        service.quality.retry_unfinished()               # DCR back: decision executed
        view = self.incident()
        self.assertIsNone(view["pendingDecision"])
        self.assertEqual(view["decision"]["qualityStatusEventId"], pending["eventId"])
        self.run_path(["CloseInvestigation"])
        self.assertTrue(replay(MODEL, self.trace())["accepting"])

    def test_demo_option_19_executes_an_enabled_activity(self):
        from tools import demo

        self.start_p2_transport()
        self.readings(10, 10, 10)
        self.deliver_p4()

        def local_http(base, path, data=None):
            response = (self.http.get(path) if data is None
                        else self.http.post(path, json=data))
            return response.status_code, response.json

        def pick(prompt):
            if prompt.startswith("Activity"):
                listed = [c.args[0] for c in printed.call_args_list if c.args]
                line = next(x for x in listed if x.strip().endswith("ReviewTemperatureLog"))
                return line.split()[0]
            return ""

        with (patch.object(demo, "http", side_effect=local_http),
              patch.object(demo, "choose_case", return_value=("S-test", "V-01", 3)),
              patch("builtins.input", side_effect=pick),
              patch("builtins.print") as printed):
            demo.perform("http://example.test", "19")
        self.assertEqual(self.trace(), ["OpenIncident", "ReviewTemperatureLog"])

    def test_health_reports_dcr_engine_and_consumer(self):
        health = self.http.get("/health").json["integrations"]
        self.assertEqual(health["p4DcrEngine"], "local-dcr-engine")


class HandoverFixtures(unittest.TestCase):
    """docs/P4-FOUNDATION-HANDOVER.md fixtures, consumed in order by the real P4 service."""

    def test_contract_examples_are_accepted_and_versions_enforced(self):
        from p4_contract_examples import produce

        with tempfile.TemporaryDirectory() as directory:
            seen = {}

            def before_rejection(registry, initial, urgent):
                service_ = quality.QualityService(
                    lambda: registry, lambda: engine)
                for message in registry.pending_messages("p4-http"):
                    ack = service_.accept_handoff(message["variables"], message["messageId"])
                    validate_p4_acknowledgement(ack, message["messageId"])
                    registry.message_result(message["messageId"])
                    seen[message["variables"]["event"]["assessment"]["reason"]] = ack["effect"]
                again = service_.accept_handoff(initial, initial["event"]["deliveryId"])
                self.assertEqual(again["status"], "duplicate")
                incident = initial["event"]["incidentId"]
                view = service_.view(incident)
                self.assertEqual(view["evidenceVersion"],
                                 registry.get("CONTRACT-001")["evidenceVersion"])
                self.assertEqual(view["priority"], "urgent")
                self.assertEqual(len(view["evidence"]), 5)

            engine = None
            path = Path(directory) / "registry.sqlite"
            Registry(path)  # create the file before the engine shares it
            engine = dcr_client.LocalDCR(MODEL, path)
            produce(directory, before_rejection=before_rejection)
        self.assertEqual(seen, {
            "delivery-risk": "incident-opened", "temperature-excursion": "evidence-added",
            "combined-transport-risk": "evidence-added",
            "container-telemetry-unavailable": "evidence-added",
            "monitoring-evaluation-unavailable": "evidence-added"})

    def test_legacy_delivery_risk_without_extension_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite"
            registry = Registry(path)
            registry.create(stage.sample())
            with registry.transaction() as db:
                record = registry.read(db, "S-test")
                record.pop("monitoringProfile", None)  # legacy, unmonitored record
                registry.establish_delivery_risk_hold(db, record, dict(
                    eventId="legacy-1", occurredAt="2026-12-01T10:20:00Z",
                    eventType="DeliveryRiskDetected", payload={}))
                registry.save(db, record)
            [message] = registry.pending_messages("p4-http")
            self.assertNotIn("assessment", message["variables"]["event"])
            service_ = quality.QualityService(lambda: registry,
                                              lambda: dcr_client.LocalDCR(MODEL, path))
            ack = service_.accept_handoff(message["variables"], message["messageId"])
            validate_p4_acknowledgement(ack, message["messageId"])
            incident = ack["incidentId"]
            for step in ("ReviewTemperatureLog", "AssessEvidence"):
                service_.execute(incident, step, {"actor": "officer"})
            service_.execute(incident, "DecideRelease", {"actor": "officer", "reason": "ok"})
            self.assertEqual(registry.get("S-test")["qualityStatus"], "RELEASED")
            self.assertIsNone(service_.view(incident)["decision"]["assessedEvidenceVersion"])


class LocalEngine(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dcr.sqlite"

    def test_marking_survives_restart_and_matches_checker(self):
        engine = dcr_client.LocalDCR(MODEL, self.path)
        case = engine.create_simulation("INC-1")
        steps = ["OpenIncident", "ReviewTemperatureLog", "AssessEvidence"]
        for step in steps:
            engine.execute(case, step)
        restarted = dcr_client.LocalDCR(MODEL, self.path)
        state = restarted.events(case, filter_="all")
        graph = Graph(MODEL)
        for step in steps:
            graph.execute(step)
        for event in state["events"]:
            probe = Graph(MODEL)
            probe.included, probe.executed, probe.pending = (
                set(graph.included), set(graph.executed), set(graph.pending))
            self.assertEqual(event["enabled"], probe.execute(event["id"]) is None, event)
        with self.assertRaises(dcr_client.DCRUnavailable):
            restarted.execute(case, "CloseInvestigation")

    def test_factory_prefers_repository_only_with_credentials(self):
        with patch.dict("os.environ", {"DCR_GRAPH_ID": "", "DCR_USERNAME": "",
                                       "DCR_PASSWORD": "", "DCR_MODE": "auto"}):
            self.assertIsInstance(dcr_client.make_dcr(self.path), dcr_client.LocalDCR)
        with patch.dict("os.environ", {"DCR_GRAPH_ID": "1", "DCR_USERNAME": "u",
                                       "DCR_PASSWORD": "p", "DCR_MODE": "auto"}):
            self.assertIsInstance(dcr_client.make_dcr(self.path), dcr_client.DCRClient)


class P4Graph(unittest.TestCase):
    def test_alternative_valid_orders_are_accepting(self):
        for trace in [
            ["OpenIncident", "ReviewTemperatureLog", "AssessEvidence", "DecideRelease",
             "CloseInvestigation"],
            ["OpenIncident", "ReviewRouteAndETA", "ReviewTemperatureLog",
             "RequestAdditionalCheck", "CompleteAdditionalCheck", "AssessEvidence",
             "DecideReturn", "CloseInvestigation"],
        ]:
            self.assertEqual(replay(MODEL, trace), dict(valid=True, accepting=True, pending=[]))

    def test_forbidden_traces_are_rejected(self):
        cases = {
            ("OpenIncident", "ReviewWeatherData", "AssessEvidence"): "condition",
            ("OpenIncident", "ReviewTemperatureLog", "AssessEvidence",
             "CloseInvestigation"): "condition",
            ("OpenIncident", "ReviewTemperatureLog", "AssessEvidence",
             "RequestAdditionalCheck", "DecideRelease"): "milestone",
            ("OpenIncident", "ReviewTemperatureLog", "AssessEvidence", "DecideRelease",
             "DecideReplace"): "excluded",
            ("OpenIncident", "ReviewTemperatureLog", "RequestCoolingAdjustment",
             "AssessEvidence", "DecideRelease", "CloseInvestigation"): "milestone",
            ("OpenIncident", "ReviewTemperatureLog", "RequestCoolingAdjustment",
             "RecordCoolingFailure", "RecordRecoveryEvidence"): "excluded",
        }
        for trace, kind in cases.items():
            result = replay(MODEL, list(trace))
            self.assertFalse(result["valid"], trace)
            self.assertIn(kind, result["reason"], trace)

    def test_unfinished_case_is_not_accepting(self):
        result = replay(MODEL, ["OpenIncident", "ReviewTemperatureLog", "AssessEvidence",
                                "DecideRelease"])
        self.assertEqual(result["pending"], ["CloseInvestigation"])

    def test_every_reachable_state_can_finish_and_no_activity_is_dead(self):
        """Exhaustive search of the marking space (backs the report's validation claim)."""
        start = Graph(MODEL)
        events = sorted(start.events)
        key = lambda g: (frozenset(g.included), frozenset(g.executed),  # noqa: E731
                         frozenset(g.pending))
        seen, frontier, edges, fired = {key(start): start}, [start], {}, set()
        while frontier:
            graph = frontier.pop()
            here = key(graph)
            edges[here] = []
            for event in events:
                nxt = Graph.__new__(Graph)
                nxt.events, nxt.relations = graph.events, graph.relations
                nxt.included, nxt.executed, nxt.pending = (
                    set(graph.included), set(graph.executed), set(graph.pending))
                if nxt.execute(event) is None:
                    fired.add(event)
                    there = key(nxt)
                    edges[here].append(there)
                    if there not in seen:
                        seen[there] = nxt
                        frontier.append(nxt)
        accepting = {k for k in seen if not (k[2] & k[0])}
        can_finish, changed = set(accepting), True
        while changed:
            changed = False
            for state, targets in edges.items():
                if state not in can_finish and any(t in can_finish for t in targets):
                    can_finish.add(state)
                    changed = True
        self.assertEqual(len(seen), 3425)
        self.assertEqual(set(seen) - can_finish, set())
        self.assertEqual(fired, set(events))


class Evidence(unittest.TestCase):
    def test_compliance_rules_are_the_executed_graph_without_roles(self):
        import re

        rules = (ROOT / "compliance/p4/rules.xml").read_text()
        self.assertNotIn("<role>", rules)
        self.assertEqual(rules, re.sub(r"\s*<roles>.*?</roles>", "", MODEL.read_text(),
                                       flags=re.S))
        executed, checked = Graph(MODEL), Graph(ROOT / "compliance/p4/rules.xml")
        self.assertEqual((executed.events, executed.relations, executed.included,
                          executed.pending),
                         (checked.events, checked.relations, checked.included, checked.pending))

    def test_live_runner_scenarios_follow_the_graph(self):
        import check_p4_investigation as runner

        for scenario, _, trigger, before, after, expected in runner.SCENARIOS:
            graph = Graph(MODEL)
            graph.execute("OpenIncident")
            for step in before + after:
                if isinstance(step, tuple) and step[0] == "blocked":
                    probe = Graph(MODEL)
                    probe.included, probe.executed, probe.pending = (
                        set(graph.included), set(graph.executed), set(graph.pending))
                    self.assertIsNotNone(probe.execute(step[1]), (scenario, step))
                elif isinstance(step, tuple) and step[0] == "await-evidence":
                    self.assertIsNone(graph.execute("AddAlertEvidence"), scenario)
                elif isinstance(step, str):
                    self.assertIsNone(graph.execute(step), (scenario, step))
            self.assertFalse(graph.pending & graph.included, scenario)
            decision = next(s for s in after if isinstance(s, str) and s.startswith("Decide"))
            self.assertEqual(expected, "ADMITTED" if decision == "DecideRelease" else "REJECTED")
            self.assertIn(trigger, ("delay", "sustained-excursion"))


class Client(unittest.TestCase):
    def test_parses_repository_events_and_simulation_header(self):
        raw = (b'<events isAccepting="false"><event id="AssessEvidence" included="true" '
               b'enabled="true" pending="true" executed="false" label="Assess" roles="QO"/>'
               b'</events>')
        parsed = dcr_client.parse_events(raw)
        self.assertFalse(parsed["isAccepting"])
        self.assertEqual(parsed["events"][0]["id"], "AssessEvidence")
        self.assertTrue(parsed["events"][0]["enabled"])
        self.assertEqual(dcr_client.parse_events('"' + raw.decode().replace('"', '\\"') + '"')
                         ["events"][0]["pending"], True)

        class Response:
            status, headers = 201, {"X-DCR-Simulation-ID": "29535"}
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        client = dcr_client.DCRClient("42", "user", "secret")
        with patch.object(dcr_client, "urlopen", return_value=Response()) as call:
            self.assertEqual(client.create_simulation("INC-1"), "29535")
        request = call.call_args[0][0]
        self.assertEqual(request.full_url, "https://repository.dcrgraphs.net/api/graphs/42/sims")
        self.assertTrue(request.headers["Authorization"].startswith("Basic "))

    def test_repository_nesting_groups_are_not_reported_as_activities(self):
        raw = (b'<events isAccepting="false">'
               b'<event id="A19" included="true" enabled="true" pending="false" executed="false"/>'
               b'<event id="A19.A18" included="true" enabled="true" pending="false" executed="false"/>'
               b'<event id="ReviewTemperatureLog" included="true" enabled="true" pending="false" '
               b'executed="false"/></events>')

        class Response:
            status, headers = 200, {}
            def read(self): return raw
            def __enter__(self): return self
            def __exit__(self, *a): return False

        client = dcr_client.DCRClient("2015919", "user", "secret",
                                      activities=dcr_client.model_activities(MODEL))
        with patch.object(dcr_client, "urlopen", return_value=Response()):
            events = client.events("1")["events"]
        self.assertEqual([e["id"] for e in events], ["ReviewTemperatureLog"])
        self.assertEqual(len(dcr_client.model_activities(MODEL)), 16)

    def test_unconfigured_client_reports_unavailable(self):
        with self.assertRaises(dcr_client.DCRUnavailable):
            dcr_client.DCRClient("", "", "").create_simulation()



if __name__ == "__main__":
    unittest.main()
