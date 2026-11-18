"""Exercise four P4 investigations against a live stack and capture the real events.

Each scenario is a real shipment: P1 dispatches it, P2 accepts a delivery risk (hold +
durable P4 handoff), P4 opens a DCR case, P3 receives and inspects the goods and waits
on the hold, and the P4 investigation decides. Forbidden actions are attempted and must
be refused by DCR (HTTP 409). The scenarios mirror the four dcrgraphs.net simulation runs.

Prerequisites: fresh paired version-3 data, simulator on (the default), P3 receiving and
P4 enabled (Compose defaults). Example, from the repository root:

    .venv/bin/python stage_tests/check_p4_investigation.py --base http://localhost:5001
    .venv/bin/python compliance/p4/build_p4_evidence.py --live runtime/p4-compliance/evidence.json
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_p3_compliance import http, require, timestamp, wait_for  # noqa: E402

BATCHES = {
    "PRD-001": "DEMO-MILK-001",
    "PRD-002": "DEMO-YOGHURT-001",
    "PRD-003": "DEMO-CHEESE-001",
    "PRD-004": "DEMO-CHICKEN-001",
    "PRD-005": "DEMO-SALAD-001",
}

# (scenario, product, steps, expected P3 outcome). A step is an activity name,
# ("blocked", activity) for an action DCR must refuse, or "temperature-alert".
SCENARIOS = [
    ("run1-cooling-recovery-release", "PRD-001", [
        ("blocked", "AssessEvidence"),
        "ReviewWeatherData", "RequestCoolingAdjustment", "ReviewTemperatureLog",
        "RecordCoolingAcknowledgement", "RecordRecoveryEvidence", "AssessEvidence",
        "DecideRelease", "CloseInvestigation",
    ], "ADMITTED"),
    ("run2-cooling-failure-replace", "PRD-002", [
        "ReviewTemperatureLog", "RequestCoolingAdjustment", "RecordCoolingFailure",
        ("blocked", "RecordRecoveryEvidence"),
        "AssessEvidence", "DecideReplace", "CloseInvestigation",
    ], "REJECTED"),
    ("run3-additional-check-return", "PRD-003", [
        "ReviewRouteAndETA", "ReviewTemperatureLog", "RequestAdditionalCheck",
        ("blocked", "AssessEvidence"), ("blocked", "CloseInvestigation"),
        "CompleteAdditionalCheck", "AssessEvidence", "DecideReturn", "CloseInvestigation",
    ], "REJECTED"),
    ("run4-new-alert-reassessment-release", "PRD-005", [
        "ReviewTemperatureLog", "AssessEvidence", "temperature-alert",
        ("blocked", "DecideRelease"),
        "AssessEvidence", "DecideRelease", "CloseInvestigation",
    ], "ADMITTED"),
]
ACTORS = {"RecordCoolingAcknowledgement": "vehicle-simulator",
          "RecordCoolingFailure": "vehicle-simulator",
          "RecordRecoveryEvidence": "siddhi-recovery",
          "CompleteAdditionalCheck": "cafeteria-receiver"}


def dispatch(base, shipment_id, product_id, vehicle_id):
    now = datetime.now(timezone.utc)
    require(http(base, "/shipments", {
        "shipmentId": shipment_id, "journeyId": f"journey-{shipment_id}",
        "vehicleId": vehicle_id, "recipientId": "C-01", "routeId": "route-01",
        "quantity": 1, "productId": product_id, "batchId": BATCHES[product_id],
        "deliveryDeadline": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    }), 202, "create shipment")
    url = f"/shipments/{shipment_id}"
    wait_for("AWAITING_LOADING with no pending command", lambda: (
        (r := http(base, url)[1]).get("state") == "AWAITING_LOADING"
        and not r.get("pendingCommand")))
    require(http(base, url + "/verify-loading", {
        "actor": "p4-check", "quantityChecked": True, "packagingChecked": True,
        "vehicleReady": True}), 202, "verify loading")
    wait_for("loading verification", lambda: http(base, url)[1].get("state") == "AWAITING_DISPATCH")
    require(http(base, url + "/dispatch", {"actor": "p4-check"}), 202, "dispatch")
    return wait_for("journey start", lambda: (
        r if (r := http(base, url)[1]).get("transportState") == "IN_TRANSIT" else None))


def incident_path(incident_id, suffix=""):
    return f"/quality/incidents/{quote(incident_id, safe='')}{suffix}"


def run_case(base, run_id, index, scenario, product_id, steps, expected):
    shipment_id = f"p4comp-{run_id}-{index}"
    url = f"/shipments/{shipment_id}"
    record = dispatch(base, shipment_id, product_id, "V-01" if index % 2 else "V-02")
    require(http(base, "/integrations/siddhi/delivery-risk", {
        "shipmentId": shipment_id, "eventId": f"risk-{shipment_id}",
        "occurredAt": timestamp(), "deliveryDeadline": record["deliveryDeadline"],
        "eta": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace(
            "+00:00", "Z"),
        "reason": "P4 live check: late ETA", "estimatedDelaySeconds": 3600,
    }), 202, "accept delivery risk")
    incident_id = wait_for("quality hold placed by P2", lambda: (
        r.get("qualityIncidentId") if (r := http(base, url)[1]).get("qualityStatus")
        == "PENDING" else None))
    wait_for("P4 DCR case opened from the P2 handoff", lambda: (
        http(base, incident_path(incident_id))[1].get("status") == "OPEN"))
    refused = []
    blocked = http(base, incident_path(incident_id, "/activities/CloseInvestigation"),
                   {"actor": "quality-officer"})
    if blocked[0] != 409:
        raise RuntimeError(f"CloseInvestigation right after opening was not refused: {blocked}")
    refused.append({"activity": "CloseInvestigation", "status": 409,
                    "enabledAtThatTime": blocked[1].get("enabled")})

    wait_for("arrival and P3 receiving", lambda: http(base, url)[1].get("p3Status")
             == "AWAITING_RECEIPT", timeout=300)
    require(http(base, url + "/receiving/receipt", {
        "eventId": f"receipt-{shipment_id}", "occurredAt": timestamp(), "recipientId": "C-01",
        "quantityReceived": 1, "receiver": "cafeteria-receiver"}), 202, "physical receipt")
    wait_for("receipt", lambda: http(base, url)[1].get("p3Status") == "AWAITING_INSPECTION")
    require(http(base, url + "/receiving/inspection", {
        "eventId": f"inspection-{shipment_id}", "occurredAt": timestamp(),
        "inspector": "cafeteria-receiver", "passed": True}), 202, "inspection")
    wait_for("P3 admission waiting on the quality hold",
             lambda: http(base, url)[1].get("p3Status") == "ON_HOLD")

    alerts = 0
    for step in steps:
        if step == "temperature-alert":
            alerts += 1
            require(http(base, "/integrations/p4/temperature-excursion", {
                "eventId": f"temp-{shipment_id}-{alerts}", "shipmentId": shipment_id,
                "occurredAt": timestamp(), "source": "p4-check", "inputMode": "simulated",
                "payload": {"note": "simulated excursion while investigating"}}),
                202, "temperature excursion")
            wait_for("alert added as evidence", lambda: not http(
                base, incident_path(incident_id))[1].get("pendingSystemEvents"))
            continue
        if isinstance(step, tuple):
            status, body = http(base, incident_path(incident_id, f"/activities/{step[1]}"),
                                {"actor": "quality-officer", "reason": "should be refused"})
            if status != 409:
                raise RuntimeError(f"{step[1]} was not refused by DCR: {status} {body}")
            refused.append({"activity": step[1], "status": status,
                            "enabledAtThatTime": body.get("enabled")})
            continue
        data = {"actor": ACTORS.get(step, "quality-officer"),
                "eventId": f"{shipment_id}-{step}-{len(refused)}-{alerts}"}
        if step.startswith("Decide"):
            data["reason"] = f"{scenario}: evidence assessed"
        require(http(base, incident_path(incident_id, f"/activities/{step}"), data),
                200, f"execute {step}")

    final = wait_for(f"P3 {expected}", lambda: (
        r if (r := http(base, url)[1]).get("p3Status") == expected else None))
    investigation = require(http(base, incident_path(incident_id)), 200, "read incident")
    if investigation["status"] != "CLOSED" or not investigation.get("dispositionApplied"):
        raise RuntimeError(f"investigation not closed and applied: {investigation}")
    events = require(http(base, url + "/events"), 200, "read event history")
    return {
        "scenario": scenario, "shipmentId": shipment_id, "incidentId": incident_id,
        "dcrMode": investigation["dcrMode"], "dcrCaseId": investigation["dcrCaseId"],
        "decision": investigation["decision"], "qualityStatus": final["qualityStatus"],
        "p3Status": final["p3Status"], "refusedActions": refused,
        "processInstanceKeys": {k: final.get(k) for k in (
            "processInstanceKey", "p2ProcessInstanceKey", "p3ProcessInstanceKey")},
        "events": events,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://localhost:5001")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "runtime/p4-compliance/evidence.json")
    args = parser.parse_args()
    health = require(http(args.base, "/health"), 200, "health check")
    if health.get("stage") != 3 or "p4DcrEngine" not in health.get("integrations", {}):
        raise RuntimeError(f"Expected the service with P4 enabled, got {health}")
    if health["integrations"].get("p4IncidentHandoff") != "enabled":
        raise RuntimeError("P4_INCIDENT_URL is not set; the P2 handoff would stay queued")
    stock = require(http(args.base, "/stock"), 200, "read inventory")
    if stock.get("datasetVersion") != 3 or stock.get("shipmentCount") != 0:
        raise RuntimeError("Use a fresh isolated version-3 registry with no shipments")
    run_id = uuid.uuid4().hex[:8]
    cases = [run_case(args.base, run_id, i, *definition)
             for i, definition in enumerate(SCENARIOS, start=1)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "capturedAt": timestamp(), "baseUrl": args.base, "runId": run_id,
        "health": health, "source": "live HTTP run; P1/P2/P3 in Zeebe; P4 in "
        f"{health['integrations']['p4DcrEngine']}", "cases": cases,
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "runId": run_id, "evidence": str(args.output),
                      "cases": [{k: c[k] for k in ("scenario", "shipmentId", "incidentId",
                                                   "qualityStatus", "p3Status")}
                                for c in cases]}, indent=2))


if __name__ == "__main__":
    main()
