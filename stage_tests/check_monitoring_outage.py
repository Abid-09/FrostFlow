"""Live closure timeout check; configure SIDDIHI_CONTAINER_URL to an unavailable URL first.

Uses the existing matched stack and a new milk shipment. A labelled test release
covers the recorded missing-evaluation evidence; no P4 investigation runs.
"""
import json
import uuid
from pathlib import Path

from check_p3_compliance import http, require, run_case
from check_stage1 import engine_records
from check_p3_compliance import wait_for
from datetime import datetime


def main():
    case = run_case("http://localhost:5001", "outage-" + uuid.uuid4().hex[:8],
                    "evaluation-unavailable", "PRD-001", "V-01", "RELEASED", monitoring_scenario="normal")
    record = require(http("http://localhost:5001", f"/shipments/{case['shipmentId']}"), 200, "final record")
    assert record["monitoring"]["finalization"] == "HELD", record
    assert "monitoring-evaluation-unavailable" in record["assessmentReasons"], record
    by_type = {e["eventType"]: e for e in case["events"]}
    def time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    received = time(by_type["GoodsReceived"]["recordedAt"])
    released = time(by_type["VehicleReleased"]["recordedAt"])
    finalized = time(by_type["MonitoringFinalized"]["recordedAt"])
    assert finalized - received >= 20, (received, finalized)
    assert released < finalized, (released, finalized)
    wait_for("P3 engine end", lambda: engine_records(case["p3ProcessInstanceKey"], "StockAdmitted", "ELEMENT_COMPLETED"))
    case.update(final=record, vehicleReleasedBeforeFinalization=True,
                closureEvaluationWaitSeconds=finalized - received,
                inputModes=["unavailable-siddhi", "simulated-container", "local-demo-estimate", "test-disposition"])
    path = Path("runtime/foundation/monitoring-outage.json")
    path.write_text(json.dumps(case, indent=2) + "\n")
    print("PASS: unavailable Siddhi created closure hold; P2 released vehicle before finalization; current-version test release allowed admission", flush=True)


if __name__ == "__main__":
    main()
