"""Run four P4 investigations on new monitored shipments against a live stack.

Each scenario is a real shipment: P1 dispatches it, container monitoring and P2 transport
run, the foundation places the hold and sends its durable handoff, P4 opens a DCR case,
P3 receives (releasing the vehicle while quality is still on hold), finalizes monitoring
and waits ON_HOLD, and the P4 investigation decides with the assessed evidence version.
Forbidden actions are attempted and must be refused (HTTP 409).

  run1  delay risk -> cooling acknowledged, recovery evidence -> release -> admitted
  run2  container temperature excursion (AssessmentRequested) -> failed cooling -> replace
  run3  delay risk -> additional check -> return
  run4  delay risk -> assessed -> new container evidence -> decision refused ->
        reassessment -> release -> admitted

Prerequisites: fresh paired version-3 data, simulator on, local Siddhi, Compose default
P4_INCIDENT_URL, GPS_TICK_SECONDS=15 (time to raise a delay risk in transit). Example:

    .venv/bin/python stage_tests/check_p4_investigation.py --base http://localhost:5001
    .venv/bin/python compliance/p4/build_p4_evidence.py --live runtime/p4-compliance/evidence.json
"""

import argparse
import json
import sys
import time
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

# (scenario, product, trigger, steps before receipt, steps after ON_HOLD, P3 outcome).
# A step is an activity, ("blocked", activity) for an action that must be refused,
# ("temperature", scenario) to change the simulated container scenario, or
# ("await-evidence", n) to wait until P4 holds evidence version n.
SCENARIOS = [
    ("run1-delay-cooling-recovery-release", "PRD-001", "delay", [], [
        ("blocked", "AssessEvidence"),
        "ReviewWeatherData", "RequestCoolingAdjustment", "ReviewTemperatureLog",
        "RecordCoolingAcknowledgement", "RecordRecoveryEvidence", "AssessEvidence",
        "DecideRelease", "CloseInvestigation",
    ], "ADMITTED"),
    ("run2-temperature-failed-cooling-replace", "PRD-002", "sustained-excursion",
     [("temperature", "normal")], [
        "ReviewTemperatureLog", "RequestCoolingAdjustment", "RecordCoolingFailure",
        ("blocked", "RecordRecoveryEvidence"),
        "AssessEvidence", "DecideReplace", "CloseInvestigation",
    ], "REJECTED"),
    ("run3-delay-additional-check-return", "PRD-003", "delay", [], [
        "ReviewRouteAndETA", "ReviewTemperatureLog", "RequestAdditionalCheck",
        ("blocked", "AssessEvidence"), ("blocked", "CloseInvestigation"),
        "CompleteAdditionalCheck", "AssessEvidence", "DecideReturn", "CloseInvestigation",
    ], "REJECTED"),
    ("run4-newer-evidence-reassessment-release", "PRD-005", "delay", [
        "ReviewTemperatureLog", "AssessEvidence",
        ("temperature", "sustained-excursion"), ("await-evidence", 2),
        ("blocked", "DecideRelease"), ("temperature", "normal"),
    ], ["AssessEvidence", "DecideRelease", "CloseInvestigation"], "ADMITTED"),
]
ACTORS = {"RecordCoolingAcknowledgement": "vehicle-simulator",
          "RecordCoolingFailure": "vehicle-simulator",
          "RecordRecoveryEvidence": "recovery-evidence-manual",
          "CompleteAdditionalCheck": "cafeteria-receiver"}


def incident_path(incident_id, suffix=""):
    return f"/quality/incidents/{quote(incident_id, safe='')}{suffix}"


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


class Case:
    def __init__(self, base, shipment_id):
        self.base, self.shipment_id = base, shipment_id
        self.url = f"/shipments/{shipment_id}"
        self.incident_id, self.refused, self.reassessments = None, [], []
        self.counter = 0

    def get(self, path=None):
        return http(self.base, path or self.url)[1]

    def temperature(self, scenario):
        self.counter += 1
        require(http(self.base, self.url + "/simulation/temperature", {
            "commandId": f"temperature-{self.shipment_id}-{self.counter}",
            "actor": "p4-check", "scenario": scenario}), 202, f"temperature {scenario}")

    def incident(self):
        return self.get(incident_path(self.incident_id))

    def activity(self, step):
        self.counter += 1
        data = {"actor": ACTORS.get(step, "quality-officer"),
                "eventId": f"{self.shipment_id}-{self.counter}-{step}"}
        if step.startswith("Decide"):
            data["reason"] = "P4 live check: evidence assessed"
        return http(self.base, incident_path(self.incident_id, f"/activities/{step}"), data)

    def ensure_current_assessment(self):
        """Newer evidence (e.g. evaluation-unavailable at closure) requires reassessment."""
        view = self.incident()
        due = [e["id"] for e in (view.get("dcr") or {}).get("events", [])
               if e["pending"] and e["included"]]
        if "AssessEvidence" in due or view["registry"]["reassessmentNeeded"]:
            require(self.activity("AssessEvidence"), 200, "reassess newer evidence")
            self.reassessments.append(view["evidenceVersion"])

    def step(self, step):
        if isinstance(step, tuple):
            kind, value = step
            if kind == "temperature":
                return self.temperature(value)
            if kind == "await-evidence":
                return wait_for(f"P4 evidence version {value}", lambda: (
                    (view := self.incident()).get("evidenceVersion") or 0) >= value
                    and not view["pendingSystemEvents"], timeout=180)
            status, body = self.activity(value)
            if status != 409:
                raise RuntimeError(f"{value} was not refused: {status} {body}")
            self.refused.append({"activity": value, "status": status,
                                 "reason": body.get("error"),
                                 "enabledAtThatTime": body.get("enabled")})
            return None
        if step.startswith("Decide"):
            self.ensure_current_assessment()
            status, body = self.activity(step)
            if status == 409 and body.get("reassessmentRequired"):
                self.refused.append({"activity": step, "status": status,
                                     "reason": body.get("error")})
                wait_for("newer evidence delivered to P4",
                         lambda: not self.incident()["pendingSystemEvents"])
                self.ensure_current_assessment()
                status, body = self.activity(step)
            if status != 200:
                raise RuntimeError(f"{step} failed: {status} {body}")
            return body
        return require(self.activity(step), 200, f"execute {step}")


def dispatch(case, product_id, vehicle_id, trigger):
    now = datetime.now(timezone.utc)
    require(http(case.base, "/shipments", {
        "shipmentId": case.shipment_id, "journeyId": f"journey-{case.shipment_id}",
        "vehicleId": vehicle_id, "recipientId": "C-01", "routeId": "route-01",
        "quantity": 1, "productId": product_id, "batchId": BATCHES[product_id],
        "deliveryDeadline": iso(now + timedelta(hours=2)),
    }), 202, "create shipment")
    wait_for("AWAITING_LOADING with no pending command", lambda: (
        (r := case.get()).get("state") == "AWAITING_LOADING" and not r.get("pendingCommand")))
    if "monitoringProfile" not in case.get():
        raise RuntimeError("expected a new monitored shipment")
    require(http(case.base, case.url + "/verify-loading", {
        "actor": "p4-check", "quantityChecked": True, "packagingChecked": True,
        "vehicleReady": True}), 202, "verify loading")
    wait_for("loading verification", lambda: case.get().get("state") == "AWAITING_DISPATCH")
    if trigger != "delay":
        case.temperature(trigger)
    require(http(case.base, case.url + "/dispatch", {"actor": "p4-check"}), 202, "dispatch")
    record = wait_for("journey start", lambda: (
        r if (r := case.get()).get("transportState") == "IN_TRANSIT" else None))
    if trigger == "delay":
        require(http(case.base, "/integrations/siddhi/delivery-risk", {
            "shipmentId": case.shipment_id, "eventId": f"risk-{case.shipment_id}",
            "occurredAt": timestamp(), "deliveryDeadline": record["deliveryDeadline"],
            "eta": iso(datetime.now(timezone.utc) + timedelta(hours=3)),
            "reason": "P4 live check: late ETA", "estimatedDelaySeconds": 3600,
        }), 202, "accept delivery risk")
    case.incident_id = wait_for("quality hold", lambda: (
        r.get("qualityIncidentId") if (r := case.get()).get("qualityStatus") == "PENDING"
        else None), timeout=180)
    wait_for("P4 DCR case opened from the foundation handoff", lambda: (
        case.incident().get("status") == "OPEN"))


def run_case(base, run_id, index, scenario, product_id, trigger, before, after, expected):
    case = Case(base, f"p4live-{run_id}-{index}")
    dispatch(case, product_id, "V-01" if index % 2 else "V-02", trigger)
    case.step(("blocked", "CloseInvestigation"))
    wait_for("arrival and P3 receiving", lambda: case.get().get("p3Status")
             == "AWAITING_RECEIPT", timeout=420)
    for step in before:              # container sampling continues until receipt
        case.step(step)
    require(http(base, case.url + "/receiving/receipt", {
        "eventId": f"receipt-{case.shipment_id}", "occurredAt": timestamp(),
        "recipientId": "C-01", "quantityReceived": 1, "receiver": "cafeteria-receiver"}),
        202, "physical receipt")
    held_receipt = wait_for("vehicle released by receipt while quality is on hold", lambda: (
        r if (r := case.get()).get("vehicleReleased") else None), timeout=120)
    require(http(base, case.url + "/receiving/inspection", {
        "eventId": f"inspection-{case.shipment_id}", "occurredAt": timestamp(),
        "inspector": "cafeteria-receiver", "passed": True}), 202, "inspection")
    wait_for("monitoring finalized and P3 waiting on the quality hold",
             lambda: case.get().get("p3Status") == "ON_HOLD", timeout=180)
    for step in after:
        case.step(step)
    final = wait_for(f"P3 {expected}", lambda: (
        r if (r := case.get()).get("p3Status") == expected else None))
    investigation = case.incident()
    if investigation["status"] != "CLOSED" or not investigation.get("decision"):
        raise RuntimeError(f"investigation not closed with an applied decision: {investigation}")
    events = require(http(base, case.url + "/events"), 200, "read event history")
    return {
        "scenario": scenario, "shipmentId": case.shipment_id, "incidentId": case.incident_id,
        "trigger": trigger, "dcrMode": investigation["dcrMode"],
        "dcrCaseId": investigation["dcrCaseId"],
        "evidence": [{k: e[k] for k in ("deliveryId", "eventType", "reason", "evidenceVersion",
                                         "priority")} for e in investigation["evidence"]],
        "decision": investigation["decision"],
        "unappliedDecisions": investigation["unappliedDecisions"],
        "reassessedVersions": case.reassessments,
        "receiptWhileHeld": {"qualityStatus": held_receipt["qualityStatus"],
                             "vehicleReleased": held_receipt["vehicleReleased"]},
        "monitoringFinalization": final.get("monitoring", {}).get("finalization"),
        "qualityStatus": final["qualityStatus"], "p3Status": final["p3Status"],
        "refusedActions": case.refused,
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
    integrations = health.get("integrations", {})
    if health.get("stage") != 3 or "p4DcrEngine" not in integrations:
        raise RuntimeError(f"Expected the service with P4 enabled, got {health}")
    if integrations.get("p4IncidentHandoff") != "enabled":
        raise RuntimeError("P4_INCIDENT_URL is not set; the foundation handoff would stay queued")
    stock = require(http(args.base, "/stock"), 200, "read inventory")
    if stock.get("datasetVersion") != 3 or stock.get("shipmentCount") != 0:
        raise RuntimeError("Use a fresh isolated version-3 registry with no shipments")
    run_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    cases = []
    for index, definition in enumerate(SCENARIOS, start=1):
        cases.append(run_case(args.base, run_id, index, *definition))
        print("PASS", definition[0], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "capturedAt": timestamp(), "baseUrl": args.base, "runId": run_id,
        "durationSeconds": round(time.monotonic() - started), "health": health,
        "source": "live HTTP run on new monitored shipments; P1/P2/P3 in Zeebe; "
        f"container evaluation in Siddhi; P4 in {integrations['p4DcrEngine']}",
        "cases": cases,
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "runId": run_id, "evidence": str(args.output),
                      "cases": [{k: c[k] for k in ("scenario", "shipmentId", "incidentId",
                                                   "qualityStatus", "p3Status")}
                                for c in cases]}, indent=2))


if __name__ == "__main__":
    main()
