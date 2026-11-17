"""Live P2 acceptance check through the local Siddhi app.

Run against a fresh dataset with the full Compose stack (including the
``siddhi`` service) and a short GPS tick, for example GPS_TICK_SECONDS=10.

Two shipments travel at the same time:

* A has a one-minute deadline. Only after THREE consecutive fresh late ETAs may
  Siddhi raise a SIDDHI:RISK alert; accepting it must establish a quality hold.
  An approved route change must be acknowledged through Siddhi, keep the same
  destination, and be visible in Camunda's exported variables (what Operate
  shows) exactly as in the shipment API. A must arrive at its original destination.
* B has a generous deadline and must complete without any risk alert.
* C gets an alert nobody answers: arrival closes it and a late decision is refused.

After receipt, every shipment is inspected: B is admitted, while A and C stay
ON_HOLD until their CURRENT quality incident is released (P4 stand-in); a
release naming any other incident is refused.

Arrival starts P3. Both must stay ARRIVED (vehicle reserved) while P2's manual
receipt input is refused, until P3 records the receipt; P3's acknowledgement then
completes P2 and releases the vehicle. Evidence is written to
runtime/verification-results/p2-siddhi.json.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://localhost:5001"
EVIDENCE = Path("runtime/verification-results/p2-siddhi.json")
ROUTES = ("route-01", "route-02", "route-03")


def utc(delta=timedelta()):
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def http(path, data=None):
    request = Request(
        BASE + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}


def get(shipment_id):
    status, record = http(f"/shipments/{shipment_id}")
    assert status == 200, record
    return record


def events(shipment_id):
    status, result = http(f"/shipments/{shipment_id}/events")
    assert status == 200, result
    return result


def post(path, data):
    status, result = http(path, data)
    assert status == 202, f"{path} -> HTTP {status}: {result}"
    return result


def wait(description, predicate, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}")


def prepare(shipment_id, vehicle, recipient, route, product, deadline):
    post("/shipments", dict(
        shipmentId=shipment_id, journeyId="journey-" + shipment_id,
        vehicleId=vehicle, recipientId=recipient, routeId=route, quantity=5,
        productId=product["productId"], batchId=product["batchId"],
        deliveryDeadline=deadline,
    ))
    wait(f"{shipment_id} loading", lambda: get(shipment_id)["state"] == "AWAITING_LOADING")
    post(f"/shipments/{shipment_id}/verify-loading", dict(
        actor="warehouse-check", quantityChecked=True,
        packagingChecked=True, vehicleReady=True,
    ))
    wait(f"{shipment_id} dispatch-ready",
         lambda: get(shipment_id)["state"] == "AWAITING_DISPATCH")
    post(f"/shipments/{shipment_id}/dispatch", dict(actor="dispatcher-check"))
    wait(f"{shipment_id} handoff",
         lambda: get(shipment_id).get("handoffStatus") == "ACKNOWLEDGED")


def acknowledge_receipt(record):
    """Receipt goes through P3; P2's manual receipt input must be refused."""
    shipment_id = record["shipmentId"]
    wait(f"{shipment_id} P3 started by arrival",
         lambda: get(shipment_id).get("p3Status") == "AWAITING_RECEIPT")
    status, refused = http(f"/shipments/{shipment_id}/acknowledge-receipt", dict(
        eventId=f"check-manual-receipt-{shipment_id}", occurredAt=utc(),
        recipientId=record["recipientId"], quantityReceived=record["quantity"],
    ))
    assert status == 409, f"manual P2 receipt must be refused while P3 is on: {status} {refused}"
    assert get(shipment_id)["vehicleReleased"] is False
    post(f"/shipments/{shipment_id}/receiving/receipt", dict(
        eventId=f"check-receipt-{shipment_id}", occurredAt=utc(),
        recipientId=record["recipientId"], quantityReceived=record["quantity"],
        receiver="check-receiver", receiptReference=f"CHECK-{shipment_id}",
    ))
    completed = wait(
        f"{shipment_id} completion",
        lambda: (lambda r: r if r.get("transportState") == "COMPLETED"
                 and r.get("vehicleReleased") is True else None)(get(shipment_id)),
    )
    order = [e["eventType"] for e in events(shipment_id)]
    assert order.index("GoodsReceived") < order.index("ReceiptAcknowledged") < order.index("VehicleReleased"), order
    return completed


def operate_variable(process_instance_key, name):
    """Latest value of a process variable as exported by Zeebe (what Operate shows)."""
    request = Request(
        "http://localhost:9200/zeebe-record_variable*/_search",
        data=json.dumps({
            "query": {"bool": {"filter": [
                {"term": {"value.processInstanceKey": int(process_instance_key)}},
                {"term": {"value.name": name}},
            ]}},
            "sort": [{"position": "desc"}], "size": 1,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            hits = json.loads(response.read())["hits"]["hits"]
    except HTTPError:
        return None
    return json.loads(hits[0]["_source"]["value"]["value"]) if hits else None


def inspect_goods(shipment_id):
    post(f"/shipments/{shipment_id}/receiving/inspection", dict(
        eventId=f"check-inspection-{shipment_id}", occurredAt=utc(),
        inspector="check-inspector", passed=True,
    ))
    return wait(f"{shipment_id} admission decision", lambda: (
        lambda r: r if r.get("p3Status") in ("ADMITTED", "ON_HOLD", "REJECTED") else None
    )(get(shipment_id)))


def main():
    status, health = http("/health")
    assert status == 200 and health.get("status") == "ready", health
    integrations = health.get("integrations", {})
    assert integrations.get("siddhiTelemetry") == "enabled", integrations
    assert integrations.get("siddhiCommands") == "enabled", integrations
    assert integrations.get("gps") == "scripted-demo", integrations
    assert integrations.get("p3ArrivalNotification") == "enabled", integrations
    status, stock = http("/stock")
    assert stock.get("datasetVersion") == 3 and stock.get("shipmentCount") == 0, (
        "use a fresh dataset; see docs/RUNNING.md"
    )
    products = stock["products"]
    run = datetime.now(timezone.utc).strftime("%H%M%S")
    a, b = f"S-SID-A-{run}", f"S-SID-B-{run}"

    prepare(a, "V-01", "C-01", "route-01", products[0], utc(timedelta(minutes=1)))
    prepare(b, "V-02", "C-02", "route-02", products[1], utc(timedelta(minutes=60)))
    print(f"Dispatched {a} (short deadline) and {b} (generous deadline)")

    # 1. Siddhi detects A's late ETA and P2 waits for a dispatcher decision.
    destination = wait("A journey started", lambda: get(a).get("routeCoordinates"))[-1]
    risk_id = wait("Siddhi risk alert for A", lambda: get(a).get("pendingDeliveryRisk"))
    assert risk_id.startswith("SIDDHI:RISK:P2:" + a + ":ETAUpdated:0:"), risk_id
    # Persistence rule: the alert comes from the third (or later) consecutive late ETA.
    trigger_index = int(risk_id.rsplit(":", 1)[1])
    assert trigger_index >= 3, f"alert raised before three late ETAs: {risk_id}"
    late_before = [e for e in events(a) if e["eventType"] == "ETAUpdated"
                   and e["eventId"].startswith(f"P2:{a}:ETAUpdated:0:")
                   and int(e["eventId"].rsplit(":", 1)[1]) <= trigger_index
                   and e["payload"]["etaMillis"] > e["payload"]["deliveryDeadlineMillis"]]
    assert len(late_before) >= 3, late_before
    held = get(a)
    assert held["qualityStatus"] == "PENDING" and held["qualityIncidentId"] == "DELAY:" + risk_id, held
    wait("P2 DeliveryRiskDetected activity", lambda: any(
        e["eventType"] == "DeliveryRiskDetected" and e.get("producerEventId") == risk_id
        for e in events(a)))
    print(f"Risk alert from Siddhi recorded by P2: {risk_id}")

    # 2. Approve an alternative route; the acknowledgement must come back through Siddhi.
    before = get(a)
    target = next(r for r in ROUTES if r != before["routeId"])
    decision_id = f"check-decision-{risk_id}"
    post(f"/shipments/{a}/dispatcher-decision", dict(
        eventId=decision_id, decision="APPROVE_ALTERNATIVE_ROUTE", routeId=target))
    ack_event = wait("RouteChangeAcknowledged", lambda: next(
        (e for e in events(a) if e["eventType"] == "RouteChangeAcknowledged"), None))
    ack = ack_event["payload"]["simulatorAcknowledgement"]
    assert ack.get("commandId") == decision_id and ack.get("simulatorAcknowledged") is True, ack
    assert "messageId" in ack and ack.get("mode") != "local-demo", (
        f"acknowledgement did not pass through Siddhi's http-service bridge: {ack}")
    changed = get(a)
    assert changed["routeId"] == target and changed.get("routeRevision") == 1, changed
    assert changed["routeCoordinates"][-1] == destination, "route change moved the destination"
    p2_key = changed["p2ProcessInstanceKey"]
    wait("Camunda route variables match the shipment API", lambda: (
        operate_variable(p2_key, "routeId") == target
        and operate_variable(p2_key, "routeRevision") == 1))
    print(f"Route change {before['routeId']} -> {target} acknowledged via Siddhi; "
          "same destination; Operate variables match")

    # 3. Later telemetry follows the new route revision.
    wait("GPS/ETA on the new route", lambda: (
        any(e["eventType"] == "GPSPositionUpdated" and e["eventId"].startswith(f"P2:{a}:GPSPositionUpdated:1:")
            and e["payload"]["routeId"] == target for e in events(a))
        and any(e["eventType"] == "ETAUpdated" and e["eventId"].startswith(f"P2:{a}:ETAUpdated:1:")
                and e["payload"]["routeId"] == target for e in events(a))))
    print("GPS and ETA updates now use the new route revision")

    # 4. Further late ETAs on the new revision may raise one more alert; accept the delay.
    accepted = []

    def arrived_a():
        record = get(a)
        pending = record.get("pendingDeliveryRisk")
        if pending and pending not in accepted:
            post(f"/shipments/{a}/dispatcher-decision", dict(
                eventId=f"check-decision-{pending}", decision="ACCEPT_DELAY"))
            accepted.append(pending)
        return record if record.get("transportState") == "ARRIVED" else None

    for shipment_id, check in ((a, arrived_a), (b, None)):
        record = wait(f"{shipment_id} automatic arrival", check or (
            lambda s=shipment_id: (lambda r: r if r.get("transportState") == "ARRIVED" else None)(get(s))),
            timeout=180)
        time.sleep(2)
        record = get(shipment_id)
        assert record["transportState"] == "ARRIVED" and record["vehicleReleased"] is False, (
            f"{shipment_id} must wait for receipt: {record}")
    arrived = get(a)
    assert arrived["currentPosition"] == destination, (arrived["currentPosition"], destination)
    arrival = next(e for e in events(a) if e["eventType"] == "ShipmentArrived")
    assert [arrival["payload"]["latitude"], arrival["payload"]["longitude"]] == destination, arrival
    print("Both shipments arrived automatically (A at its original destination) and are blocked until receipt")

    completed = {s: acknowledge_receipt(get(s)) for s in (a, b)}
    b_events = events(b)
    assert not any(e["eventType"] == "DeliveryRiskDetected" for e in b_events), "B raised a risk"
    print("Both transports completed after receipt; vehicles released")

    # 5. C: nobody decides before the truck reaches its destination. Arrival must
    #    close the alert, and a late decision must be refused (no route change).
    c = f"S-SID-C-{run}"
    prepare(c, "V-01", "C-03", "route-01", products[2], utc(timedelta(minutes=1)))
    wait("Siddhi risk alert for C", lambda: get(c).get("pendingDeliveryRisk"))
    arrived_c = wait("C arrival without a decision", lambda: (
        lambda r: r if r.get("transportState") == "ARRIVED" else None)(get(c)), timeout=180)
    c_types = [e["eventType"] for e in events(c)]
    assert "DeliveryRiskClosedAtArrival" in c_types, c_types
    assert "DispatcherDecisionRecorded" not in c_types and "RouteChangeAcknowledged" not in c_types, c_types
    assert arrived_c["routeId"] == "route-01" and not arrived_c.get("pendingDeliveryRisk"), arrived_c
    status, refused = http(f"/shipments/{c}/dispatcher-decision", dict(
        eventId="check-late-decision", decision="APPROVE_ALTERNATIVE_ROUTE", routeId="route-02"))
    assert status == 409, (status, refused)
    assert arrived_c["qualityStatus"] == "PENDING", arrived_c
    completed[c] = acknowledge_receipt(get(c))
    print("C: arrival closed the undecided alert; late decision refused (409)")

    # 6. Quality hold: risk shipments wait for their CURRENT incident's release.
    outcomes = {s: inspect_goods(s)["p3Status"] for s in (a, b, c)}
    assert outcomes == {a: "ON_HOLD", b: "ADMITTED", c: "ON_HOLD"}, outcomes
    releases = {}
    for s in (a, c):
        incident = get(s)["qualityIncidentId"]
        status, refused = http("/integrations/p4/quality-status", dict(
            shipmentId=s, eventId=f"check-stale-release-{s}", occurredAt=utc(),
            qualityStatus="RELEASED", incidentId="DELAY:some-older-incident"))
        assert status == 409, (status, refused)
        assert get(s)["p3Status"] == "ON_HOLD"
        post("/integrations/p4/quality-status", dict(
            shipmentId=s, eventId=f"check-release-{s}", occurredAt=utc(),
            qualityStatus="RELEASED", incidentId=incident))
        releases[s] = incident
        wait(f"{s} admitted after release", lambda s=s: get(s).get("p3Status") == "ADMITTED")
    print("Quality hold: B admitted; A and C held until their current incident was released")

    evidence = {
        "checkedAt": utc(),
        "health": health,
        "shipments": {s: {"record": completed[s], "events": events(s)} for s in completed},
        "riskAlertFromSiddhi": risk_id,
        "routeChange": {"from": before["routeId"], "to": target, "acknowledgement": ack},
        "additionalAcceptedDelays": accepted,
        "riskRule": {"triggerEtaIndex": trigger_index, "lateEtasBeforeAlert": len(late_before)},
        "destinationPreserved": destination,
        "operateRouteVariables": {"routeId": target, "routeRevision": 1},
        "qualityReleases": releases,
        "admission": outcomes,
    }
    for s in completed:
        evidence["shipments"][s]["record"] = get(s)
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2))
    print(f"PASS - evidence written to {EVIDENCE}")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print("FAIL:", error)
        sys.exit(1)
