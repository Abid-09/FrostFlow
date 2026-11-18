"""Live restart watchdog regression on one explicitly named communication container.

Stops only that service for 21 seconds, then starts it with its original matched
registry/engine dataset. Requires default-port stack, simulator on and a free
V-01 vehicle plus one salad crate. Never removes containers or volumes.
"""
import argparse
import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_p3_compliance import http, require, timestamp, wait_for
from check_stage1 import engine_records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--communication-container", required=True)
    args = parser.parse_args()
    base = "http://localhost:5001"
    sid = "restart-" + uuid.uuid4().hex[:8]
    url = "/shipments/" + sid
    def get():
        return require(http(base, url), 200, "shipment state")
    def post(path, data):
        return require(http(base, path, data), 202, path)
    post("/shipments", {"shipmentId": sid, "journeyId": "J-" + sid, "vehicleId": "V-01", "recipientId": "C-01",
                       "routeId": "route-01", "quantity": 1, "productId": "PRD-005", "batchId": "DEMO-SALAD-001",
                       "deliveryDeadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()})
    wait_for("loading readiness", lambda: get().get("state") == "AWAITING_LOADING" and not get().get("pendingCommand"))
    post(url + "/verify-loading", {"actor": "restart-check", "quantityChecked": True, "packagingChecked": True, "vehicleReady": True})
    before = wait_for("first container reading", lambda: r if (r := get()).get("monitoring", {}).get("sequence", 0) >= 1 else None)
    subprocess.run(["docker", "stop", args.communication_container], check=True)
    time.sleep(21)
    subprocess.run(["docker", "start", args.communication_container], check=True)
    def healthy():
        try:
            return http(base, "/health")[0] == 200
        except OSError:
            return False
    wait_for("communication restart", healthy)
    restored = wait_for("durable silence hold", lambda: r if (r := get()).get("qualityStatus") == "PENDING" else None)
    assert "container-telemetry-unavailable" in restored["assessmentReasons"], restored
    assert restored["monitoring"]["sessionId"] == before["monitoring"]["sessionId"]
    assert restored["monitoringProfile"] == before["monitoringProfile"]
    post(url + "/dispatch", {"actor": "restart-check"})
    wait_for("P3 receipt subscription", lambda: get().get("p3Status") == "AWAITING_RECEIPT")
    post(url + "/receiving/receipt", {"eventId": sid + "-receipt", "occurredAt": timestamp(), "recipientId": "C-01",
                                     "quantityReceived": 1, "receiver": "restart-check"})
    wait_for("inspection subscription", lambda: get().get("p3Status") == "AWAITING_INSPECTION")
    post(url + "/receiving/inspection", {"eventId": sid + "-inspection", "occurredAt": timestamp(),
                                        "inspector": "restart-check", "passed": True})
    held = wait_for("quality admission hold", lambda: r if (r := get()).get("p3Status") == "ON_HOLD" else None)
    assert held["vehicleReleased"], held
    post("/integrations/p4/quality-status", {"shipmentId": sid, "eventId": sid + "-test-reject", "occurredAt": timestamp(),
                                           "qualityStatus": "REJECTED", "incidentId": held["qualityIncidentId"],
                                           "expectedEvidenceVersion": held["evidenceVersion"]})
    final = wait_for("rejected receiving", lambda: r if (r := get()).get("p3Status") == "REJECTED" else None)
    wait_for("P3 engine end", lambda: engine_records(final["p3ProcessInstanceKey"], "GoodsRejected", "ELEMENT_COMPLETED"))
    events = require(http(base, url + "/events"), 200, "events")
    Path("runtime/foundation/restart-live.json").write_text(json.dumps({
        "status": "passed", "before": before, "restored": restored, "final": final, "events": events,
        "inputModes": ["service-restart", "simulated-container", "live-siddhi", "local-demo-estimate", "test-disposition"]}, indent=2) + "\n")
    print("PASS: restart retained session/profile and detected silence before resumed samples; admission held; receipt released vehicle", flush=True)


if __name__ == "__main__":
    main()
