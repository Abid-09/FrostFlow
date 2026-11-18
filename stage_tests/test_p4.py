"""Offline P4 checks: DCR investigation connected to the implemented P2 hold and P3 admission.

The P2/P3 registry code is real; Zeebe calls are substituted as in test_stage1.py.
P4 runs on the local DCR engine (deployer/dcr_client.LocalDCR) over
dcr/p4_quality_investigation.xml. These are not dcrgraphs.net repository executions.
"""

import itertools
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "deployer"), str(ROOT / "stage_tests")]
import test_stage1 as stage  # noqa: E402  (shares the app import and helpers)
service = stage.service

import dcr_client  # noqa: E402
import quality  # noqa: E402
from compliance.check import Graph, replay  # noqa: E402
from registry import Conflict  # noqa: E402

MODEL = ROOT / "dcr/p4_quality_investigation.xml"
INCIDENT = "DELAY:SIDDHI:RISK:late-1"


class DownDCR:
    mode, graph_id = "test-outage", None

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise dcr_client.DCRUnavailable("DCR unreachable (test)")
        return fail


class P4Integration(unittest.TestCase):
    # Reuse the P1/P2 fixtures without inheriting Preparation's tests.
    for _name in ("setUp", "create", "check", "loading_wait", "verify",
                  "start_p2_transport", "risk_at_index", "decide"):
        locals()[_name] = getattr(stage.Preparation, _name)

    def deliver_p4(self, expect=202):
        """Do what deliver_messages does for P4_INCIDENT_URL, for each pending handoff."""
        results = []
        for message in self.db.pending_messages(destination="p4-http"):
            response = self.http.post(
                "/integrations/p4/incident", json=message["variables"],
                headers={"Idempotency-Key": message["messageId"]})
            self.assertEqual(response.status_code, expect, response.json)
            self.db.message_result(message["messageId"])
            results.append(response.json)
        return results

    def held(self, event_id="SIDDHI:RISK:late-1", index=2):
        self.risk_at_index(index, event_id)
        return self.deliver_p4()

    def act(self, activity, incident=INCIDENT, **body):
        return self.http.post(f"/quality/incidents/{incident}/activities/{activity}",
                              json={"actor": "officer-anna", **body})

    def run_path(self, steps, incident=INCIDENT):
        for step in steps:
            body = {"reason": "evidence reviewed"} if step.startswith("Decide") else {}
            response = self.act(step, incident, **body)
            self.assertEqual(response.status_code, 200, (step, response.json))

    def incident(self, incident=INCIDENT):
        return self.http.get(f"/quality/incidents/{incident}").json

    def trace(self, incident=INCIDENT):
        dcr = set(quality.ROLES)
        return [e["eventType"] for e in self.db.events("S-test")
                if e.get("incidentId") == incident and e["eventType"] in dcr]

    def p3_waiting_on_hold(self):
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.update(p3ProcessInstanceKey="777", inspectionStatus="PASSED",
                          p3Status="AWAITING_ADMISSION")
            self.db.save(db, record)
        self.assertEqual(self.db.admit_p3_stock("S-test", 777), {"admissionOutcome": "HELD"})
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record["p3Status"] = "ON_HOLD"
            self.db.save(db, record)

    # -- P2 handoff -> DCR case ---------------------------------------------
    def test_p2_handoff_opens_one_dcr_case_and_keeps_hold(self):
        self.start_p2_transport()
        [accepted] = self.held()
        self.assertEqual(accepted["effect"], "incident-opened")
        view = self.incident()
        self.assertEqual(view["status"], "OPEN")
        self.assertEqual(view["dcrMode"], "local-dcr-engine")
        self.assertEqual(self.trace(), ["OpenIncident"])
        enabled = {e["id"] for e in view["dcr"]["events"] if e["enabled"]}
        self.assertIn("ReviewTemperatureLog", enabled)
        self.assertNotIn("CloseInvestigation", enabled)
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")
        types = [e["eventType"] for e in self.db.events("S-test")]
        self.assertLess(types.index("QualityHoldEstablished"), types.index("OpenIncident"))
        self.assertEqual(self.db.pending_messages(destination="p4-http"), [])

    def test_handoff_retries_are_idempotent_and_validated(self):
        self.start_p2_transport()
        self.risk_at_index(2)
        [message] = self.db.pending_messages(destination="p4-http")
        post = lambda body, key: self.http.post(  # noqa: E731
            "/integrations/p4/incident", json=body, headers={"Idempotency-Key": key})
        first = post(message["variables"], message["messageId"])
        again = post(message["variables"], message["messageId"])
        self.assertEqual((first.status_code, again.status_code), (202, 202))
        self.assertEqual(first.json, again.json)
        self.assertEqual(self.trace(), ["OpenIncident"])
        self.assertEqual(post(message["variables"], "other").status_code, 400)
        changed = json.loads(json.dumps(message["variables"]))
        changed["event"]["journeyId"] = "J-other"
        self.assertEqual(post(changed, message["messageId"]).status_code, 409)
        forged = json.loads(json.dumps(message["variables"]))
        forged["event"]["deliveryId"] = "P4:forged"
        forged["event"]["riskEvent"]["eventId"] = "never-accepted"
        self.assertEqual(post(forged, "P4:forged").status_code, 409)

    def test_second_alert_becomes_evidence_and_requires_reassessment(self):
        self.start_p2_transport()
        self.held()
        self.run_path(["ReviewTemperatureLog", "AssessEvidence"])
        decision = self.decide("delay-1", "ACCEPT_DELAY")
        self.db.apply_dispatcher_decision(
            "S-test", 999, decision, {"commandId": "delay-1", "simulatorAcknowledged": True})
        [second] = self.held("SIDDHI:RISK:late-2", 3)
        self.assertEqual((second["incidentId"], second["effect"]), (INCIDENT, "evidence-added"))
        self.assertEqual(self.trace()[-1], "AddAlertEvidence")
        self.assertEqual(self.act("DecideRelease", reason="x").status_code, 409)
        self.run_path(["AssessEvidence", "DecideRelease", "CloseInvestigation"])

    def test_dcr_outage_keeps_hold_and_case_opens_on_retry(self):
        self.start_p2_transport()
        with patch.object(service.quality, "get_dcr", DownDCR):
            [accepted] = self.held()
        self.assertEqual(accepted["effect"], "incident-opened")  # 202: durably accepted
        self.assertEqual(self.incident()["status"], "DCR_UNAVAILABLE")
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")
        self.assertEqual(self.act("ReviewTemperatureLog").status_code, 409)
        service.quality.retry_unfinished()
        self.assertEqual(self.incident()["status"], "OPEN")
        self.assertEqual(self.trace(), ["OpenIncident"])

    # -- DCR-enforced investigation and P3 effect ---------------------------
    def test_dcr_blocks_forbidden_actions(self):
        self.start_p2_transport()
        self.held()
        blocked = self.act("CloseInvestigation")
        self.assertEqual(blocked.status_code, 409)
        self.assertNotIn("CloseInvestigation", blocked.json["enabled"])
        self.assertEqual(self.act("AssessEvidence").status_code, 409)       # R4
        self.assertEqual(self.act("DecideRelease", reason="x").status_code, 409)
        self.run_path(["ReviewTemperatureLog", "AssessEvidence", "RequestAdditionalCheck"])
        for activity in ("AssessEvidence", "DecideRelease", "CloseInvestigation"):  # R7
            self.assertEqual(self.act(activity, reason="x").status_code, 409, activity)
        self.assertEqual(self.act("OpenIncident").status_code, 400)
        self.assertEqual(self.act("Unknown").status_code, 400)
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")

    def test_release_resumes_held_p3_admission(self):
        self.start_p2_transport()
        self.held()
        self.p3_waiting_on_hold()
        self.run_path(["ReviewWeatherData", "RequestCoolingAdjustment", "ReviewTemperatureLog"])
        self.run_path(["RecordCoolingAcknowledgement", "RecordRecoveryEvidence"])
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")  # no auto-release
        self.assertEqual(self.act("DecideRelease").status_code, 400)  # reason required
        self.run_path(["AssessEvidence", "DecideRelease", "CloseInvestigation"])
        record = self.db.get("S-test")
        self.assertEqual((record["qualityStatus"], record["qualityIncidentId"]),
                         ("RELEASED", INCIDENT))
        [message] = [m for m in self.db.pending_messages(destination="zeebe")
                     if m["name"] == "QualityStatusChanged"]
        self.db.record_p3_quality_status("S-test", 777, message["variables"]["qualityStatusEvent"])
        self.assertEqual(self.db.admit_p3_stock("S-test", 777), {"admissionOutcome": "ADMITTED"})
        view = self.incident()
        self.assertEqual((view["status"], view["decision"]["decision"]), ("CLOSED", "RELEASE"))
        self.assertTrue(replay(MODEL, self.trace())["accepting"])
        names = [e["eventType"] for e in self.db.events("S-test")]
        self.assertIn("DispositionRecorded", names)

    def test_replace_rejects_goods_and_later_alert_is_only_evidence(self):
        self.start_p2_transport()
        self.held()
        self.run_path(["ReviewTemperatureLog", "RequestCoolingAdjustment",
                       "RecordCoolingFailure"])
        self.assertEqual(self.act("RecordRecoveryEvidence").status_code, 409)  # R6
        self.run_path(["AssessEvidence", "DecideReplace"])
        self.assertEqual(self.act("DecideRelease", reason="x").status_code, 409)  # R8
        self.assertEqual(self.act("RequestAdditionalCheck").status_code, 409)
        self.run_path(["CloseInvestigation"])
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "REJECTED")
        self.assertEqual(self.act("ReviewWeatherData").status_code, 409)  # R9 closed
        decision = self.decide("delay-1", "ACCEPT_DELAY")
        self.db.apply_dispatcher_decision(
            "S-test", 999, decision, {"commandId": "delay-1", "simulatorAcknowledged": True})
        [late] = self.held("SIDDHI:RISK:late-2", 3)
        self.assertFalse(late["handoff"]["incidentOpened"])
        self.assertEqual(late["effect"], "evidence-recorded")
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "REJECTED")
        self.assertIn("LateAlertEvidenceRecorded",
                      [e["eventType"] for e in self.db.events("S-test")])
        self.assertTrue(replay(MODEL, self.trace())["accepting"])

    def test_decision_for_superseded_incident_does_not_change_status(self):
        self.start_p2_transport()
        self.held()
        self.run_path(["ReviewTemperatureLog", "AssessEvidence"])
        # A non-P4 stand-in releases the incident, then a new risk opens another one.
        self.db.accept_quality_status("S-test", "cli-release", "2026-12-01T10:30:00Z",
                                      "RELEASED", INCIDENT)
        decision = self.decide("delay-1", "ACCEPT_DELAY")
        self.db.apply_dispatcher_decision(
            "S-test", 999, decision, {"commandId": "delay-1", "simulatorAcknowledged": True})
        [second] = self.held("SIDDHI:RISK:late-2", 3)
        self.assertEqual(second["effect"], "incident-opened")
        self.run_path(["DecideReturn"])
        record = self.db.get("S-test")
        self.assertEqual((record["qualityStatus"], record["qualityIncidentId"]),
                         ("PENDING", "DELAY:SIDDHI:RISK:late-2"))
        self.assertIn("notApplied", self.incident()["decision"])
        self.assertIn("StaleDispositionIgnored",
                      [e["eventType"] for e in self.db.events("S-test")])

    def test_disposition_is_retried_when_the_registry_write_fails(self):
        self.start_p2_transport()
        self.held()
        self.run_path(["ReviewTemperatureLog", "AssessEvidence"])
        with patch.object(self.db, "accept_quality_status",
                          side_effect=RuntimeError("database is locked")):
            self.run_path(["DecideRelease"])
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")
        self.assertFalse(self.incident()["dispositionApplied"])
        service.quality.retry_unfinished()
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "RELEASED")
        self.assertTrue(self.incident()["dispositionApplied"])

    def test_external_acknowledgement_is_idempotent(self):
        self.start_p2_transport()
        self.held()
        self.run_path(["RequestCoolingAdjustment"])
        for _ in range(2):
            response = self.act("RecordCoolingAcknowledgement", actor="simulator",
                                eventId="ack-1")
            self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.trace().count("RecordCoolingAcknowledgement"), 1)
        reused = self.act("RecordCoolingFailure", actor="simulator", eventId="ack-1")
        self.assertEqual(reused.status_code, 409)

    # -- temperature excursions ---------------------------------------------
    def temperature(self, n=1, **change):
        body = dict(eventId=f"temp-{n}", shipmentId="S-test",
                    occurredAt="2026-12-01T10:10:00Z", source="siddhi",
                    inputMode="simulated", payload={"readings": 3})
        body.update(change)
        return self.http.post("/integrations/p4/temperature-excursion", json=body)

    def test_temperature_excursion_places_hold_then_opens_case(self):
        self.start_p2_transport()
        response = self.temperature()
        self.assertEqual(response.status_code, 202, response.json)
        incident = response.json["incidentId"]
        self.assertEqual(incident, "TEMP:temp-1")
        record = self.db.get("S-test")
        self.assertEqual((record["qualityStatus"], record["qualityIncidentId"]),
                         ("PENDING", incident))
        self.assertEqual(self.temperature().json, response.json)          # retry
        self.assertEqual(self.temperature(payload={"readings": 9}).status_code, 409)
        self.risk_at_index(2)        # a delay now joins the temperature incident
        [joined] = self.deliver_p4()
        self.assertEqual((joined["incidentId"], joined["effect"]), (incident, "evidence-added"))
        self.assertEqual(self.trace(incident), ["OpenIncident", "AddAlertEvidence"])

    def test_temperature_alert_validation(self):
        self.assertEqual(self.temperature().status_code, 404)
        self.create()
        self.assertEqual(self.temperature().status_code, 409)   # not in transport
        self.assertEqual(self.temperature(inputMode="guess").status_code, 400)
        self.assertEqual(self.temperature(occurredAt="2026-12-01T11:10:00+01:00")
                         .status_code, 400)
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "CLEAR")

    def test_demo_menu_executes_an_enabled_activity_through_the_api(self):
        from tools import demo

        self.start_p2_transport()
        self.held()

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
              patch("builtins.input", side_effect=pick),
              patch("builtins.print") as printed):
            demo.investigate_quality("http://example.test", "S-test")
        self.assertEqual(self.trace(), ["OpenIncident", "ReviewTemperatureLog"])
        self.assertEqual(self.db.events("S-test")[-1]["actor"], "quality-officer")

    def test_health_reports_dcr_engine(self):
        self.assertEqual(self.http.get("/health").json["integrations"]["p4DcrEngine"],
                         "local-dcr-engine")


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
    def test_compliance_rules_are_the_executed_graph(self):
        self.assertEqual((ROOT / "compliance/p4/rules.xml").read_bytes(), MODEL.read_bytes())

    def test_live_runner_scenarios_follow_the_graph(self):
        import check_p4_investigation as runner

        for scenario, _, steps, expected in runner.SCENARIOS:
            graph = Graph(MODEL)
            graph.execute("OpenIncident")
            for step in steps:
                if isinstance(step, tuple):
                    probe = Graph(MODEL)
                    probe.included, probe.executed, probe.pending = (
                        set(graph.included), set(graph.executed), set(graph.pending))
                    self.assertIsNotNone(probe.execute(step[1]), (scenario, step))
                else:
                    name = "AddAlertEvidence" if step == "temperature-alert" else step
                    self.assertIsNone(graph.execute(name), (scenario, step))
            self.assertFalse(graph.pending & graph.included, scenario)
            decision = next(s for s in steps if isinstance(s, str) and s.startswith("Decide"))
            self.assertEqual(expected, "ADMITTED" if decision == "DecideRelease" else "REJECTED")


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

    def test_unconfigured_client_reports_unavailable(self):
        with self.assertRaises(dcr_client.DCRUnavailable):
            dcr_client.DCRClient("", "", "").create_simulation()


del itertools, Conflict  # kept for readers of the fixtures

if __name__ == "__main__":
    unittest.main()
