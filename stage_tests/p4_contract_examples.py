"""Deterministic, sanitized examples emitted by the real foundation registry.

Run this file explicitly to refresh the checked-in contract examples. These are
synthetic contract fixtures, never engine execution or P4/DCR evidence.
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deployer"))
from registry import Registry

AT = "2026-11-18T12:00:00Z"
FIXTURES = Path(__file__).parent / "fixtures/p4-contract"


def produce(directory, before_rejection=None):
    registry = Registry(Path(directory) / "registry.sqlite")
    registry.create(dict(shipmentId="CONTRACT-001", journeyId="J-CONTRACT-001", vehicleId="V-01",
                         recipientId="C-01", productId="PRD-001", batchId="DEMO-MILK-001",
                         quantity=1, routeId="route-01", deliveryDeadline=AT))
    with registry.transaction() as db:
        record = registry.read(db, "CONTRACT-001")
        registry.monitoring_start(record, "2026-11-18T11:59:50Z")
        registry.save(db, record)

    def emit(reason, event_id, priority="normal", missing=None):
        with registry.transaction() as db:
            record = registry.read(db, "CONTRACT-001")
            evidence = dict(eventId=event_id, occurredAt=AT, inputMode="recorded-fixture")
            if missing:
                evidence["missingEvidence"] = missing
            if reason == "delivery-risk":
                risk = dict(evidence, eventType="DeliveryRiskDetected", payload={
                    "etaSource": "local-demo-estimate", "estimatedArrival": "2026-11-18T12:10:00Z"})
                registry.establish_delivery_risk_hold(db, record, risk)
            else:
                registry.assessment_hold(db, record, reason, evidence, priority, event_id)
            registry.save(db, record)
        return registry.pending_messages("p4-http")[-1]["variables"]

    with patch("monitoring_registry.stamp", return_value=AT), patch("registry.now", return_value=AT):
        delay = emit("delivery-risk", "risk-001")
        initial = emit("temperature-excursion", "temperature-001")
        # Fixture context represents recorded observations, independent of live providers.
        with registry.transaction() as db:
            record = registry.read(db, "CONTRACT-001")
            record["weather"] = dict(available=True, observationId="weather-fixture-001",
                                     providerTime=AT, retrievedAt=AT, coordinates=[48.8566, 2.3522],
                                     conditions={"temperature_2m": 32}, inputMode="recorded-fixture")
            record["etaAssessment"] = dict(qualified=True, late=True, observedAt=AT, revision=0,
                                           inputMode="recorded-fixture")
            registry.save(db, record)
        urgent = emit("combined-transport-risk", "rising-window-001", "urgent")
        uncertainty = emit("container-telemetry-unavailable", "silence-001", missing=["valid container reading"])
        evaluation = emit("monitoring-evaluation-unavailable", "closure-001", missing=["evaluation through closing sequence"])
        record = registry.get("CONTRACT-001")
        rejection = dict(shipmentId=record["shipmentId"], eventId="decision-001", occurredAt=AT,
                         qualityStatus="REJECTED", incidentId=record["qualityIncidentId"],
                         expectedEvidenceVersion=record["evidenceVersion"])
        if before_rejection is not None:
            before_rejection(registry, initial, urgent)
        registry.accept_quality_status(record["shipmentId"], rejection["eventId"], AT, "REJECTED",
                                       rejection["incidentId"], rejection["expectedEvidenceVersion"])
        rejected = emit("temperature-excursion", "temperature-002")
    delivery = initial["event"]["deliveryId"]
    return registry, {
        "delivery-risk.json": delay,
        "initial-assessment.json": initial,
        "urgent-escalation.json": urgent,
        "uncertainty.json": uncertainty,
        "evaluation-unavailable.json": evaluation,
        "duplicate-delivery.json": dict(request=initial, idempotencyKey=delivery,
                                        acknowledgement=dict(deliveryId=delivery, status="duplicate")),
        "newer-evidence.json": dict(previous=initial, current=urgent, staleDisposition=dict(
            shipmentId="CONTRACT-001", eventId="stale-decision-001", occurredAt=AT,
            qualityStatus="RELEASED", incidentId=initial["event"]["incidentId"],
            expectedEvidenceVersion=initial["event"]["assessment"]["evidenceVersion"])),
        "rejected-goods.json": dict(disposition=rejection, subsequentAssessment=rejected),
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        _, examples = produce(directory)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, example in examples.items():
        (FIXTURES / name).write_text(json.dumps(example, indent=2, ensure_ascii=False) + "\n")
