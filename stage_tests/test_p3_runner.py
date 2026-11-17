"""Regression for the asynchronous loading command gate in the live runner."""
import unittest
from unittest.mock import patch

from stage_tests import check_p3_compliance as runner


class LoadingReadiness(unittest.TestCase):
    def test_verification_waits_for_loading_and_command_completion(self):
        states = iter([
            {"state": "CREATED", "pendingCommand": None},
            {"state": "AWAITING_LOADING", "pendingCommand": {"name": "allocate"}},
            {"state": "AWAITING_LOADING", "pendingCommand": None},
        ])
        observed = []

        def http(base, path, data=None):
            if path == "/shipments":
                return 202, {}
            if path.endswith("/verify-loading"):
                self.assertEqual(len(observed), 3)
                raise StopIteration("readiness verified")
            current = next(states)
            observed.append(current)
            return 200, current

        with patch.object(runner, "http", side_effect=http), patch.object(runner.time, "sleep"):
            with self.assertRaisesRegex(StopIteration, "readiness verified"):
                runner.run_case("http://test", "run", "normal", "PRD-001", "V-01")

    def test_readiness_timeout_names_the_required_gate(self):
        with patch.object(runner.time, "monotonic", side_effect=[0, 181]):
            with self.assertRaisesRegex(RuntimeError, "AWAITING_LOADING.*pending command"):
                runner.wait_for("AWAITING_LOADING with no pending command", lambda: False)
