"""Check handover examples against real producers and the disposition boundary."""
import json
import tempfile
import unittest

from p4_contract_examples import AT, FIXTURES, produce
from registry import Conflict
from transport_integrations import validate_p4_acknowledgement


class P4Contract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry, self.examples = produce(self.temp.name)

    def test_examples_match_foundation_producer_and_duplicate_ack(self):
        self.assertEqual({p.name for p in FIXTURES.glob('*.json')}, set(self.examples))
        for name, expected in self.examples.items():
            with self.subTest(name=name):
                self.assertEqual(json.loads((FIXTURES / name).read_text()), expected)
        duplicate = self.examples['duplicate-delivery.json']
        self.assertEqual(duplicate['request'], self.examples['initial-assessment.json'])
        validate_p4_acknowledgement(duplicate['acknowledgement'], duplicate['idempotencyKey'])
        with self.assertRaises(ValueError):
            validate_p4_acknowledgement({'handoff': duplicate['request']['event']}, duplicate['idempotencyKey'])

    def test_new_evidence_rejects_stale_disposition_before_rejection(self):
        def check(registry, initial, urgent):
            event = initial['event']
            self.assertIn('monitoringProfile', registry.get('CONTRACT-001'))
            self.assertGreater(urgent['event']['assessment']['evidenceVersion'], event['assessment']['evidenceVersion'])
            with self.assertRaisesRegex(Conflict, 'current expectedEvidenceVersion'):
                registry.accept_quality_status('CONTRACT-001', 'stale-release', AT, 'RELEASED',
                                               event['incidentId'], event['assessment']['evidenceVersion'])
            self.assertEqual(registry.get('CONTRACT-001')['qualityStatus'], 'PENDING')
        with tempfile.TemporaryDirectory() as directory:
            produce(directory, before_rejection=check)

    def test_rejected_goods_keep_incident_and_cannot_be_released(self):
        fixture = self.examples['rejected-goods.json']
        event = fixture['subsequentAssessment']['event']
        self.assertEqual(event['incidentId'], fixture['disposition']['incidentId'])
        self.assertEqual(event['qualityStatus'], 'REJECTED')
        self.assertFalse(event['incidentOpened'])
        self.assertGreater(event['assessment']['evidenceVersion'], fixture['disposition']['expectedEvidenceVersion'])
        with self.assertRaisesRegex(Conflict, 'cannot change from REJECTED'):
            self.registry.accept_quality_status('CONTRACT-001', 'forbidden-release', AT, 'RELEASED',
                                                event['incidentId'], event['assessment']['evidenceVersion'])
