"""Monitoring regressions use real SQLite transactions; engine/providers are substituted."""
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deployer"))
from registry import Conflict, Registry
from monitoring import evaluate, profile, seconds


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "registry.sqlite"
        self.db = Registry(self.path)
        self.base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
        self.create()

    def at(self, offset):
        return (self.base + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")

    def create(self, sid="S", vehicle="V-01", product="PRD-001"):
        batch = "DEMO-CHICKEN-001" if product == "PRD-004" else "DEMO-MILK-001"
        self.db.create(dict(shipmentId=sid, journeyId="J" + sid, productId=product, batchId=batch,
                            vehicleId=vehicle, recipientId="C-01", routeId="route-01", quantity=1,
                            deliveryDeadline=self.at(5)))
        with self.db.transaction() as db:
            record = self.db.read(db, sid)
            self.db.monitoring_start(record, self.at(0))
            record.update(p3ProcessInstanceKey="3", inspectionStatus="PASSED", p3Status="AWAITING_ADMISSION")
            self.db.save(db, record)

    def sample(self, offset, value=3, sid="S"):
        return self.db.sample_container(sid, self.at(offset), value)

    def ack(self, seq, offset=None, sid="S"):
        with self.db.transaction() as db:
            item = json.loads(db.execute("SELECT data FROM container_observations WHERE shipment_id=? AND sequence=?", (sid, seq)).fetchone()[0])
        at = self.at(offset) if offset is not None else item["observation"]["occurredAt"]
        excursion, rising = evaluate(item["window"], self.db.get(sid)["monitoringProfile"], at)
        combined = self.db.combined_candidate(item, self.db.get(sid)["monitoringProfile"], at, rising)
        data = {k: item["observation"][k] for k in ("shipmentId", "journeyId", "eventId", "sequence")}
        data.update(evaluatedAtMillis=int(seconds(at) * 1000), excursion=excursion, combined=combined)
        return self.db.acknowledge_container(data)

    def close(self, offset=11, sid="S"):
        with self.db.transaction() as db:
            record = self.db.read(db, sid)
            self.db.monitoring_close(db, record, self.at(offset))
            self.db.save(db, record)

    def disposition(self, status="RELEASED", version=None):
        record = self.db.get("S")
        return self.db.accept_quality_status("S", "D" + str(record["evidenceVersion"]), self.at(20), status,
                                             record["qualityIncidentId"], record["evidenceVersion"] if version is None else version)

    def test_normal_ordered_finalization_exact_inventory(self):
        for t in (0, 5, 10):
            self.sample(t)
        self.ack(3)
        self.close()
        self.assertEqual(self.db.get("S")["monitoring"]["evaluatedThrough"], 0)
        with patch("registry.now", return_value=self.at(12)), self.assertRaises(Conflict):
            self.db.admit_p3_stock("S", 3)
        self.ack(1)
        self.ack(2)
        self.ack(3)
        self.assertEqual(self.db.get("S")["monitoring"]["finalization"], "FINALIZED")
        self.assertEqual(self.db.admit_p3_stock("S", 3), {"admissionOutcome": "ADMITTED"})
        self.assertEqual(self.db.admit_p3_stock("S", 3), {"admissionOutcome": "ADMITTED"})
        self.assertEqual(self.db.receiving_inventory()["available"], 1)
        self.assertEqual(self.db.inventory()["available"], 100)

    def test_spike_and_threshold_boundaries(self):
        for t, value in ((0, 3), (5, 10), (10, 3), (15, 5), (20, 5), (25, 5)):
            self.sample(t, value)
        for seq in range(1, 7):
            self.ack(seq)
        self.assertEqual(self.db.get("S")["qualityStatus"], "CLEAR")
        self.create("C", "V-02", "PRD-004")
        self.assertEqual(self.db.get("C")["monitoringProfile"]["thresholdC"], 4)
        for t in (0, 5, 10):
            self.sample(t, 4.01, "C")
            self.ack(t // 5 + 1, sid="C")
        self.assertEqual(self.db.get("C")["qualityStatus"], "PENDING")
        self.assertEqual(self.db.get("S")["qualityStatus"], "CLEAR")

    def test_excursion_coalesces_release_and_new_episode(self):
        for t in (0, 5, 10, 15):
            self.sample(t, 10)
            self.ack(t // 5 + 1)
        first = self.db.get("S")["qualityIncidentId"]
        self.assertEqual(self.db.get("S")["evidenceVersion"], 1)
        self.disposition()
        self.sample(20, 10)
        self.ack(5)
        self.assertEqual(self.db.get("S")["qualityStatus"], "RELEASED")
        for t, value in ((25, 3), (30, 10), (35, 10), (40, 10)):
            self.sample(t, value)
            self.ack(t // 5 + 1)
        self.assertNotEqual(self.db.get("S")["qualityIncidentId"], first)
        self.assertEqual(self.db.get("S")["qualityStatus"], "PENDING")

    def test_missing_after_restart_and_close_timeout(self):
        self.db.temperature_scenario("S", "cmd", "test", "missing-telemetry")
        self.db.tick_monitoring(self.at(0))
        self.db = Registry(self.path)
        self.db.tick_monitoring(self.at(20))
        self.assertIn("container-telemetry-unavailable", self.db.get("S")["assessmentReasons"])
        self.db.tick_monitoring(self.at(25))
        self.assertEqual(self.db.get("S")["evidenceVersion"], 1)
        self.db.temperature_scenario("S", "cmd2", "test", "normal")
        self.sample(30)
        self.close(31)
        self.db.tick_monitoring(self.at(51))
        record = self.db.get("S")
        self.assertEqual(record["monitoring"]["finalization"], "HELD")
        self.assertIn("monitoring-evaluation-unavailable", record["assessmentReasons"])
        self.assertEqual(self.db.admit_p3_stock("S", 3), {"admissionOutcome": "HELD"})
        with self.assertRaises(Conflict):
            self.disposition(version=1)
        self.disposition()
        self.assertEqual(self.db.admit_p3_stock("S", 3), {"admissionOutcome": "ADMITTED"})

    def test_delayed_evaluation_holds_and_never_releases(self):
        for t in (0, 5, 10):
            self.sample(t, 10)
        for seq in (1, 2, 3):
            self.ack(seq, 40)
        self.assertIn("monitoring-evaluation-unavailable", self.db.get("S")["assessmentReasons"])
        self.assertEqual(self.db.get("S")["qualityStatus"], "PENDING")

    def test_weather_changes_priority_below_standalone_threshold(self):
        for sid, temp in (("S", 32), ("C", 20)):
            if sid == "C":
                self.create("C", "V-02")
            context = {"available": True, "observationId": sid, "retrievedAt": self.at(0), "providerTime": self.at(0),
                       "coordinates": [48.8566, 2.3522], "conditions": {"temperature_2m": temp}, "inputMode": "recorded-fixture"}
            self.db.store_weather(sid, context)
            with self.db.transaction() as db:
                record = self.db.read(db, sid)
                record["etaAssessment"] = {"qualified": True, "late": True, "observedAt": self.at(0), "revision": 0}
                self.db.save(db, record)
            for t, value in ((0, 3), (5, 3.3), (10, 3.6)):
                self.sample(t, value, sid)
                self.ack(t // 5 + 1, sid=sid)
        self.assertEqual(self.db.get("S")["assessmentPriority"], "urgent")
        self.assertEqual(self.db.get("S")["assessmentReasons"], ["combined-transport-risk"])
        self.assertEqual(self.db.get("C")["qualityStatus"], "CLEAR")
        handoff = self.db.pending_messages("p4-http")[0]
        self.assertEqual(handoff["variables"]["event"]["assessment"]["weather"]["conditions"]["temperature_2m"], 32)
        self.db.message_result(handoff["messageId"], "mock consumer unavailable")
        self.assertEqual(self.db.pending_messages("p4-http")[0], {**handoff, "lastError": "mock consumer unavailable"})
        self.db.message_result(handoff["messageId"])
        self.assertEqual(self.db.pending_messages("p4-http"), [])

    def test_scenarios_idempotency_and_profile_snapshot(self):
        accepted = self.db.temperature_scenario("S", "cmd", "actor", "isolated-spike")
        self.assertEqual(self.db.temperature_scenario("S", "cmd", "actor", "isolated-spike"), accepted)
        with self.assertRaises(Conflict):
            self.db.temperature_scenario("S", "cmd", "actor", "normal")
        self.assertEqual(self.db.sample_container("S", self.at(0))["celsius"], 10)
        self.assertEqual(self.db.sample_container("S", self.at(5))["celsius"], 3)
        self.assertIsNone(self.db.sample_container("S", self.at(5)))
        self.assertEqual(Registry(self.path).get("S")["monitoringProfile"], profile("PRD-001", {}))
        self.close(6)
        self.assertIsNone(self.db.sample_container("S", self.at(10)))

    def test_terminal_rejection_retains_identity(self):
        for t in (0, 5, 10):
            self.sample(t, 10)
            self.ack(t // 5 + 1)
        self.disposition("REJECTED")
        incident = self.db.get("S")["qualityIncidentId"]
        self.sample(15, 3)
        self.ack(4)
        for t in (20, 25, 30):
            self.sample(t, 10)
            self.ack(t // 5 + 1)
        record = self.db.get("S")
        self.assertEqual(record["qualityStatus"], "REJECTED")
        self.assertEqual(record["qualityIncidentId"], incident)
        last = self.db.pending_messages("p4-http")[-1]["variables"]["event"]
        self.assertFalse(last["incidentOpened"])
        self.assertEqual(last["qualityStatus"], "REJECTED")

    def test_stale_future_gaps_and_short_span_do_not_qualify(self):
        settings = profile("PRD-001", {})
        def run(times, at):
            return evaluate([{"occurredAt": self.at(t), "celsius": 10} for t in times], settings, self.at(at))
        for times, at in (([0, 5, 10], 16), ([0, 5, 10], 9), ([0, 11, 16], 16), ([0, 4, 8], 8), ([0, 0, 10], 10)):
            self.assertEqual(run(times, at), (False, False))
        self.assertEqual(run([0, 5, 10], 15), (True, False))

    def test_invalid_acknowledgements_and_exact_retries(self):
        self.sample(0)
        self.ack(1)
        with self.db.transaction() as db:
            raw = db.execute("SELECT ack FROM container_observations WHERE shipment_id='S' AND sequence=1").fetchone()[0]
        data = json.loads(raw)
        self.assertEqual(self.db.acknowledge_container(data)["status"], "accepted")
        with self.assertRaises(Conflict):
            self.db.acknowledge_container({**data, "excursion": True})
        with self.assertRaises(Conflict):
            self.db.acknowledge_container({**data, "eventId": "wrong"})
        with self.assertRaises(Conflict):
            self.db.acknowledge_container({**data, "sequence": 999})
        with self.assertRaises(ValueError):
            self.db.acknowledge_container({**data, "evaluatedAtMillis": 10**100})

    def test_empty_session_and_release_not_reopened_without_new_episode(self):
        self.close(1)
        record = self.db.get("S")
        self.assertEqual(record["monitoring"]["finalization"], "FINALIZED")
        self.assertEqual(record["qualityStatus"], "PENDING")
        self.assertEqual(self.db.admit_p3_stock("S", 3), {"admissionOutcome": "HELD"})
        self.disposition()
        self.db.tick_monitoring(self.at(50))
        self.assertEqual(self.db.get("S")["qualityStatus"], "RELEASED")

    def test_mock_consumer_retry_receives_same_delivery_and_payload(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.error import HTTPError
        from transport_integrations import post_json, validate_p4_acknowledgement
        for t in (0, 5, 10):
            self.sample(t, 10)
            self.ack(t // 5 + 1)
        deliveries = []
        class Consumer(BaseHTTPRequestHandler):
            def do_POST(inner):
                body = json.loads(inner.rfile.read(int(inner.headers["Content-Length"])))
                deliveries.append((inner.headers["Idempotency-Key"], body))
                inner.send_response(503 if len(deliveries) == 1 else 200)
                inner.end_headers()
                inner.wfile.write(json.dumps({"deliveryId": inner.headers["Idempotency-Key"], "status": "accepted"}).encode())
            def log_message(inner, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Consumer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            message = self.db.pending_messages("p4-http")[0]
            url = f"http://127.0.0.1:{server.server_port}/assessment"
            with self.assertRaises(HTTPError):
                post_json(url, message["variables"], {"Idempotency-Key": message["messageId"]})
            self.db.message_result(message["messageId"], "HTTP 503")
            self.assertEqual(self.db.get("S")["qualityStatus"], "PENDING")
            retry = self.db.pending_messages("p4-http")[0]
            response = post_json(url, retry["variables"], {"Idempotency-Key": retry["messageId"]})
            validate_p4_acknowledgement(response, retry["messageId"])
            for invalid in ({}, {"deliveryId": "wrong", "status": "accepted"}):
                with self.assertRaises(ValueError):
                    validate_p4_acknowledgement(invalid, retry["messageId"])
            self.db.message_result(retry["messageId"])
            self.assertEqual(deliveries[0], deliveries[1])
            self.assertEqual(self.db.pending_messages("p4-http"), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_material_evidence_invalidates_old_release_and_escalates_once(self):
        with self.db.transaction() as db:
            record = self.db.read(db, "S")
            incident = self.db.assessment_hold(db, record, "delivery-risk", {"eventId": "delay", "occurredAt": self.at(0)})
            self.db.save(db, record)
        old_version = self.db.get("S")["evidenceVersion"]
        with self.db.transaction() as db:
            record = self.db.read(db, "S")
            self.db.assessment_hold(db, record, "combined-transport-risk", {"eventId": "hot-rise", "occurredAt": self.at(10)}, "urgent", "rise-1")
            self.db.save(db, record)
        self.assertEqual(self.db.get("S")["qualityIncidentId"], incident)
        self.assertEqual(self.db.get("S")["assessmentPriority"], "urgent")
        with self.assertRaises(Conflict):
            self.disposition(version=old_version)
        with self.db.transaction() as db:
            record = self.db.read(db, "S")
            self.db.assessment_hold(db, record, "combined-transport-risk", {"eventId": "hot-rise-2", "occurredAt": self.at(15)}, "urgent", "rise-1")
            self.db.save(db, record)
        self.assertEqual(self.db.get("S")["evidenceVersion"], old_version + 1)

    def test_snapshot_configuration_does_not_reinterpret_existing_shipments(self):
        with patch.dict("os.environ", {"MONITORING_PROFILE_JSON": '{"thresholdC": 8}'}):
            self.create("C", "V-02")
        self.assertEqual(self.db.get("S")["monitoringProfile"]["thresholdC"], 5)
        self.assertEqual(self.db.get("C")["monitoringProfile"]["thresholdC"], 8)
        with self.assertRaises(ValueError):
            profile("PRD-001", {"MONITORING_PROFILE_JSON": '{"sampleSeconds": 0}'})

    def test_new_evaluation_gap_after_release_opens_new_incident(self):
        self.sample(0)
        self.ack(1, 40)
        first = self.db.get("S")["qualityIncidentId"]
        self.disposition()
        self.sample(5)
        self.ack(2, 5)
        self.sample(10)
        self.ack(3, 50)
        record = self.db.get("S")
        self.assertEqual(record["qualityStatus"], "PENDING")
        self.assertNotEqual(record["qualityIncidentId"], first)

    def test_future_reading_does_not_move_valid_reading_clock(self):
        self.sample(0)
        before = self.db.get("S")["monitoring"]["lastValidAt"]
        with self.assertRaises(ValueError):
            self.sample(700)
        self.assertEqual(self.db.get("S")["monitoring"]["lastValidAt"], before)

    def test_eta_duplicates_route_changes_and_unavailability_reset_qualification(self):
        with self.db.transaction() as db:
            record = self.db.read(db, "S")
            record.update(transportState="IN_TRANSIT", routeRevision=0)
            self.db.save(db, record)
        late = self.at(1000)
        for index in (1, 2, 1):
            self.db.advance_telemetry("S", [48.8566, 2.3522], index, late, "recorded-fixture")
        self.assertFalse(self.db.get("S")["etaAssessment"]["qualified"])
        self.db.advance_telemetry("S", [48.8566, 2.3522], 3, late, "recorded-fixture")
        self.assertTrue(self.db.get("S")["etaAssessment"]["qualified"])
        self.db.advance_telemetry("S", [48.8566, 2.3522], 4, None, "tomtom-unavailable")
        self.assertFalse(self.db.get("S")["etaAssessment"]["qualified"])
        self.db.advance_telemetry("S", [48.8566, 2.3522], 5, late, "recorded-fixture")
        self.assertFalse(self.db.get("S")["etaAssessment"]["qualified"])
        with self.db.transaction() as db:
            record = self.db.read(db, "S")
            record["routeRevision"] = 1
            self.db.save(db, record)
        self.db.advance_telemetry("S", [48.8566, 2.3522], 1, late, "recorded-fixture")
        self.assertFalse(self.db.get("S")["etaAssessment"]["qualified"])

    def test_timeout_disposition_covers_late_unavailable_evaluation_of_closing_sequence(self):
        self.sample(0)
        self.close(1)
        self.db.tick_monitoring(self.at(21))
        self.disposition()
        self.ack(1, 40)
        self.assertEqual(self.db.get("S")["qualityStatus"], "RELEASED")
