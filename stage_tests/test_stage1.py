"""Offline API/business-effect checks. Engine calls are substituted."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "deployer")]
IMPORT_DIR = tempfile.TemporaryDirectory()
os.environ["REGISTRY_PATH"] = str(Path(IMPORT_DIR.name) / "import.sqlite")
import app as service
from registry import Conflict, Registry
from pyzeebe.grpc_internals.types import CreateProcessInstanceResponse
from compliance.check import Graph, replay, export_xes

MODEL = ROOT / "compliance/p1/rules.xml"


def sample(
    shipment="S-test",
    journey="J-test",
    vehicle="V-01",
    quantity=3,
    product="PRD-001",
    batch="DEMO-MILK-001",
):
    return dict(
        shipmentId=shipment,
        journeyId=journey,
        vehicleId=vehicle,
        recipientId="C-01",
        routeId="route-01",
        quantity=quantity,
        productId=product,
        batchId=batch,
        deliveryDeadline="2026-12-01T12:00:00Z",
    )


class Preparation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        service.registry = Registry(Path(self.temp.name) / "registry.sqlite")
        self.db = service.registry
        self.http = service.app.test_client()
        self.key = 123456789
        service.ready.set()
        self.addCleanup(service.ready.clear)

        class Engine:
            def run_process(inner, *args, **kwargs):
                return CreateProcessInstanceResponse(
                    1, service.PROCESS_ID, 1, self.key, "<default>"
                )

        for target, replacement in [
            ("client", Engine()),
            ("engine_call", lambda value: value),
        ]:
            patcher = patch.object(service, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self, **kwargs):
        response = self.http.post("/shipments", json=sample(**kwargs))
        self.assertEqual(response.status_code, 202, response.json)
        # This original suite remains the legacy/unmonitored compatibility cohort.
        # test_monitoring exercises new shipment snapshots and monitoring gates.
        with self.db.transaction() as db:
            record = self.db.read(db, sample(**kwargs)["shipmentId"])
            record.pop("monitoringProfile", None)
            self.db.save(db, record)

    def available(self, product="PRD-001"):
        return next(
            p["available"]
            for p in self.db.inventory()["products"]
            if p["productId"] == product
        )

    def check(self, sid="S-test", job=1):
        return self.db.check_stock(sid, self.key, job)

    def loading_wait(self):
        self.create()
        self.check()
        self.db.advance(
            "S-test", self.key, "STOCK_RESERVED", "STOCK_ALLOCATED", "StockAllocated"
        )
        self.db.advance(
            "S-test",
            self.key,
            "STOCK_ALLOCATED",
            "AWAITING_LOADING",
            "TransportAssigned",
        )

    def wait_shortage(self):
        self.create(quantity=15)
        self.check()
        self.create(shipment="S-two", journey="J-two", vehicle="V-02", quantity=10)
        self.assertFalse(self.check("S-two", 2)["stockAvailable"])

    def verify(self, sid="S-test"):
        return self.http.post(
            f"/shipments/{sid}/verify-loading",
            json=dict(
                actor="warehouse",
                quantityChecked=True,
                packagingChecked=True,
                vehicleReady=True,
            ),
        )

    def start_p2_transport(self):
        self.loading_wait()
        self.verify()
        self.db.advance(
            "S-test", self.key, "AWAITING_LOADING", "AWAITING_DISPATCH", "LoadingVerified"
        )
        self.db.reserve_command("S-test", "DispatchApproved", "dispatcher")
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        dispatch = next(
            event for event in self.db.events("S-test")
            if event["eventType"] == "ShipmentDispatched"
        )
        self.db.start_transport("S-test", 999, dispatch)
        points = service.transport_integrations.route("route-01", "C-01")
        eta, source = service.transport_integrations.eta(points, "route-01", 0)
        self.db.start_journey("S-test", 999, points[0], eta, source, points)

    def test_claim_accepts_long_foundation_incident_id(self):
        shipment = "S" * 31
        journey = "journey-" + shipment
        incident = f"ASSESS:{shipment}:CONTAINER:{shipment}:{journey}:7"
        self.assertGreater(len(incident), 100)
        product = self.db.inventory()["products"][0]
        with self.db.transaction() as db:
            self.db.save(db, dict(product, shipmentId=shipment, journeyId=journey,
                                  qualityIncidentId=incident))
        loss = dict(originalShipmentId=shipment, incidentId=incident,
                    lossDescription="One rejected crate", actor="operator",
                    evidenceRefs=["inspection-1"])
        result = self.http.post("/claims", json=loss)
        self.assertEqual(result.status_code, 202, result.json)
        claim = self.http.get("/claims/" + result.json["claimId"])
        self.assertEqual(claim.json["loss"]["incidentId"], incident)
        self.assertEqual(self.http.post("/claims", json=dict(loss, incidentId="x" * 1001)).status_code, 400)

    def test_dispatch_requires_processed_loading(self):
        self.loading_wait()

        def dispatch():
            return self.http.post(
                "/shipments/S-test/dispatch", json={"actor": "dispatcher"}
            )

        self.assertEqual(dispatch().status_code, 409)
        self.assertEqual(self.verify().status_code, 202)
        self.assertEqual(dispatch().status_code, 409)
        self.db.advance(
            "S-test",
            self.key,
            "AWAITING_LOADING",
            "AWAITING_DISPATCH",
            "LoadingVerified",
        )
        self.assertEqual(dispatch().status_code, 202)
        self.assertEqual(dispatch().status_code, 409)
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        self.assertEqual(dispatch().status_code, 409)
        events = self.db.events("S-test")
        self.assertTrue(replay(MODEL, events)["accepting"])
        handoffs = [
            m for m in self.db.pending_messages() if m["name"] == "ShipmentDispatched"
        ]
        self.assertEqual(len(handoffs), 1)
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        self.assertEqual(
            len(
                [
                    m
                    for m in self.db.pending_messages()
                    if m["name"] == "ShipmentDispatched"
                ]
            ),
            1,
        )

    def test_shortage_replenishment_recheck_and_deduplication(self):
        self.wait_shortage()
        with self.assertRaises(Conflict):
            self.db.advance(
                "S-two", self.key, "WAITING_STOCK", "STOCK_ALLOCATED", "StockAllocated"
            )
        self.assertEqual(self.verify("S-two").status_code, 409)
        self.assertEqual(
            self.http.post(
                "/shipments/S-two/dispatch", json={"actor": "dispatcher"}
            ).status_code,
            409,
        )
        old_key = self.db.get("S-two")["stockWaitKey"]
        payload = dict(actor="warehouse", quantity=1, eventId="replenish-1")
        for _ in range(2):
            self.assertEqual(
                self.http.post(
                    "/shipments/S-two/replenish-stock", json=payload
                ).status_code,
                202,
            )
        self.assertEqual(self.available(), 6)
        self.assertFalse(self.check("S-two", 3)["stockAvailable"])
        self.assertNotEqual(self.db.get("S-two")["stockWaitKey"], old_key)
        self.assertEqual(
            self.http.post(
                "/shipments/S-two/replenish-stock", json={**payload, "quantity": 2}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.http.post(
                "/shipments/S-two/replenish-stock",
                json={**payload, "eventId": "replenish-2", "quantity": 4},
            ).status_code,
            202,
        )
        self.assertTrue(self.check("S-two", 4)["stockAvailable"])
        self.assertEqual(self.available(), 0)
        self.assertEqual(
            Registry(self.db.path).check_stock("S-two", self.key, 4)["stockAvailable"],
            True,
        )
        self.assertEqual(self.available(), 0)

    def test_cancellation_only_while_waiting_releases_vehicle(self):
        self.wait_shortage()
        self.assertEqual(
            self.http.post(
                "/shipments/S-test/cancel", json={"actor": "dispatcher"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.http.post(
                "/shipments/S-two/cancel", json={"actor": "dispatcher"}
            ).status_code,
            202,
        )
        self.db.advance(
            "S-two", self.key, "WAITING_STOCK", "CANCELLED", "ShipmentCancelled"
        )
        self.assertEqual(
            self.http.post(
                "/shipments/S-two/dispatch", json={"actor": "dispatcher"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.http.post(
                "/shipments/S-two/cancel", json={"actor": "dispatcher"}
            ).status_code,
            409,
        )
        self.assertEqual(self.available(), 5)
        self.create(shipment="S-three", journey="J-three", vehicle="V-02")
        self.assertTrue(replay(MODEL, self.db.events("S-two"))["accepting"])

    def test_competing_replenishment_and_cancellation_have_one_winner(self):
        self.wait_shortage()

        def attempt(command):
            try:
                self.db.reserve_command(
                    "S-two",
                    command,
                    "operator",
                    ("supply-race", 10) if command == "StockReplenished" else None,
                )
                return "accepted"
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(
                list(
                    pool.map(
                        attempt, ["StockReplenished", "ShipmentCancellationRequested"]
                    )
                ),
                ["accepted", "conflict"],
            )

    def test_restart_preserves_pending_commands_and_reservations(self):
        self.loading_wait()
        self.verify()
        reopened = Registry(self.db.path)
        self.assertEqual(reopened.inventory()["available"], 97)
        self.assertEqual(
            reopened.get("S-test")["pendingCommand"]["name"], "LoadingVerified"
        )
        self.assertEqual(len(reopened.pending_messages()), 1)
        self.assertEqual(self.http.post("/shipments", json=sample()).status_code, 409)

    def test_transport_inbox_protects_duplicate_effects_and_ack_race(self):
        self.loading_wait()
        self.verify()
        self.db.advance(
            "S-test",
            self.key,
            "AWAITING_LOADING",
            "AWAITING_DISPATCH",
            "LoadingVerified",
        )
        self.db.reserve_command("S-test", "DispatchApproved", "dispatcher")
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        event = self.db.events("S-test")[-1]
        for _ in range(2):
            variables = self.db.start_transport("S-test", 999, event)
            self.assertEqual(variables["productId"], "PRD-001")
            self.assertEqual(variables["batchId"], "DEMO-MILK-001")
            self.assertEqual(variables["productName"], "Fresh milk")
        self.db.message_result(
            event["eventId"]
        )  # Publisher response must not overwrite a fast ACK.
        self.assertEqual(self.db.get("S-test")["handoffStatus"], "ACKNOWLEDGED")
        self.assertFalse(
            any(m["name"] == "ShipmentDispatched" for m in self.db.pending_messages())
        )
        self.assertEqual(
            sum(e["eventType"] == "TransportStarted" for e in self.db.events("S-test")),
            1,
        )
        with self.assertRaises(Conflict):
            self.db.start_transport("S-test", 1000, event)
        with self.assertRaises(Conflict):
            self.db.start_transport("S-test", 999, {**event, "shipmentId": "other"})

    def test_stock_and_worker_retry_do_not_double_reserve(self):
        self.create(quantity=15)
        self.check()
        self.check()
        self.assertEqual(self.available(), 5)
        self.db.advance(
            "S-test", self.key, "STOCK_RESERVED", "STOCK_ALLOCATED", "StockAllocated"
        )
        self.db.advance(
            "S-test", self.key, "STOCK_RESERVED", "STOCK_ALLOCATED", "StockAllocated"
        )
        self.assertEqual(len(self.db.events("S-test")), 3)
        with self.assertRaises(Conflict):
            self.db.attach_key("S-test", 999)

    def test_transport_registration_retry_preserves_later_progress(self):
        self.loading_wait()
        self.verify()
        self.db.advance(
            "S-test",
            self.key,
            "AWAITING_LOADING",
            "AWAITING_DISPATCH",
            "LoadingVerified",
        )
        self.db.reserve_command("S-test", "DispatchApproved", "dispatcher")
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        event = self.db.events("S-test")[-1]
        self.db.start_transport("S-test", 999, event)
        # Simulate a future P2 worker completing transport after registration.
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.update(transportState="COMPLETED", vehicleReleased=True)
            self.db.save(db, record)
        Registry(self.db.path).start_transport("S-test", 999, event)
        record = self.db.get("S-test")
        self.assertEqual(record["transportState"], "COMPLETED")
        self.assertTrue(record["vehicleReleased"])
        self.assertEqual(record["p2ProcessInstanceKey"], "999")
        self.assertEqual(
            sum(e["eventType"] == "TransportStarted" for e in self.db.events("S-test")),
            1,
        )

    def test_p2_manual_arrival_and_receipt_gate_vehicle_release(self):
        self.loading_wait()
        self.verify()
        self.db.advance(
            "S-test",
            self.key,
            "AWAITING_LOADING",
            "AWAITING_DISPATCH",
            "LoadingVerified",
        )
        self.db.reserve_command("S-test", "DispatchApproved", "dispatcher")
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        dispatch_event = next(
            event
            for event in self.db.events("S-test")
            if event["eventType"] == "ShipmentDispatched"
        )
        self.db.start_transport("S-test", 999, dispatch_event)
        self.db.start_journey("S-test", 999)
        self.assertEqual(self.db.get("S-test")["transportState"], "IN_TRANSIT")
        self.assertFalse(self.db.get("S-test")["vehicleReleased"])

        receipt = dict(
            eventId="receipt-1",
            occurredAt="2026-12-01T10:24:00Z",
            recipientId="C-01",
            quantityReceived=3,
        )
        self.assertEqual(
            self.http.post(
                "/shipments/S-test/acknowledge-receipt", json=receipt
            ).status_code,
            409,
        )

        arrival = dict(
            # This deliberately collides with the internal P2 vehicle-release
            # activity ID. External IDs must have their own journal namespace.
            eventId="P2:S-test:VehicleReleased",
            eventType="ShipmentArrived",
            shipmentId="S-test",
            journeyId="J-test",
            occurredAt="2026-12-01T10:22:00Z",
            source="p2-manual-test",
            inputMode="manual",
            payload={
                "recipientId": "C-01",
                "latitude": 48.8566,
                "longitude": 2.3522,
            },
        )
        for _ in range(2):
            response = self.http.post("/shipments/S-test/arrive", json=arrival)
            self.assertEqual(response.status_code, 202, response.json)
        self.assertEqual(
            self.http.post(
                "/shipments/S-test/arrive",
                json={**arrival, "payload": {**arrival["payload"], "recipientId": "C-02"}},
            ).status_code,
            409,
        )
        arrival_message = next(
            message
            for message in self.db.pending_messages()
            if message["name"] == "ShipmentArrived"
        )
        arrival_event = arrival_message["variables"]["arrivalEvent"]
        self.db.record_arrival("S-test", 999, arrival_event)
        self.db.record_arrival("S-test", 999, arrival_event)
        record = self.db.get("S-test")
        self.assertEqual(record["transportState"], "ARRIVED")
        self.assertFalse(record["vehicleReleased"])
        p3_notifications = self.db.pending_messages(destination="p3-http")
        self.assertEqual(len(p3_notifications), 1)
        self.assertNotEqual(p3_notifications[0]["messageId"], arrival_event["eventId"])
        self.assertEqual(
            p3_notifications[0]["variables"]["event"]["shipmentId"], "S-test"
        )
        p3_message_id = p3_notifications[0]["messageId"]
        self.db.message_result(p3_message_id, "receiver unavailable")
        self.assertEqual(
            len(self.db.pending_messages(destination="p3-http")), 1
        )
        self.db.message_result(p3_message_id)
        self.assertEqual(
            len(self.db.pending_messages(destination="p3-http")), 0
        )

        for _ in range(2):
            response = self.http.post(
                "/shipments/S-test/acknowledge-receipt", json=receipt
            )
            self.assertEqual(response.status_code, 202, response.json)
        receipt_message = next(
            message
            for message in self.db.pending_messages()
            if message["name"] == "ReceiptAcknowledged"
        )
        receipt_event = receipt_message["variables"]["receiptEvent"]
        self.db.release_vehicle("S-test", 999, receipt_event)
        record = self.db.get("S-test")
        self.assertEqual(record["transportState"], "COMPLETED")
        self.assertTrue(record["vehicleReleased"])
        self.db.release_vehicle("S-test", 999, receipt_event)
        events = self.db.events("S-test")
        arrival_record = next(e for e in events if e["eventType"] == "ShipmentArrived")
        self.assertEqual(
            arrival_record["producerEventId"], "P2:S-test:VehicleReleased"
        )
        self.assertIn("P2:external:ShipmentArrived:S-test:", arrival_record["eventId"])
        self.assertEqual(
            sum(event["eventType"] == "ShipmentArrived" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["eventType"] == "ReceiptAcknowledged" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["eventType"] == "VehicleReleased" for event in events),
            1,
        )
        self.create(shipment="S-next", journey="J-next", vehicle="V-01")

    def test_simulated_route_records_siddhi_telemetry_and_queues_arrival(self):
        self.start_p2_transport()
        points = service.transport_integrations.route("route-01", "C-01")
        for index in range(1, len(points)):
            eta, source = service.transport_integrations.eta(points[index:], "route-01", index)
            record = self.db.advance_telemetry("S-test", points[index], index, eta, source)
        self.assertEqual(record["currentPosition"], points[-1])
        self.assertEqual(record["etaSource"], "at-destination")
        self.assertEqual(
            len(self.db.pending_messages(destination="siddhi-http")), 2 * (len(points) - 1)
        )
        accepted = self.db.queue_transport_event(
            "S-test", "ShipmentArrived", "SIM:J-test:ShipmentArrived",
            record["lastGpsEventOccurredAt"],
            {"recipientId": "C-01", "latitude": points[-1][0], "longitude": points[-1][1]},
            source="p2-simulator", input_mode="simulated",
        )
        self.assertEqual(accepted["event"]["inputMode"], "simulated")
        self.assertEqual(accepted["event"]["journeyId"], "J-test")
        self.assertEqual(accepted["event"]["shipmentId"], "S-test")

    def test_unavailable_eta_is_recorded_without_deadline_comparison(self):
        self.start_p2_transport()
        points = service.transport_integrations.route("route-01", "C-01")
        record = self.db.advance_telemetry("S-test", points[1], 1, None, "tomtom-unavailable")
        self.assertIsNone(record["eta"])
        self.assertEqual(record["etaSource"], "tomtom-unavailable")
        eta_event = next(e for e in self.db.events("S-test") if e["eventType"] == "ETAUpdated")
        self.assertIsNone(eta_event["payload"]["etaMillis"])
        self.assertEqual(eta_event["payload"]["etaSource"], "tomtom-unavailable")

    def test_risk_decision_is_deduplicated_and_route_waits_for_ack(self):
        self.start_p2_transport()
        risk = {
            "shipmentId": "S-test", "eventId": "risk-1",
            "occurredAt": "2026-12-01T10:20:00Z", "eta": "2026-12-01T12:00:00Z",
            "deliveryDeadline": "2026-12-01T11:00:00Z", "reason": "traffic delay",
            "estimatedDelaySeconds": 3600,
        }
        for _ in range(2):
            self.assertEqual(self.http.post("/integrations/siddhi/delivery-risk", json=risk).status_code, 202)
        self.assertEqual(
            sum(item["name"] == "DeliveryRiskDetected" for item in self.db.pending_messages(destination="zeebe")),
            1,
        )
        self.assertEqual(
            self.http.post("/integrations/siddhi/delivery-risk", json={**risk, "eventId": "risk-2"}).status_code,
            409,
        )
        risk_message = next(
            item for item in self.db.pending_messages(destination="zeebe")
            if item["name"] == "DeliveryRiskDetected"
        )
        risk_event = risk_message["variables"]["deliveryRiskEvent"]
        self.db.record_delivery_risk("S-test", 999, risk_event)
        decision = {"eventId": "decision-1", "decision": "APPROVE_ALTERNATIVE_ROUTE", "routeId": "route-03"}
        for _ in range(2):
            self.assertEqual(self.http.post("/shipments/S-test/dispatcher-decision", json=decision).status_code, 202)
        accepted = next(
            item for item in self.db.pending_messages(destination="zeebe")
            if item["name"] == "DispatcherDecisionRecorded"
        )["variables"]["dispatcherDecision"]
        with self.assertRaises(Conflict):
            self.db.apply_dispatcher_decision("S-test", 999, accepted,
                                               {"commandId": "decision-1", "simulatorAcknowledged": False})
        points = service.transport_integrations.route(
            "route-03", "C-01", start=self.db.get("S-test")["currentPosition"])
        eta, source = service.transport_integrations.eta(points, "route-03", 0)
        ack = service.transport_integrations.route_command({"commandId": "decision-1"})
        record = self.db.apply_dispatcher_decision("S-test", 999, accepted, ack, points, eta, source)
        self.assertEqual(record["routeId"], "route-03")
        self.assertEqual(
            sum(event["eventType"] == "RouteChangeAcknowledged" for event in self.db.events("S-test")),
            1,
        )
        self.assertEqual(record["routeCoordinates"][0], record["currentPosition"])
        self.assertIsNone(record.get("pendingDeliveryRisk"))
        next_position = record["routeCoordinates"][1]
        next_eta, next_source = service.transport_integrations.eta(
            record["routeCoordinates"][1:], "route-03", 1
        )
        self.db.advance_telemetry("S-test", next_position, 1, next_eta, next_source)
        self.assertTrue(
            any(event["eventType"] == "RouteChangeAcknowledged" for event in self.db.events("S-test"))
        )
        self.assertEqual(
            self.db.get("S-test")["currentPosition"], next_position
        )
        self.assertEqual(self.http.post("/shipments/S-test/dispatcher-decision", json=decision).status_code, 202)

    def risk_at_index(self, index, event_id="SIDDHI:RISK:late-1"):
        """Move the scripted vehicle to `index`, then accept a Siddhi delivery risk."""
        record = self.db.get("S-test")
        points = record["routeCoordinates"]
        for i in range(record.get("routeIndex", 0) + 1, index + 1):
            eta, source = service.transport_integrations.eta(points[i:], record["routeId"], i)
            self.db.advance_telemetry("S-test", points[i], i, eta, source)
        response = self.http.post("/integrations/siddhi/delivery-risk", json={
            "shipmentId": "S-test", "eventId": event_id,
            "occurredAt": "2026-12-01T10:20:00Z", "eta": "2026-12-01T12:00:00Z",
            "deliveryDeadline": "2026-12-01T11:00:00Z", "reason": "late",
        })
        self.assertEqual(response.status_code, 202, response.json)
        risk = next(item for item in self.db.pending_messages(destination="zeebe")
                    if item["name"] == "DeliveryRiskDetected"
                    and item["variables"]["deliveryRiskEvent"]["eventId"] == event_id)
        self.db.record_delivery_risk("S-test", 999, risk["variables"]["deliveryRiskEvent"])
        return response.json

    def decide(self, event_id, decision, route_id=None):
        body = {"eventId": event_id, "decision": decision}
        if route_id:
            body["routeId"] = route_id
        response = self.http.post("/shipments/S-test/dispatcher-decision", json=body)
        self.assertEqual(response.status_code, 202, response.json)
        return next(item for item in self.db.pending_messages(destination="zeebe")
                    if item["name"] == "DispatcherDecisionRecorded"
                    and item["variables"]["dispatcherDecision"]["eventId"] == event_id
                    )["variables"]["dispatcherDecision"]

    def test_route_change_preserves_destination_and_arrival_happens_there(self):
        import asyncio
        from types import SimpleNamespace
        from transport_integrations import DESTINATIONS

        self.start_p2_transport()
        destination = self.db.get("S-test")["routeCoordinates"][-1]
        self.assertEqual(destination, DESTINATIONS["C-01"])
        self.risk_at_index(3)
        decision = self.decide("change-1", "APPROVE_ALTERNATIVE_ROUTE", "route-02")
        # The real worker plans from the current position, applies after the
        # acknowledgement and returns the updated route variables to Camunda.
        result = asyncio.run(service.apply_dispatcher_decision(
            "S-test", decision, SimpleNamespace(process_instance_key=999)))
        self.assertEqual(result, {"simulatorAcknowledged": True,
                                  "routeId": "route-02", "routeRevision": 1})
        record = self.db.get("S-test")
        self.assertEqual(record["routeId"], "route-02")
        self.assertEqual(record["routeCoordinates"][0], record["currentPosition"])
        self.assertEqual(record["routeCoordinates"][-1], destination)
        points = record["routeCoordinates"]
        for i in range(1, len(points)):
            eta, source = service.transport_integrations.eta(points[i:], "route-02", i)
            record = self.db.advance_telemetry("S-test", points[i], i, eta, source)
        self.assertEqual(record["currentPosition"], destination)
        self.assertEqual(record["etaSource"], "at-destination")
        arrival = self.db.queue_transport_event(
            "S-test", "ShipmentArrived", "SIM:J-test:ShipmentArrived",
            record["lastGpsEventOccurredAt"],
            {"recipientId": "C-01", "latitude": record["currentPosition"][0],
             "longitude": record["currentPosition"][1]},
            source="p2-simulator", input_mode="simulated")
        payload = arrival["event"]["payload"]
        self.assertEqual([payload["latitude"], payload["longitude"]], destination)

    def test_incompatible_routes_are_rejected(self):
        self.start_p2_transport()
        self.risk_at_index(2)
        unknown = self.http.post("/shipments/S-test/dispatcher-decision", json={
            "eventId": "bad-1", "decision": "APPROVE_ALTERNATIVE_ROUTE", "routeId": "route-09"})
        self.assertEqual(unknown.status_code, 400, unknown.json)
        # A stored route ending somewhere other than the recipient (e.g. a legacy
        # journey) must refuse any change that would end elsewhere.
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record["routeCoordinates"] = [*record["routeCoordinates"][:-1], [48.80, 2.20]]
            self.db.save(db, record)
        moved = self.http.post("/shipments/S-test/dispatcher-decision", json={
            "eventId": "bad-2", "decision": "APPROVE_ALTERNATIVE_ROUTE", "routeId": "route-02"})
        self.assertEqual(moved.status_code, 409, moved.json)
        self.assertIn("destination", moved.json["error"])
        command = self.http.post("/integrations/simulator/route-command", json={"event": {
            "commandId": "bad-3", "shipmentId": "S-test", "journeyId": "J-test",
            "riskEventId": "SIDDHI:RISK:late-1", "command": "ChangeRoute", "routeId": "route-02"}})
        self.assertEqual(command.status_code, 409, command.json)

    def test_delivery_risk_holds_quality_until_current_incident_release(self):
        self.start_p2_transport()
        self.risk_at_index(2)
        record = self.db.get("S-test")
        incident = record["qualityIncidentId"]
        self.assertEqual(record["qualityStatus"], "PENDING")
        self.assertEqual(incident, "DELAY:SIDDHI:RISK:late-1")
        hold = [e for e in self.db.events("S-test") if e["eventType"] == "QualityHoldEstablished"]
        self.assertEqual(len(hold), 1)
        self.assertEqual(hold[0]["process"], "P4")
        handoffs = self.db.pending_messages(destination="p4-http")
        self.assertEqual(len(handoffs), 1)  # durable: stays pending while P4 is absent
        self.assertEqual(handoffs[0]["variables"]["event"]["incidentId"], incident)
        # A later alert joins the active incident instead of opening a new one.
        decision = self.decide("delay-1", "ACCEPT_DELAY")
        self.db.apply_dispatcher_decision(
            "S-test", 999, decision, {"commandId": "delay-1", "simulatorAcknowledged": True})
        self.risk_at_index(3, "SIDDHI:RISK:late-2")
        self.assertEqual(self.db.get("S-test")["qualityIncidentId"], incident)
        self.assertEqual(len(self.db.pending_messages(destination="p4-http")), 2)
        # P3 admission is held while the incident is open.
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.update(p3ProcessInstanceKey="777", inspectionStatus="PASSED",
                          p3Status="AWAITING_ADMISSION")
            self.db.save(db, record)
        self.assertEqual(self.db.admit_p3_stock("S-test", 777), {"admissionOutcome": "HELD"})
        # A release for any other incident cannot clear the hold...
        with self.assertRaises(Conflict):
            self.db.accept_quality_status("S-test", "release-old", "2026-12-01T11:00:00Z",
                                          "RELEASED", "DELAY:older-incident")
        self.assertEqual(self.db.get("S-test")["qualityStatus"], "PENDING")
        # ...only releasing the current incident lets the stock in.
        self.db.accept_quality_status("S-test", "release-1", "2026-12-01T11:00:00Z",
                                      "RELEASED", incident)
        self.assertEqual(self.db.admit_p3_stock("S-test", 777), {"admissionOutcome": "ADMITTED"})

    def test_delivery_risk_preserves_terminal_quality_rejection(self):
        self.start_p2_transport()
        self.risk_at_index(2)
        incident = self.db.get("S-test")["qualityIncidentId"]
        self.db.accept_quality_status("S-test", "reject-1", "2026-12-01T11:00:00Z",
                                      "REJECTED", incident)
        decision = self.decide("delay-1", "ACCEPT_DELAY")
        self.db.apply_dispatcher_decision(
            "S-test", 999, decision, {"commandId": "delay-1", "simulatorAcknowledged": True})
        self.risk_at_index(3, "SIDDHI:RISK:late-2")
        record = self.db.get("S-test")
        self.assertEqual(record["qualityStatus"], "REJECTED")
        self.assertEqual(record["qualityIncidentId"], incident)
        handoff = self.db.pending_messages(destination="p4-http")[-1]["variables"]["event"]
        self.assertEqual(handoff["qualityStatus"], "REJECTED")
        self.assertEqual(handoff["incidentId"], incident)
        self.assertFalse(handoff["incidentOpened"])
        self.assertEqual(len([e for e in self.db.events("S-test")
                              if e["eventType"] == "QualityHoldEstablished"]), 1)
        with self.assertRaises(Conflict):
            self.db.accept_quality_status("S-test", "release-1", "2026-12-01T11:01:00Z",
                                          "RELEASED", incident)
        # Transport decisions remain possible after terminal quality rejection.
        decision = self.decide("delay-2", "ACCEPT_DELAY")
        self.db.apply_dispatcher_decision(
            "S-test", 999, decision, {"commandId": "delay-2", "simulatorAcknowledged": True})
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.update(p3ProcessInstanceKey="777", inspectionStatus="PASSED",
                          p3Status="AWAITING_ADMISSION")
            self.db.save(db, record)
        self.assertEqual(self.db.admit_p3_stock("S-test", 777), {"admissionOutcome": "REJECTED"})

    def test_simulator_accepts_siddhi_accept_delay_without_route(self):
        """Siddhi's JSON sink sends a null routeId as the string "undefined"."""
        self.start_p2_transport()
        self.assertEqual(self.http.post("/integrations/siddhi/delivery-risk", json={
            "event": {
                "shipmentId": "S-test", "eventId": "SIDDHI:RISK:eta-1",
                "occurredAt": "2026-12-01T10:20:00Z", "eta": "2026-12-01T12:00:00Z",
                "deliveryDeadline": "2026-12-01T11:00:00Z",
                "reason": "ETA exceeds delivery deadline", "estimatedDelaySeconds": 3600,
            },
        }).status_code, 202)
        command = {
            "messageId": "siddhi-message-1", "commandId": "decision-delay",
            "shipmentId": "S-test", "journeyId": "J-test",
            "riskEventId": "SIDDHI:RISK:eta-1", "command": "AcceptDelay",
        }
        for route_id in ("undefined", "undefined", ""):
            response = self.http.post("/integrations/simulator/route-command",
                                      json={"event": {**command, "routeId": route_id}})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["commandId"], "decision-delay")
            self.assertIs(response.json["simulatorAcknowledged"], True)
        rejected = self.http.post("/integrations/simulator/route-command",
                                  json={"event": {**command, "routeId": "route-02"}})
        self.assertIn(rejected.status_code, (400, 409))

    def test_arrival_during_pending_decision_closes_alert(self):
        """The truck reaches its destination before the dispatcher decides."""
        self.start_p2_transport()
        self.assertEqual(self.http.post("/integrations/siddhi/delivery-risk", json={
            "shipmentId": "S-test", "eventId": "SIDDHI:RISK:late-1",
            "occurredAt": "2026-12-01T10:20:00Z", "eta": "2026-12-01T12:00:00Z",
            "deliveryDeadline": "2026-12-01T11:00:00Z", "reason": "late",
        }).status_code, 202)
        risk = next(item for item in self.db.pending_messages(destination="zeebe")
                    if item["name"] == "DeliveryRiskDetected")["variables"]["deliveryRiskEvent"]
        self.db.record_delivery_risk("S-test", 999, risk)
        arrival = self.db.queue_transport_event(
            "S-test", "ShipmentArrived", "SIM:J-test:ShipmentArrived",
            "2026-12-01T10:30:00Z", {"recipientId": "C-01"},
            source="p2-simulator", input_mode="simulated",
        )
        late = self.http.post("/shipments/S-test/dispatcher-decision", json={
            "eventId": "late-decision", "decision": "APPROVE_ALTERNATIVE_ROUTE",
            "routeId": "route-02"})
        self.assertEqual(late.status_code, 409, late.json)
        self.assertIn("already arrived", late.json["error"])
        record = self.db.record_arrival("S-test", 999, arrival["event"])
        self.assertEqual(record["transportState"], "ARRIVED")
        self.assertEqual(record["routeId"], "route-01")
        self.assertIsNone(record.get("pendingDeliveryRisk"))
        types = [event["eventType"] for event in self.db.events("S-test")]
        self.assertLess(types.index("DeliveryRiskClosedAtArrival"), types.index("ShipmentArrived"))
        self.assertNotIn("RouteChangeAcknowledged", types)

    def test_p2_manual_event_rejects_invalid_time_and_mismatched_receipt(self):
        unknown_arrival = self.http.post(
            "/shipments/not-known/arrive",
            json={
                "eventId": "unknown-arrival",
                "occurredAt": "2026-12-01T10:22:00Z",
                "recipientId": "C-01",
            },
        )
        unknown_receipt = self.http.post(
            "/shipments/not-known/acknowledge-receipt",
            json={
                "eventId": "unknown-receipt",
                "occurredAt": "2026-12-01T10:24:00Z",
                "recipientId": "C-01",
                "quantityReceived": 3,
            },
        )
        self.assertEqual(unknown_arrival.status_code, 404)
        self.assertEqual(unknown_receipt.status_code, 404)
        self.loading_wait()
        self.verify()
        self.db.advance(
            "S-test",
            self.key,
            "AWAITING_LOADING",
            "AWAITING_DISPATCH",
            "LoadingVerified",
        )
        self.db.reserve_command("S-test", "DispatchApproved", "dispatcher")
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        dispatch_event = next(
            event
            for event in self.db.events("S-test")
            if event["eventType"] == "ShipmentDispatched"
        )
        self.db.start_transport("S-test", 999, dispatch_event)
        self.db.start_journey("S-test", 999)
        arrival = dict(
            eventId="arrival-bad-time",
            occurredAt="not-a-timestamp",
            recipientId="C-01",
        )
        self.assertEqual(
            self.http.post("/shipments/S-test/arrive", json=arrival).status_code,
            400,
        )
        arrival["occurredAt"] = "2026-12-01T10:22:00Z"
        self.assertEqual(
            self.http.post("/shipments/S-test/arrive", json=arrival).status_code,
            202,
        )
        arrival_event = next(
            message["variables"]["arrivalEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "ShipmentArrived"
        )
        self.db.record_arrival("S-test", 999, arrival_event)
        bad_receipt = dict(
            eventId="receipt-wrong-count",
            occurredAt="2026-12-01T10:24:00Z",
            recipientId="C-01",
            quantityReceived=2,
        )
        self.assertEqual(
            self.http.post(
                "/shipments/S-test/acknowledge-receipt", json=bad_receipt
            ).status_code,
            409,
        )
        self.assertFalse(self.db.get("S-test")["vehicleReleased"])

    def test_upgrade_repairs_legacy_receipt_vehicle_release_collision(self):
        self.loading_wait()
        self.verify()
        self.db.advance(
            "S-test", self.key, "AWAITING_LOADING", "AWAITING_DISPATCH", "LoadingVerified"
        )
        self.db.reserve_command("S-test", "DispatchApproved", "dispatcher")
        self.db.advance(
            "S-test", self.key, "AWAITING_DISPATCH", "DISPATCHED", "ShipmentDispatched"
        )
        dispatch = next(
            event
            for event in self.db.events("S-test")
            if event["eventType"] == "ShipmentDispatched"
        )
        self.db.start_transport("S-test", 999, dispatch)
        self.db.start_journey("S-test", 999)
        arrival = {
            "eventId": "arrival-upgrade",
            "occurredAt": "2026-12-01T10:22:00Z",
            "recipientId": "C-01",
        }
        self.assertEqual(self.http.post("/shipments/S-test/arrive", json=arrival).status_code, 202)
        arrival_event = next(
            message["variables"]["arrivalEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "ShipmentArrived"
        )
        self.db.record_arrival("S-test", 999, arrival_event)

        receipt = {
            "eventId": "P2:S-test:VehicleReleased",
            "occurredAt": "2026-12-01T10:24:00Z",
            "recipientId": "C-01",
            "quantityReceived": 3,
        }
        self.assertEqual(
            self.http.post("/shipments/S-test/acknowledge-receipt", json=receipt).status_code,
            202,
        )
        receipt_event = next(
            message["variables"]["receiptEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "ReceiptAcknowledged"
        )

        # Simulate the previous version: receipt activity occupied the internal
        # VehicleReleased ID, while the completed state was already persisted.
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.update(transportState="COMPLETED", vehicleReleased=True)
            self.db.event(
                db,
                record,
                "ReceiptAcknowledged",
                actor=receipt_event["source"],
                process="P2",
                event_id=receipt_event["eventId"],
                payload=receipt_event["payload"],
                occurred_at=receipt_event["occurredAt"],
                recorded_at=receipt_event["recordedAt"],
                source=receipt_event["source"],
                input_mode=receipt_event["inputMode"],
            )
            self.db.save(db, record)

        for _ in range(2):
            record = self.db.release_vehicle("S-test", 999, receipt_event)
            self.assertEqual(record["transportState"], "COMPLETED")
            self.assertTrue(record["vehicleReleased"])
        events = self.db.events("S-test")
        receipt_activities = [e for e in events if e["eventType"] == "ReceiptAcknowledged"]
        release_activities = [e for e in events if e["eventType"] == "VehicleReleased"]
        self.assertEqual(len(receipt_activities), 1)
        self.assertEqual(len(release_activities), 1)
        self.assertEqual(receipt_activities[0]["producerEventId"], receipt["eventId"])

    def test_invalid_input(self):
        for change in (
            {"quantity": True},
            {"quantity": 0},
            {"quantity": 101},
            {"vehicleId": "V-03"},
            {"shipmentId": " "},
            {"deliveryDeadline": "tomorrow"},
            {"originalShipmentId": "old"},
            {"productId": "UNKNOWN"},
            {"batchId": "DEMO-YOGHURT-001"},
            {"productId": None},
            {"batchId": ""},
        ):
            self.assertEqual(
                self.http.post("/shipments", json={**sample(), **change}).status_code,
                400,
            )

        self.assertEqual(self.http.post("/shipments", json=[]).status_code, 400)
        self.loading_wait()
        for value in (False, "true", 1, None):
            self.assertEqual(
                self.http.post(
                    "/shipments/S-test/verify-loading",
                    json=dict(
                        actor="warehouse",
                        quantityChecked=value,
                        packagingChecked=True,
                        vehicleReady=True,
                    ),
                ).status_code,
                400,
            )

    def test_unroutable_shipment_ids_have_no_side_effects(self):
        with patch.object(service, "engine_call") as engine:
            for shipment_id in (
                "S/001",
                "S%2F001",
                "S\\001",
                ".",
                "..",
                "S?001",
                "S#001",
                "S 001",
                "S\n001",
                "S\x00001",
                "S-é",
            ):
                with self.subTest(shipment_id=shipment_id):
                    response = self.http.post(
                        "/shipments", json=sample(shipment=shipment_id)
                    )
                    self.assertEqual(response.status_code, 400, response.json)
                    self.assertIn("shipmentId", response.json["error"])
            engine.assert_not_called()
        with self.db.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM shipments").fetchone()[0], 0
            )
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.available(), 20)
        self.create()

    def test_supported_shipment_id_is_routable(self):
        shipment_id = "S_001.v2-3"
        self.create(shipment=shipment_id)
        self.assertEqual(self.http.get(f"/shipments/{shipment_id}").status_code, 200)
        self.assertEqual(
            self.http.get(f"/shipments/{shipment_id}/events").status_code, 200
        )
        response = self.http.post(
            f"/shipments/{shipment_id}/dispatch", json={"actor": "dispatcher"}
        )
        self.assertEqual(response.status_code, 409)

    def test_uncertain_start_stays_visible_and_blocks_duplicate(self):
        with (
            patch.object(service, "engine_call", side_effect=TimeoutError),
            self.assertLogs(service.app.logger, level="ERROR"),
        ):
            self.assertEqual(
                self.http.post("/shipments", json=sample()).status_code, 502
            )
        self.assertEqual(self.http.post("/shipments", json=sample()).status_code, 409)

    def test_supply_id_cannot_shadow_a_future_business_activity(self):
        self.wait_shortage()
        # Supply IDs must not collide with internal activity IDs.
        self.db.reserve_command(
            "S-two", "StockReplenished", "warehouse", ("P1:S-two:ShipmentCancelled", 1)
        )
        self.check("S-two", 3)
        self.db.reserve_command("S-two", "ShipmentCancellationRequested", "dispatcher")
        self.db.advance(
            "S-two", self.key, "WAITING_STOCK", "CANCELLED", "ShipmentCancelled"
        )
        self.assertEqual(self.db.get("S-two")["state"], "CANCELLED")

    def test_expired_uncertain_messages_are_not_republished(self):
        self.loading_wait()
        self.verify()
        with self.db.transaction() as db:
            import json

            message_id, raw = db.execute("SELECT id,data FROM outbox").fetchone()
            data = json.loads(raw)
            data["createdAt"] = "2020-01-01T00:00:00Z"
            db.execute(
                "UPDATE outbox SET data=? WHERE id=?", (json.dumps(data), message_id)
            )
        self.assertEqual(self.db.pending_messages(), [])
        with self.db.transaction() as db:
            self.assertIn(
                "EXPIRED", db.execute("SELECT data FROM outbox").fetchone()[0]
            )

    def test_catalog_contains_five_distinct_products_and_preserves_stock_on_reopen(
        self,
    ):
        stock = self.http.get("/stock").json
        self.assertEqual(stock["datasetVersion"], 3)
        self.assertEqual(stock["available"], 100)
        self.assertEqual(stock["shipmentCount"], 0)
        self.assertEqual(len(stock["products"]), 5)
        self.assertEqual(len({p["productId"] for p in stock["products"]}), 5)
        self.assertTrue(
            all(
                p["available"] == 20 and p["packsPerCrate"] == 12
                for p in stock["products"]
            )
        )
        self.create(quantity=15)
        self.check()
        reopened = Registry(self.db.path).inventory()
        self.assertEqual(reopened["available"], 85)
        self.assertEqual(reopened["shipmentCount"], 1)
        self.assertEqual(
            next(
                p["available"]
                for p in reopened["products"]
                if p["productId"] == "PRD-001"
            ),
            5,
        )

    def test_other_products_cannot_satisfy_milk_shortage(self):
        self.wait_shortage()
        self.assertEqual(self.available(), 5)
        self.assertEqual(self.available("PRD-002"), 20)
        self.assertEqual(self.db.inventory()["available"], 85)
        self.assertEqual(self.db.get("S-two")["state"], "WAITING_STOCK")
        self.db.reserve_command(
            "S-two", "StockReplenished", "warehouse", ("milk-supply", 5)
        )
        self.assertTrue(self.check("S-two", 3)["stockAvailable"])
        self.assertEqual(self.available(), 0)
        for product in self.db.inventory()["products"][1:]:
            self.assertEqual(product["available"], 20)

    def test_different_products_reserve_independently(self):
        self.create(quantity=15)
        self.check()
        self.create(
            shipment="S-two",
            journey="J-two",
            vehicle="V-02",
            quantity=10,
            product="PRD-005",
            batch="DEMO-SALAD-001",
        )
        self.assertTrue(self.check("S-two", 2)["stockAvailable"])
        self.assertEqual(self.available(), 5)
        self.assertEqual(self.available("PRD-005"), 10)
        self.assertEqual(self.available("PRD-004"), 20)

    def test_simultaneous_orders_cannot_overdraw_one_product(self):
        self.create(quantity=15)
        self.create(shipment="S-two", journey="J-two", vehicle="V-02", quantity=15)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda item: self.db.check_stock(item[0], self.key, item[1]),
                    [("S-test", 10), ("S-two", 11)],
                )
            )
        self.assertCountEqual([r["stockAvailable"] for r in results], [True, False])
        self.assertEqual(self.available(), 5)

    def test_replenishment_rejects_other_product_or_batch_without_side_effects(self):
        self.wait_shortage()
        for change in ({"productId": "PRD-002"}, {"batchId": "DEMO-YOGHURT-001"}):
            response = self.http.post(
                "/shipments/S-two/replenish-stock",
                json={
                    "actor": "warehouse",
                    "eventId": "wrong-product",
                    "quantity": 5,
                    **change,
                },
            )
            self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.available(), 5)
        self.assertIsNone(self.db.get("S-two")["pendingCommand"])

    def test_product_validation_does_not_start_engine_or_reserve_vehicle(self):
        with patch.object(service, "engine_call") as engine:
            for change in (
                {"productId": "PRD-006"},
                {"batchId": "OTHER-BATCH"},
                {"productId": "PRD-002"},
                {"productId": None},
            ):
                response = self.http.post("/shipments", json={**sample(), **change})
                self.assertEqual(response.status_code, 400, response.json)
            missing = sample()
            del missing["batchId"]
            self.assertEqual(
                self.http.post("/shipments", json=missing).status_code, 400
            )
            engine.assert_not_called()
        self.assertEqual(self.db.inventory()["shipmentCount"], 0)
        self.create()

    def test_activity_and_xes_export_preserve_product_batch_identity(self):
        import xml.etree.ElementTree as ET

        self.loading_wait()
        events = self.db.events("S-test")
        self.assertTrue(
            all(
                e["payload"]["productId"] == "PRD-001"
                and e["payload"]["batchId"] == "DEMO-MILK-001"
                for e in events
            )
        )
        path = Path(self.temp.name) / "events.xes"
        export_xes(path, {"S-test": events})
        ns = {"x": "http://www.xes-standard.org/"}
        root = ET.parse(path).getroot()
        self.assertEqual(
            root.find("x:trace/x:string[@key='productId']", ns).get("value"), "PRD-001"
        )
        self.assertEqual(
            root.find("x:trace/x:event/x:string[@key='batchId']", ns).get("value"),
            "DEMO-MILK-001",
        )

    def test_legacy_pooled_registry_is_rejected_without_rewriting_it(self):
        import sqlite3

        path = Path(self.temp.name) / "legacy.sqlite"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE inventory (id INTEGER PRIMARY KEY, available INTEGER NOT NULL)"
            )
            db.execute("INSERT INTO inventory VALUES (1,17)")
        db.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "legacy pooled stock"):
            Registry(path)
        self.assertEqual(path.read_bytes(), before)

    def test_demo_menu_can_select_a_different_chilled_product(self):
        from tools import demo

        def local_http(base, path, data=None):
            response = (
                self.http.get(path) if data is None else self.http.post(path, json=data)
            )
            return response.status_code, response.json

        with (
            patch.object(demo, "http", side_effect=local_http),
            patch.object(demo, "wait_for_action"),
            patch("builtins.input", side_effect=["A", "4"]),
            patch("builtins.print"),
        ):
            demo.perform("http://example.test", "1")
        record = self.db.get("demo-A")
        self.assertEqual(record["productId"], "PRD-004")
        self.assertEqual(record["batchId"], "DEMO-CHICKEN-001")
        self.assertEqual(record["quantity"], 15)

    def test_demo_manual_actions_post_to_the_event_endpoints(self):
        from tools import demo

        self.create(shipment="demo-A", vehicle="V-01", quantity=15)
        shipment = self.db.get("demo-A")
        calls = []

        def local_http(base, path, data=None):
            calls.append((path, data))
            if path == "/shipments/demo-A":
                return 200, shipment
            if path == "/shipments/demo-A/events":
                return 200, [
                    {
                        "eventType": "ShipmentCreated",
                        "occurredAt": "2026-11-11T10:00:00Z",
                    }
                ]
            return 202, {"accepted": True}

        with (
            patch.object(demo, "http", side_effect=local_http),
            patch.object(demo, "wait_for_action"),
            patch("builtins.input", return_value="A"),
            patch("builtins.print"),
        ):
            demo.perform("http://example.test", "9")
            demo.perform("http://example.test", "10")
            demo.perform("http://example.test", "14")
            demo.perform("http://example.test", "15")

        posted_paths = [path for path, data in calls if data is not None]
        self.assertEqual(
            posted_paths,
            [
                "/shipments/demo-A/arrive",
                "/shipments/demo-A/acknowledge-receipt",
                "/shipments/demo-A/receiving/receipt",
                "/shipments/demo-A/receiving/inspection",
            ],
        )


class P3Receiving(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        service.registry = Registry(Path(self.temp.name) / "registry.sqlite")
        self.db = service.registry
        self.http = service.app.test_client()
        self.db.create(sample())
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.pop("monitoringProfile", None)  # legacy fixture bypasses loading
            record.update(
                state="DISPATCHED",
                stockReserved=True,
                processInstanceKey="111",
                handoffStatus="ACKNOWLEDGED",
                p2ProcessInstanceKey="999",
                transportState="ARRIVED",
                vehicleReleased=False,
            )
            self.db.save(db, record)
            self.db.event(
                db,
                record,
                "ShipmentArrived",
                process="P2",
                event_id=self.db.transport_event_journal_id(
                    "S-test", "ShipmentArrived", "arrival-1"
                ),
                payload={"recipientId": "C-01"},
                occurred_at="2026-12-01T10:22:00Z",
                recorded_at="2026-12-01T10:22:01Z",
                source="p2-simulator",
                input_mode="simulated",
                producer_event_id="arrival-1",
            )
        self.p3_key = 777
        self.notification = {
            "deliveryId": "P2:S-test:ShipmentArrivalNotification:arrival-1",
            "eventType": "ShipmentArrivalNotification",
            "shipmentId": "S-test",
            "journeyId": "J-test",
            "productId": "PRD-001",
            "batchId": "DEMO-MILK-001",
            "arrival": {"recipientId": "C-01"},
            "sourceEventId": "arrival-1",
            "occurredAt": "2026-12-01T10:22:00Z",
            "recordedAt": "2026-12-01T10:22:01Z",
        }

    def test_quality_disposition_worker_is_registered(self):
        self.assertIn("p3-record-quality-status", service.JOB_TYPES)

    def test_delayed_p3_notification_is_accepted_after_p2_completes(self):
        with self.db.transaction() as db:
            record = self.db.read(db, "S-test")
            record.update(transportState="COMPLETED", vehicleReleased=True)
            self.db.save(db, record)

        response = self.start_p3()

        self.assertEqual(response.status_code, 202, response.json)
        starts = [
            message
            for message in self.db.pending_messages()
            if message["name"] == "P3ShipmentArrived"
        ]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["correlationKey"], "S-test")

    def manual_receipt(self):
        return self.http.post("/shipments/S-test/acknowledge-receipt", json={
            "eventId": "manual-receipt-1", "occurredAt": "2026-12-01T10:30:00Z",
            "recipientId": "C-01", "quantityReceived": 3,
        })

    def test_manual_p2_receipt_refused_while_p3_receiving_is_enabled(self):
        with patch.dict(os.environ, {"P3_ARRIVAL_URL": "http://communication:5001/integrations/p3/arrival"}):
            response = self.manual_receipt()
        self.assertEqual(response.status_code, 409, response.json)
        self.assertIn("receiving/receipt", response.json["error"])
        self.assertFalse(any(m["name"] == "ReceiptAcknowledged" for m in self.db.pending_messages()))

    def test_manual_p2_receipt_refused_once_p3_owns_the_shipment(self):
        self.assertEqual(self.start_p3().status_code, 202)
        service.ready.set()
        self.addCleanup(service.ready.clear)
        with patch.dict(os.environ, {"P3_ARRIVAL_URL": ""}):
            response = self.manual_receipt()
        self.assertEqual(response.status_code, 409, response.json)
        self.assertIn("through P3", response.json["error"])
        self.assertFalse(self.db.get("S-test")["vehicleReleased"])

    def start_p3(self):
        return self.http.post(
            "/integrations/p3/arrival",
            json={"event": self.notification},
            headers={"Idempotency-Key": self.notification["deliveryId"]},
        )

    def receive_and_inspect(self, passed=True):
        arrival = self.start_p3()
        self.assertEqual(arrival.status_code, 202, arrival.json)
        self.db.record_p3_arrival("S-test", self.p3_key, self.notification)
        received = self.http.post(
            "/shipments/S-test/receiving/receipt",
            json={
                "eventId": "receipt-1",
                "occurredAt": "2026-12-01T10:24:00Z",
                "recipientId": "C-01",
                "quantityReceived": 3,
                "receiver": "cafeteria",
            },
        )
        self.assertEqual(received.status_code, 202, received.json)
        receipt_event = next(
            message["variables"]["receiptEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "GoodsReceived"
        )
        self.db.record_p3_receipt("S-test", self.p3_key, receipt_event)
        p2_receipt = next(
            message["variables"]["receiptEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "ReceiptAcknowledged"
        )
        self.db.release_vehicle("S-test", 999, p2_receipt)
        inspected = self.http.post(
            "/shipments/S-test/receiving/inspection",
            json={
                "eventId": "inspection-1",
                "occurredAt": "2026-12-01T10:30:00Z",
                "inspector": "quality-checker",
                "passed": passed,
            },
        )
        self.assertEqual(inspected.status_code, 202, inspected.json)
        inspection_event = next(
            message["variables"]["inspectionEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "GoodsInspected"
        )
        self.db.record_p3_inspection("S-test", self.p3_key, inspection_event)

    def test_receipt_releases_vehicle_and_inspection_admits_separate_stock(self):
        first = self.start_p3()
        self.assertEqual(first.status_code, 202, first.json)
        retry = self.start_p3()
        self.assertEqual(retry.status_code, 202, retry.json)
        self.assertEqual(first.json["messageId"], retry.json["messageId"])
        changed = dict(self.notification, productId="PRD-002")
        self.assertEqual(
            self.http.post(
                "/integrations/p3/arrival",
                json={"event": changed},
                headers={"Idempotency-Key": changed["deliveryId"]},
            ).status_code,
            409,
        )
        self.db.record_p3_arrival("S-test", self.p3_key, self.notification)

        payload = {
            "eventId": "receipt-1",
            "occurredAt": "2026-12-01T10:24:00Z",
            "recipientId": "C-01",
            "quantityReceived": 3,
            "receiver": "cafeteria",
        }
        response = self.http.post(
            "/shipments/S-test/receiving/receipt", json=payload
        )
        self.assertEqual(response.status_code, 202, response.json)
        self.assertEqual(
            self.http.post(
                "/shipments/S-test/receiving/receipt", json=payload
            ).status_code,
            202,
        )
        receipt_event = next(
            message["variables"]["receiptEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "GoodsReceived"
        )
        self.db.record_p3_receipt("S-test", self.p3_key, receipt_event)
        p2_receipt = next(
            message["variables"]["receiptEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "ReceiptAcknowledged"
        )
        released = self.db.release_vehicle("S-test", 999, p2_receipt)
        self.assertTrue(released["vehicleReleased"])
        self.assertEqual(released["transportState"], "COMPLETED")

        inspected = self.http.post(
            "/shipments/S-test/receiving/inspection",
            json={
                "eventId": "inspection-1",
                "occurredAt": "2026-12-01T10:30:00Z",
                "inspector": "quality-checker",
                "passed": True,
            },
        )
        self.assertEqual(inspected.status_code, 202, inspected.json)
        inspection_event = next(
            message["variables"]["inspectionEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "GoodsInspected"
        )
        self.db.record_p3_inspection("S-test", self.p3_key, inspection_event)
        self.assertTrue(
            self.db.record_p3_inspection(
                "S-test", self.p3_key, inspection_event
            )["inspectionPassed"]
        )
        self.assertEqual(
            self.db.admit_p3_stock("S-test", self.p3_key)["admissionOutcome"],
            "ADMITTED",
        )
        self.assertEqual(
            self.db.admit_p3_stock("S-test", self.p3_key)["admissionOutcome"],
            "ADMITTED",
        )
        stock = self.http.get("/receiving-stock?recipientId=C-01").json
        self.assertEqual(stock["available"], 3)
        self.assertEqual(stock["items"][0]["batchId"], "DEMO-MILK-001")
        self.assertNotIn("initialCrates", stock["items"][0])
        self.assertEqual(self.db.inventory()["products"][0]["available"], 20)

    def test_quality_hold_requires_p4_release_before_admission(self):
        self.receive_and_inspect()
        pending = self.http.post(
            "/integrations/p4/quality-status",
            json={
                "shipmentId": "S-test",
                "eventId": "quality-pending",
                "occurredAt": "2026-12-01T10:31:00Z",
                "qualityStatus": "PENDING",
                "incidentId": "INC-1",
            },
        )
        self.assertEqual(pending.status_code, 202, pending.json)
        self.assertEqual(
            self.db.admit_p3_stock("S-test", self.p3_key)["admissionOutcome"],
            "HELD",
        )
        stale_release = self.http.post(
            "/integrations/p4/quality-status",
            json={
                "shipmentId": "S-test",
                "eventId": "stale-release",
                "occurredAt": "2026-12-01T10:34:00Z",
                "qualityStatus": "RELEASED",
                "incidentId": "INC-OLD",
            },
        )
        self.assertEqual(stale_release.status_code, 409, stale_release.json)
        released = self.http.post(
            "/integrations/p4/quality-status",
            json={
                "shipmentId": "S-test",
                "eventId": "quality-released",
                "occurredAt": "2026-12-01T10:35:00Z",
                "qualityStatus": "RELEASED",
                "incidentId": "INC-1",
            },
        )
        self.assertEqual(released.status_code, 202, released.json)
        quality_event = next(
            message["variables"]["qualityStatusEvent"]
            for message in self.db.pending_messages()
            if message["name"] == "QualityStatusChanged"
        )
        self.db.record_p3_quality_status(
            "S-test", self.p3_key, quality_event
        )
        self.assertEqual(
            self.db.admit_p3_stock("S-test", self.p3_key)["admissionOutcome"],
            "ADMITTED",
        )
        self.assertEqual(self.http.get("/receiving-stock").json["available"], 3)

    def test_failed_inspection_cannot_admit_stock(self):
        self.receive_and_inspect(passed=False)
        with self.assertRaises(Conflict):
            self.db.admit_p3_stock("S-test", self.p3_key)
        self.assertEqual(self.http.get("/receiving-stock").json["available"], 0)


class TomTomAdapter(unittest.TestCase):
    KEY = "test-key-should-never-be-logged"

    def adapter(self, directory):
        from transport_integrations import TransportIntegrations

        return TransportIntegrations({
            "TOMTOM_API_KEY": self.KEY,
            "REGISTRY_PATH": str(Path(directory) / "registry.sqlite"),
        })

    def log_text(self, directory):
        return (Path(directory) / "tomtom-responses.jsonl").read_text(encoding="utf-8")

    def test_live_response_sets_traffic_eta_and_logs_without_key(self):
        import io

        class Response(io.BytesIO):
            status = 200

        body = json.dumps({"routes": [{"summary": {
            "lengthInMeters": 6186, "travelTimeInSeconds": 1246,
            "noTrafficTravelTimeInSeconds": 1093, "trafficDelayInSeconds": 123}}]}).encode()
        with tempfile.TemporaryDirectory() as directory, patch(
            "transport_integrations.urllib.request.urlopen", return_value=Response(body)
        ) as urlopen:
            integrations = self.adapter(directory)
            eta, source = integrations.eta(integrations.route("route-01", "C-01"), "route-01", 0)
            self.assertEqual(source, "tomtom-traffic")
            self.assertTrue(eta.endswith("Z"))
            self.assertIn("traffic=true", urlopen.call_args.args[0])
            entry = json.loads(self.log_text(directory).splitlines()[-1])
            self.assertEqual(entry["result"], "ok")
            self.assertEqual(entry["summary"]["travelTimeInSeconds"], 1246)
            self.assertNotIn(self.KEY, self.log_text(directory))

    def test_failure_or_bad_data_is_unavailable_not_estimated(self):
        from urllib.error import HTTPError

        url = f"https://api.tomtom.com/routing?key={self.KEY}"
        failures = [
            HTTPError(url, 403, f"Forbidden for {url}", {}, None),
            TimeoutError("timed out"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            integrations = self.adapter(directory)
            for failure in failures:
                with patch("transport_integrations.urllib.request.urlopen", side_effect=failure):
                    self.assertEqual(
                        integrations.eta(integrations.route("route-01", "C-01"), "route-01", 0),
                        (None, "tomtom-unavailable"),
                    )
            text = self.log_text(directory)
            self.assertEqual(text.count('"result": "unavailable"'), 2)
            self.assertIn('"httpStatus": 403', text)
            self.assertNotIn(self.KEY, text)


class P2Compliance(unittest.TestCase):
    P2 = Path(__file__).resolve().parents[1] / "compliance/p2"

    def test_real_p2_log_conforms(self):
        from compliance.check import read_traces, replay

        traces = read_traces(self.P2 / "execution-log.xes")
        self.assertGreaterEqual(len(traces), 2)
        self.assertTrue(any("RouteChangeAcknowledged" in t for t in traces.values()))
        for case, events in traces.items():
            with self.subTest(case=case):
                result = replay(self.P2 / "rules.xml", events)
                self.assertTrue(result["valid"] and result["accepting"], result)

    def test_invalid_p2_traces_are_rejected(self):
        from compliance.check import replay

        data = json.loads((self.P2 / "invalid-traces.json").read_text())
        traces = {k: v for k, v in data.items() if isinstance(v, list)}
        self.assertEqual(len(traces), 6)
        for case, events in traces.items():
            with self.subTest(case=case):
                self.assertFalse(replay(self.P2 / "rules.xml", events)["valid"])

    def test_receipt_is_owed_after_arrival(self):
        from compliance.check import replay

        result = replay(self.P2 / "rules.xml", [
            "ShipmentDispatched", "TransportStarted", "JourneyStarted", "ShipmentArrived"])
        self.assertTrue(result["valid"])
        self.assertFalse(result["accepting"])
        self.assertIn("ReceiptAcknowledged", result["pending"])

    def test_p2_graph_has_portal_metadata(self):
        Graph(self.P2 / "rules.xml")
        import xml.etree.ElementTree as ET

        resources = ET.parse(self.P2 / "rules.xml").getroot().find("./specification/resources")
        self.assertIsNotNone(resources.find("subProcesses"))
        for event in resources.findall("events/event"):
            self.assertIsNotNone(event.find("custom/visualization/location"))


class Compliance(unittest.TestCase):
    def test_portal_xml_has_required_editor_metadata(self):
        import xml.etree.ElementTree as ET

        root = ET.parse(MODEL).getroot()
        resources = root.find("./specification/resources")
        self.assertIsNotNone(resources.find("subProcesses"))
        for event in resources.findall("events/event"):
            with self.subTest(event=event.get("id")):
                self.assertIsNotNone(event.find("custom/eventData"))
                location = event.find("custom/visualization/location")
                size = event.find("custom/visualization/size")
                self.assertIsNotNone(location)
                self.assertIsNotNone(size)
                self.assertGreaterEqual(int(location.get("xLoc")), 0)
                self.assertGreaterEqual(int(location.get("yLoc")), 0)
                self.assertGreater(int(size.get("width")), 0)
                self.assertGreater(int(size.get("height")), 0)

    def test_unsupported_models_are_rejected_before_replay(self):
        import xml.etree.ElementTree as ET

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.xml"
            for fault in (
                "root",
                "events",
                "nested",
                "relation",
                "endpoint",
                "marking",
            ):
                with self.subTest(fault=fault):
                    tree = ET.parse(MODEL)
                    root = tree.getroot()
                    if fault == "root":
                        root.tag = "{urn:unsupported}dcrgraph"
                    elif fault == "events":
                        root.find("./specification/resources/events").clear()
                    elif fault == "nested":
                        ET.SubElement(
                            root.find("./specification/resources/events/event"),
                            "event",
                            id="nested",
                        )
                    elif fault == "relation":
                        root.find(
                            "./specification/constraints/conditions/condition"
                        ).tag = "{urn:unsupported}condition"
                    elif fault == "endpoint":
                        del root.find(
                            "./specification/constraints/conditions/condition"
                        ).attrib["sourceId"]
                    else:
                        marking = root.find("./runtime/marking")
                        marking.remove(marking.find("pendingResponses"))
                    tree.write(path)
                    with self.assertRaises(ValueError):
                        Graph(path)

    def test_cli_rejects_empty_or_unsupported_evidence(self):
        namespace = 'xmlns="http://www.xes-standard.org/"'
        trace = '<trace><string key="concept:name" value="case"/></trace>'
        inputs = (
            ("empty.xes", f"<log {namespace}/>", "no recognized traces"),
            ("empty.json", "{}", "no recognized traces"),
            (
                "namespace-free.xes",
                '<log><trace><string key="concept:name" value="case"/>'
                '<event><string key="concept:name" value="ShipmentDispatched"/></event>'
                "</trace></log>",
                "expected an XES log in namespace",
            ),
            (
                "wrong-namespace.xes",
                '<log xmlns="urn:unsupported"><trace/></log>',
                "expected an XES log in namespace",
            ),
            (
                "duplicate.xes",
                f"<log {namespace}>{trace}{trace}</log>",
                "duplicate XES trace",
            ),
            ("unnamed.xes", f"<log {namespace}><trace/></log>", "concept:name"),
            ("empty-file.xes", "", "Cannot verify compliance"),
            ("null.json", "null", "expected a JSON event list"),
            ("missing-activity.json", "[{}]", "non-empty activity name"),
            ("numeric-activity.json", "[42]", "non-empty activity name"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for filename, content, message in inputs:
                with self.subTest(filename=filename):
                    path = Path(directory) / filename
                    path.write_text(content)
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "compliance/check.py"), str(path)],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 2, result)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_cli_checks_saved_execution_evidence(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "compliance/check.py"),
                str(ROOT / "compliance/p1/execution-log.xes"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import json

        cases = json.loads(result.stdout)
        self.assertEqual(len(cases), 3)
        self.assertTrue(
            all(case["valid"] and case["accepting"] for case in cases.values())
        )

    def test_valid_replenishment_and_cancellation(self):
        happy = [
            "ShipmentCreated",
            "StockChecked",
            "StockAllocated",
            "TransportAssigned",
            "LoadingVerified",
            "ShipmentDispatched",
        ]
        recovery = [
            "ShipmentCreated",
            "StockChecked",
            "StockUnavailable",
            "StockReplenished",
            "StockChecked",
            "StockUnavailable",
            "StockReplenished",
        ] + happy[1:]
        for events in (
            happy,
            recovery,
            [
                "ShipmentCreated",
                "StockChecked",
                "StockUnavailable",
                "ShipmentCancelled",
            ],
        ):
            self.assertEqual(
                replay(MODEL, events), dict(valid=True, accepting=True, pending=[])
            )

    def test_invalid_order_cancellation_and_unfinished_trace(self):
        traces = [
            ["ShipmentCreated", "LoadingVerified"],
            ["ShipmentCreated", "StockChecked", "ShipmentDispatched"],
            [
                "ShipmentCreated",
                "StockChecked",
                "StockUnavailable",
                "ShipmentCancelled",
                "ShipmentDispatched",
            ],
            ["ShipmentCreated", "StockChecked", "StockUnavailable", "StockAllocated"],
            [
                "ShipmentCreated",
                "StockChecked",
                "StockUnavailable",
                "StockReplenished",
                "StockAllocated",
            ],
            ["ShipmentCreated", "StockChecked", "StockAllocated", "ShipmentCancelled"],
        ]
        for events in traces:
            self.assertFalse(replay(MODEL, events)["valid"], events)
        for events in (
            [],
            ["ShipmentCreated"],
            ["ShipmentCreated", "StockChecked", "StockUnavailable"],
        ):
            self.assertFalse(replay(MODEL, events)["accepting"], events)


if __name__ == "__main__":
    unittest.main()
