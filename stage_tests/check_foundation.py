"""Live P1–P3 container scenarios on an existing healthy default-port stack.

Requires simulator on, free vehicles, normal container evaluation via Siddhi,
and four milk crates. Uses new shipments; preserves existing stock and evidence.
"""
import json
import subprocess
import uuid
from pathlib import Path

from check_p3_compliance import http, require, run_case, timestamp, wait_for
from check_stage1 import engine_records


def main():
    base = "http://localhost:5001"
    before = require(http(base, "/stock"), 200, "warehouse before")
    stock_before = require(http(base, "/receiving-stock"), 200, "receiving before")
    cases = []
    run_id = "foundation-" + uuid.uuid4().hex[:8]
    for scenario, disposition in (("normal", None), ("isolated-spike", None),
                                   ("sustained-excursion", "RELEASED"), ("missing-telemetry", "REJECTED")):
        case = run_case(base, run_id, scenario, "PRD-001", "V-01", disposition,
                        monitoring_scenario=scenario)
        sid = case["shipmentId"]
        final = require(http(base, f"/shipments/{sid}"), 200, "final record")
        if disposition:
            reason = "temperature-excursion" if scenario == "sustained-excursion" else "container-telemetry-unavailable"
            assert reason in final["assessmentReasons"], final
        else:
            assert final["qualityStatus"] == "CLEAR", final
        assert final["monitoring"]["state"] == "CLOSED", final
        assert final["monitoring"]["finalization"] in ("FINALIZED", "HELD"), final
        end = "GoodsRejected" if disposition == "REJECTED" else "StockAdmitted"
        wait_for("P3 engine end", lambda: engine_records(case["p3ProcessInstanceKey"], end, "ELEMENT_COMPLETED"))
        wait_for("P2 engine end", lambda: engine_records(case["p2ProcessInstanceKey"], "TransportCompleted", "ELEMENT_COMPLETED"))
        case["final"] = final
        case["observations"] = require(http(base, f"/shipments/{sid}/monitoring/observations"), 200, "observations")
        cases.append(case)
        print("PASS", scenario, flush=True)
    after = require(http(base, "/stock"), 200, "warehouse after")
    stock_after = require(http(base, "/receiving-stock"), 200, "receiving after")
    assert after["available"] == before["available"] - 4
    assert stock_after["available"] == stock_before["available"] + 3
    path = Path("runtime/foundation/container-live.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "passed", "capturedAt": timestamp(), "runId": run_id,
                               "testedCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                               "inputModes": ["simulated-container", "live-siddhi", "local-demo-estimate", "live-open-meteo", "test-disposition"],
                               "warehouseBefore": before, "warehouseAfter": after,
                               "receivingBefore": stock_before, "receivingAfter": stock_after, "cases": cases}, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
