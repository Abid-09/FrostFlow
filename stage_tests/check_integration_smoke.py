"""Smoke-test normal admission and quality rejection on an existing default stack.

Unlike the fresh-data acceptance suites, this checks inventory deltas and can
run after those suites. Requires two free vehicles, one salad crate and one
chicken crate, the simulator enabled, API port 5001 and Elasticsearch port 9200.
"""
import json
import subprocess
import uuid
from pathlib import Path

from check_p3_compliance import http, require, run_case, timestamp, wait_for
from check_stage1 import engine_records


def main():
    base = "http://localhost:5001"
    before = require(http(base, "/stock"), 200, "warehouse before smoke")
    receiving_before = require(http(base, "/receiving-stock"), 200, "receiving before smoke")
    run_id = "smoke-" + uuid.uuid4().hex[:8]
    definitions = [
        ("admission", "PRD-005", "V-01", None, "StockAdmitted"),
        ("quality-rejection", "PRD-004", "V-02", "REJECTED", "GoodsRejected"),
    ]
    cases = []
    for scenario, product, vehicle, disposition, end in definitions:
        case = run_case(base, run_id, scenario, product, vehicle, disposition)
        wait_for("P3 " + end + " engine completion", lambda: engine_records(
            case["p3ProcessInstanceKey"], end, "ELEMENT_COMPLETED"))
        wait_for("P2 engine completion", lambda: engine_records(
            case["p2ProcessInstanceKey"], "TransportCompleted", "ELEMENT_COMPLETED"))
        cases.append(case)
    after = require(http(base, "/stock"), 200, "warehouse after smoke")
    receiving_after = require(http(base, "/receiving-stock"), 200, "receiving after smoke")
    assert after["available"] == before["available"] - 2
    assert receiving_after["available"] == receiving_before["available"] + 1
    for initial, final in zip(before["products"], after["products"], strict=True):
        assert final["productId"] == initial["productId"]
        delta = 1 if initial["productId"] in ("PRD-004", "PRD-005") else 0
        assert final["available"] == initial["available"] - delta
    path = Path("runtime/verification-results/main-smoke.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "passed", "checkedAt": timestamp(),
        "testedCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "command": ".venv/bin/python stage_tests/check_integration_smoke.py",
        "warehouseBefore": before, "warehouseAfter": after,
        "receivingBefore": receiving_before, "receivingAfter": receiving_after,
        "cases": cases,
    }, indent=2) + "\n")
    print("PASS: merged-main admission and hold/rejection, engine end events, exact inventory deltas; " + str(path))


if __name__ == "__main__":
    main()
