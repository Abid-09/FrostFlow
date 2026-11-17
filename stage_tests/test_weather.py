import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deployer"))
from monitoring import profile
from weather import WeatherAdapter, eligible


class WeatherTests(unittest.TestCase):
    def test_cache_throttle_identity_and_eligibility(self):
        raw = json.loads(Path(__file__).with_name("fixtures").joinpath("open-meteo-current.json").read_text())
        calls = []
        adapter = WeatherAdapter(fetch=lambda *p: calls.append(p) or raw)
        first = adapter.current("J1", [48.8566, 2.3522], "2026-11-17T12:00:00Z")
        self.assertIsNone(adapter.current("J1", [48.8566, 2.3522], "2026-11-17T12:00:59Z"))
        cached = adapter.current("J2", [48.8566, 2.3522], "2026-11-17T12:01:00Z")
        self.assertEqual(cached["observationId"], first["observationId"])
        self.assertTrue(cached["cached"])
        self.assertEqual(len(calls), 1)
        refreshed = adapter.current("J1", [48.8566, 2.3522], "2026-11-17T12:10:00Z")
        self.assertEqual(refreshed["observationId"], first["observationId"])
        self.assertEqual(len(calls), 2)
        settings = profile("PRD-001", {})
        self.assertTrue(eligible(first, [48.8566, 2.3522], settings, "2026-11-17T12:30:00Z"))
        self.assertFalse(eligible(first, [48.8566, 2.3522], settings, "2026-11-17T12:30:01Z"))
        self.assertFalse(eligible(first, [49.9, 2.35], settings, "2026-11-17T12:01:00Z"))
        self.assertFalse(eligible(first, [48.8566, 2.3522], settings, "2026-11-17T11:59:59Z"))

    def test_failure_and_units_unavailable(self):
        adapter = WeatherAdapter(fetch=lambda *p: {})
        self.assertFalse(adapter.current("J", [48, 2])["available"])
        self.assertIsNone(adapter.current("J", [48, 2]))
