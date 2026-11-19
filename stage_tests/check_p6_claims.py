"""Run P6 against two existing shipment/incident pairs and export actual events."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from check_p3_compliance import http, require, timestamp, wait_for
from check_stage1 import engine_records

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from compliance.check import export_xes, replay


def run_claim(base, shipment_id, outcome):
    shipment = require(http(base, f"/shipments/{shipment_id}"), 200, "shipment lookup")
    incident_id = shipment["qualityIncidentId"]
    event_path = f"/shipments/{shipment_id}/events"
    events = require(http(base, event_path), 200, "evidence lookup")
    refs = [event["eventId"] for event in events
            if event["process"] != "P6"
            and event.get("payload", {}).get("incidentId") == incident_id]
    if not refs:
        raise RuntimeError(f"no journal evidence for {incident_id}")
    loss = dict(incidentId=incident_id, originalShipmentId=shipment_id,
                lossDescription="Demonstration loss for supplier claim",
                actor="demo operator", evidenceRefs=refs[-10:])
    accepted = require(http(base, "/claims", loss), 202, "record loss")
    claim_id = accepted["claimId"]
    claim_path = f"/claims/{claim_id}"
    repeat = require(http(base, "/claims", loss), 202, "retry loss")
    if repeat["claimId"] != claim_id:
        raise RuntimeError("identical loss created another claim")
    require(http(base, "/claims", dict(loss, lossDescription="conflicting loss")),
            409, "conflicting loss")

    def claim_in_state(*states):
        claim = require(http(base, claim_path), 200, "read claim")
        return claim if claim["status"] in states else None

    wait_for("claim submission", lambda: claim_in_state("SUBMITTED", "CLOSED"))
    response_path = claim_path + "/supplier-response"
    response = dict(outcome=outcome, reason="Demonstration supplier decision",
                    actor="simulated supplier")
    require(http(base, response_path, response), 202, "record response")
    require(http(base, response_path, response), 202, "retry response")
    final = wait_for("claim closure", lambda: claim_in_state("CLOSED"))
    if final["outcome"] != outcome:
        raise RuntimeError(f"unexpected claim outcome: {final}")
    end = "Settled" if outcome == "SETTLED" else "Rejected"
    wait_for("Zeebe end event " + end, lambda: engine_records(
        final["processInstanceKey"], end, "ELEMENT_COMPLETED"))
    events = require(http(base, event_path), 200, "claim journal")
    trace = [event for event in events if event["process"] == "P6"
             and event["payload"].get("claimId") == claim_id]
    result = replay(ROOT / "compliance/p6/rules.xml", [event["eventType"] for event in trace])
    if not result["valid"] or not result["accepting"]:
        raise RuntimeError(f"P6 journal failed local DCR replay: {result}")
    return claim_id, trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settlement_shipment")
    parser.add_argument("rejection_shipment")
    parser.add_argument("--base", default="http://localhost:5001")
    parser.add_argument("--output-dir", type=Path, default=Path("runtime/verification-results/p6"))
    args = parser.parse_args()
    if args.settlement_shipment == args.rejection_shipment:
        parser.error("supply two shipments with distinct incidents")
    traces = dict(run_claim(args.base, shipment, outcome) for shipment, outcome in (
        (args.settlement_shipment, "SETTLED"), (args.rejection_shipment, "REJECTED")
    ))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    export_xes(args.output_dir / "execution-log.xes", traces)
    report = dict(checkedAt=timestamp(), runnerCheckout=subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        inputModes=["manual loss", "simulated supplier"], traces=traces)
    (args.output_dir / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: both P6 end events and local compliance; {args.output_dir}")


if __name__ == "__main__":
    main()
