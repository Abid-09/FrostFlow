"""Offline P6 durability and compliance checks, with no Zeebe dependency."""

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "deployer")]

from compliance.check import replay
from registry import Conflict, Registry


MODEL = ROOT / "compliance/p6/rules.xml"
ACTIVITIES = [
    "ClaimableLossRecorded", "ClaimEvidenceAssembled", "SupplierClaimSubmitted",
    "SupplierResponseRecorded", "SupplierClaimClosed",
]


class SupplierClaims(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite"
        self.registry = Registry(self.path)
        self.loss = {
            "incidentId": "incident-1", "originalShipmentId": "shipment-1",
            "lossDescription": "One rejected crate", "actor": "operator",
            "evidenceRefs": ["inspection-1"],
        }
        record = {
            "shipmentId": "shipment-1", "journeyId": "journey-1",
            "qualityIncidentId": "incident-1",
        }
        catalog = self.registry.inventory()["products"][0]
        record.update(catalog)
        with self.registry.transaction() as db:
            self.registry.save(db, record)

    def test_requires_matching_incident_and_deduplicates_concurrent_requests(self):
        self.assertEqual(self.registry.events("shipment-1"), [])
        with self.assertRaises(Conflict):
            self.registry.accept_claim(dict(self.loss, incidentId="incorrect"))
        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = workers.map(self.registry.accept_claim, [self.loss, self.loss])
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.registry.accept_claim(dict(self.loss, lossDescription="different loss"))
        with self.registry.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_both_outcomes_and_guarded_retry_safe_closure(self):
        for index, outcome in enumerate(("SETTLED", "REJECTED"), 1):
            with self.subTest(outcome=outcome):
                shipment = f"shipment-{index}"
                with self.registry.transaction() as db:
                    record = self.registry.read(db, "shipment-1")
                    record.update(shipmentId=shipment, qualityIncidentId=f"incident-{index}")
                    self.registry.save(db, record)
                loss = dict(self.loss, incidentId=f"incident-{index}", originalShipmentId=shipment)
                claim_id = self.registry.accept_claim(loss)["claimId"]
                response = dict(outcome=outcome, actor="simulated supplier", reason="demo")
                with self.assertRaises(Conflict):
                    self.registry.accept_supplier_response(claim_id, response)
                self.registry.assemble_claim(claim_id, index)
                with self.assertRaises(Conflict):
                    self.registry.submit_claim(claim_id, index + 1)
                self.registry.submit_claim(claim_id, index)
                with self.assertRaises(Conflict):
                    self.registry.close_claim(claim_id, index)
                self.registry.accept_supplier_response(claim_id, response)
                self.registry = Registry(self.path)
                self.assertEqual(self.registry.close_claim(claim_id, index), outcome)
                self.registry.assemble_claim(claim_id, index)
                self.registry.submit_claim(claim_id, index)
                self.assertEqual(self.registry.close_claim(claim_id, index), outcome)
                self.assertEqual(self.registry.accept_supplier_response(claim_id, response)["status"], "CLOSED")
                with self.assertRaises(Conflict):
                    self.registry.accept_supplier_response(claim_id, dict(response, reason="changed"))
                events = self.registry.events(shipment)
                self.assertEqual([event["eventType"] for event in events], ACTIVITIES)
                self.assertTrue(all(event["processInstanceKey"] == index for event in events[1:]))

    def test_compliance_rejects_early_close_and_incomplete_claim(self):
        self.assertTrue(replay(MODEL, ACTIVITIES)["accepting"])
        self.assertFalse(replay(MODEL, ACTIVITIES[:3] + ACTIVITIES[-1:])["valid"])
        self.assertFalse(replay(MODEL, ACTIVITIES[:-1])["accepting"])
