"""Evaluate recorded evidence windows through the real local Siddhi HTTP service.

No engine/state fixture is shared with the application. This is isolated live
CEP verification, not a provider call or a P4 investigation.
"""
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deployer"))
from registry import Registry
from transport_integrations import post_json


def main():
    cases = []
    with tempfile.TemporaryDirectory() as directory:
        registry = Registry(Path(directory) / "registry.sqlite")
        for index, (name, values, outdoor, expected) in enumerate((
            ("hot-rising", [3, 3.3, 3.6], 32, "urgent"),
            ("cool-rising", [3, 3.3, 3.6], 20, None),
            ("unavailable-rising", [3, 3.3, 3.6], None, None),
            ("hot-excursion", [6, 6.3, 6.6], 32, "urgent"),
            ("cool-excursion", [6, 6.3, 6.6], 20, "normal"),
            ("isolated-spike", [3, 10, 3], 32, None),
        )):
            at = datetime.now(timezone.utc).replace(microsecond=0)
            registry.create(dict(shipmentId=name, journeyId="J-" + name, vehicleId="FIXTURE-" + str(index),
                                 productId="PRD-001", batchId="DEMO-MILK-001", quantity=1, recipientId="C-01",
                                 routeId="route-01", deliveryDeadline=at.isoformat()))
            with registry.transaction() as db:
                record = registry.read(db, name)
                registry.monitoring_start(record, (at - timedelta(seconds=10)).isoformat())
                record["etaAssessment"] = {"qualified": True, "late": True, "observedAt": at.isoformat(),
                                           "revision": 0, "inputMode": "recorded-fixture"}
                record["weather"] = {"available": outdoor is not None, "providerTime": at.isoformat(),
                                     "coordinates": [48.8566, 2.3522], "conditions": {"temperature_2m": outdoor},
                                     "inputMode": "recorded-fixture", "source": "model-response-replay"}
                registry.save(db, record)
            for i, value in enumerate(values):
                registry.sample_container(name, (at - timedelta(seconds=10 - i * 5)).isoformat(), value)
            acknowledgements = []
            for message in registry.pending_messages("container-siddhi"):
                if message["correlationKey"] != name:
                    continue
                response = post_json("http://localhost:8008/container", message["variables"])
                ack = response.get("event", response)
                registry.acknowledge_container(ack)
                registry.message_result(message["messageId"])
                acknowledgements.append(ack)
            record = registry.get(name)
            assert record.get("assessmentPriority") == expected, (name, record)
            assert record["monitoring"]["evaluatedThrough"] == 3, record
            cases.append({"scenario": name, "expectedPriority": expected,
                          "reasons": record.get("assessmentReasons"), "acknowledgements": acknowledgements})
            print("PASS", name, flush=True)
    path = Path("runtime/foundation/siddhi-container.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "passed", "testedCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                               "inputModes": ["recorded-fixture-weather", "recorded-fixture-ETA", "simulated-container", "live-siddhi"],
                               "cases": cases}, indent=2) + "\n")


if __name__ == "__main__":
    main()
