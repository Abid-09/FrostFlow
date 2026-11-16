"""Exercise four P3 scenarios against a live Zeebe stack and capture real events.

Run against an isolated, fresh version-3 stack. The script creates four
shipments and writes the actual registry histories to runtime output.
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def http(base, path, data=None):
    request = Request(
        base + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.status, json.loads(body) if body else {}


def wait_for(description, operation, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = operation()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError("Timed out waiting for " + description)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require(response, expected, action):
    status, result = response
    if status != expected:
        raise RuntimeError(f"{action} returned HTTP {status}, expected {expected}: {result}")
    return result


def run_case(base, run_id, scenario, product_id, vehicle_id, hold_outcome=None, passed=True):
    shipment_id = f"p3comp-{run_id}-{scenario}"
    now = datetime.now(timezone.utc)
    data = {
        "shipmentId": shipment_id,
        "journeyId": f"journey-{shipment_id}",
        "vehicleId": vehicle_id,
        "recipientId": "C-01",
        "routeId": "route-01",
        "quantity": 1,
        "productId": product_id,
        "batchId": {
            "PRD-001": "DEMO-MILK-001",
            "PRD-002": "DEMO-YOGHURT-001",
            "PRD-003": "DEMO-CHEESE-001",
            "PRD-004": "DEMO-CHICKEN-001",
            "PRD-005": "DEMO-SALAD-001",
        }[product_id],
        "deliveryDeadline": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    }
    record = require(http(base, "/shipments", data), 202, "create shipment")
    url = f"/shipments/{shipment_id}"
    require(
        http(base, url + "/verify-loading", {
            "actor": "p3-compliance-check",
            "quantityChecked": True,
            "packagingChecked": True,
            "vehicleReady": True,
        }),
        202,
        "verify loading",
    )
    wait_for("loading verification", lambda: http(base, url)[1].get("state") == "AWAITING_DISPATCH")
    require(
        http(base, url + "/dispatch", {"actor": "p3-compliance-check"}),
        202,
        "dispatch",
    )
    wait_for("dispatch", lambda: http(base, url)[1].get("state") == "DISPATCHED")
    record = wait_for(
        "P3 arrival subscription",
        lambda: (
            current
            if (current := http(base, url)[1]).get("p3Status") == "AWAITING_RECEIPT"
            else None
        ),
    )

    accepted_integration_events = []
    if hold_outcome:
        pending_event_id = f"quality-pending-{shipment_id}"
        pending_occurred_at = timestamp()
        pending_payload = {
            "shipmentId": shipment_id,
            "eventId": pending_event_id,
            "occurredAt": pending_occurred_at,
            "qualityStatus": "PENDING",
            "incidentId": f"incident-{shipment_id}",
        }
        require(
            http(base, "/integrations/p4/quality-status", pending_payload),
            202,
            "open P4 quality incident",
        )
        accepted_integration_events.append({
            "eventId": pending_event_id,
            "eventType": "QualityStatusChanged",
            "shipmentId": shipment_id,
            "journeyId": record["journeyId"],
            "occurredAt": pending_occurred_at,
            "recordedAt": timestamp(),
            "source": "p4-quality",
            "inputMode": "test-integration",
            "actor": "p3-compliance-check",
            "process": "P4-test",
            "processInstanceKey": None,
            "payload": {
                "qualityStatus": "PENDING",
                "incidentId": pending_payload["incidentId"],
            },
        })

    require(
        http(base, url + "/receiving/receipt", {
            "eventId": f"receipt-{shipment_id}",
            "occurredAt": timestamp(),
            "recipientId": "C-01",
            "quantityReceived": 1,
            "receiver": "p3-compliance-check",
            "receiptReference": f"receipt-{shipment_id}",
        }),
        202,
        "confirm physical receipt",
    )
    wait_for("physical receipt", lambda: http(base, url)[1].get("p3Status") == "AWAITING_INSPECTION")
    require(
        http(base, url + "/receiving/inspection", {
            "eventId": f"inspection-{shipment_id}",
            "occurredAt": timestamp(),
            "inspector": "p3-compliance-check",
            "passed": passed,
        }),
        202,
        "submit inspection",
    )
    if hold_outcome:
        wait_for(
            "inspection hold",
            lambda: (
                current
                if (current := http(base, url)[1]).get("p3Status") == "ON_HOLD"
                else None
            ),
        )
        require(
            http(base, "/integrations/p4/quality-status", {
                "shipmentId": shipment_id,
                "eventId": f"quality-{hold_outcome.lower()}-{shipment_id}",
                "occurredAt": timestamp(),
                "qualityStatus": hold_outcome,
                "incidentId": f"incident-{shipment_id}",
            }),
            202,
            f"send P4 {hold_outcome.lower()} disposition",
        )
        final_status = "ADMITTED" if hold_outcome == "RELEASED" else "REJECTED"
    else:
        final_status = "ADMITTED" if passed else "REJECTED"
    final = wait_for(
        f"final P3 status {final_status}",
        lambda: (
            current
            if (current := http(base, url)[1]).get("p3Status") == final_status
            else None
        ),
    )
    if not final.get("vehicleReleased"):
        raise RuntimeError(f"P2 vehicle was not released after P3 physical receipt: {final}")
    events_status, events = http(base, url + "/events")
    require((events_status, events), 200, "read event history")
    process_keys = {
        "p1ProcessInstanceKey": record.get("processInstanceKey"),
        "p2ProcessInstanceKey": final.get("p2ProcessInstanceKey"),
        "p3ProcessInstanceKey": final.get("p3ProcessInstanceKey"),
    }
    if not all(process_keys.values()):
        raise RuntimeError(f"Scenario lacks P1/P2/P3 process instance keys: {process_keys}")
    return {
        "scenario": scenario,
        "shipmentId": shipment_id,
        **process_keys,
        "expectedFinalStatus": final_status,
        "events": events,
        "acceptedIntegrationEvents": accepted_integration_events,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:5011")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/p3-compliance/evidence.json")
    parser.add_argument("--resume", help="Continue a partial run using its recorded run ID")
    args = parser.parse_args()
    health_status, health = http(args.base, "/health")
    require((health_status, health), 200, "health check")
    if health.get("stage") != 3:
        raise RuntimeError(f"Expected the P1/P2/P3 service, got {health}")
    stock_status, stock = http(args.base, "/stock")
    require((stock_status, stock), 200, "read initial inventory")
    if stock.get("datasetVersion") != 3:
        raise RuntimeError("Use an isolated version-3 registry")
    run_id = args.resume or uuid.uuid4().hex[:8]
    definitions = [
        ("normal-receipt-admission", "PRD-001", None, True),
        ("quality-hold-release", "PRD-002", "RELEASED", True),
        ("inspection-rejection", "PRD-003", None, False),
        ("quality-rejection", "PRD-004", "REJECTED", True),
    ]
    cases = []
    if args.resume:
        if stock.get("shipmentCount") != 1:
            raise RuntimeError("--resume expects only the first scenario to exist")
        scenario, _, _, _ = definitions[0]
        shipment_id = f"p3comp-{run_id}-{scenario}"
        record_status, record = http(args.base, f"/shipments/{shipment_id}")
        require((record_status, record), 200, "read prior live scenario")
        if record.get("p3Status") != "ADMITTED":
            raise RuntimeError("The existing normal-receipt scenario is not admitted")
        events_status, events = http(args.base, f"/shipments/{shipment_id}/events")
        require((events_status, events), 200, "read prior event history")
        cases.append({
            "scenario": scenario,
            "shipmentId": shipment_id,
            "p1ProcessInstanceKey": record.get("processInstanceKey"),
            "p2ProcessInstanceKey": record.get("p2ProcessInstanceKey"),
            "p3ProcessInstanceKey": record.get("p3ProcessInstanceKey"),
            "expectedFinalStatus": "ADMITTED",
            "events": events,
            "acceptedIntegrationEvents": [],
        })
        definitions = definitions[1:]
    elif stock.get("shipmentCount") != 0:
        raise RuntimeError("Use a fresh isolated version-3 registry with no shipments")
    for index, (scenario, product, outcome, passed) in enumerate(definitions):
        vehicle = "V-01" if index % 2 == 0 else "V-02"
        cases.append(run_case(args.base, run_id, scenario, product, vehicle, outcome, passed))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "capturedAt": timestamp(),
        "baseUrl": args.base,
        "runId": run_id,
        "source": "live HTTP integration test and P1/P2/P3 durable registry backed by Zeebe",
        "cases": cases,
    }
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "passed",
        "runId": run_id,
        "shipments": [
            {
                "scenario": case["scenario"],
                "shipmentId": case["shipmentId"],
                "p1ProcessInstanceKey": case["p1ProcessInstanceKey"],
                "p2ProcessInstanceKey": case["p2ProcessInstanceKey"],
                "p3ProcessInstanceKey": case["p3ProcessInstanceKey"],
                "finalStatus": case["expectedFinalStatus"],
            }
            for case in cases
        ],
        "evidence": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
