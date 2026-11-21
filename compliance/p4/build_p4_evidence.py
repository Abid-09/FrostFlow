"""Build and verify the P4 quality-investigation compliance evidence.

Usage (from the repository root):

    python compliance/p4/build_p4_evidence.py
    python compliance/p4/build_p4_evidence.py --live runtime/p4-compliance/evidence.json

P4's process *is* a DCR graph, so its compliance rules are that graph without its
role assignments: compliance/p4/rules.xml must equal dcr/p4_quality_investigation.xml
with the <roles> elements removed (same events, relations and marking). ProcessDoctor
reported every trace as violating (cost Infinity) against the model with roles even when
the log carried the DCR roles, and 4/4 conforming against the role-free model.

Two kinds of execution evidence are kept apart:

- dcrgraphs-simulation-log.xes: the four manual runs executed in the dcrgraphs.net
  simulator by the P4 owner, transcribed from the simulator's trace view
  (screenshots/p4_trace_run*.png). The simulator export was not available on our
  subscription, so event times were not exported and are omitted.
- execution-log.xes: written only with --live, from the evidence JSON produced by
  stage_tests/check_p4_investigation.py against a running stack. It keeps the DCR
  activities of each incident (one trace per incident).

Synthetic forbidden traces (invalid-traces.json) must be rejected. The result is
written to verification.json; an existing live section is preserved unless --live
replaces it.
"""

import argparse
import re
import hashlib
import json
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compliance.check import export_xes, read_traces, replay  # noqa: E402

HERE = Path(__file__).parent
RULES = HERE / "rules.xml"
SOURCE_MODEL = ROOT / "dcr/p4_quality_investigation.xml"
DCR_ACTIVITIES = {
    "OpenIncident", "AddAlertEvidence", "ReviewTemperatureLog", "ReviewWeatherData",
    "ReviewRouteAndETA", "RequestCoolingAdjustment", "RecordCoolingAcknowledgement",
    "RecordCoolingFailure", "RecordRecoveryEvidence", "RequestAdditionalCheck",
    "CompleteAdditionalCheck", "AssessEvidence", "DecideRelease", "DecideReplace",
    "DecideReturn", "CloseInvestigation",
}

# Transcribed from the dcrgraphs.net trace views (screenshots/p4_trace_run*.png).
SIMULATION_RUNS = {
    "dcrgraphs-run1-cooling-recovery-release": [
        "OpenIncident", "ReviewWeatherData", "RequestCoolingAdjustment",
        "ReviewTemperatureLog", "RecordCoolingAcknowledgement", "RecordRecoveryEvidence",
        "AssessEvidence", "DecideRelease", "CloseInvestigation"],
    "dcrgraphs-run2-cooling-failure-replace": [
        "OpenIncident", "ReviewTemperatureLog", "RequestCoolingAdjustment",
        "RecordCoolingFailure", "AssessEvidence", "DecideReplace", "CloseInvestigation"],
    "dcrgraphs-run3-additional-check-return": [
        "OpenIncident", "ReviewRouteAndETA", "ReviewTemperatureLog",
        "RequestAdditionalCheck", "CompleteAdditionalCheck", "AssessEvidence",
        "DecideReturn", "CloseInvestigation"],
    # Run 4 was stopped after the new alert to show that the decisions are withdrawn
    # until the evidence is assessed again (screenshots/p4_sim_run4_reassess.png).
    "dcrgraphs-run4-new-alert-partial": [
        "OpenIncident", "ReviewTemperatureLog", "AssessEvidence", "AddAlertEvidence"],
}
EXPECTED_PARTIAL = {"dcrgraphs-run4-new-alert-partial": ["AssessEvidence",
                                                         "CloseInvestigation"]}


def without_roles(text):
    return re.sub(r"\s*<roles>.*?</roles>", "", text, flags=re.S)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_simulation_log(path):
    root = ET.Element("log", {"xes.version": "1.0", "xmlns": "http://www.xes-standard.org/"})
    ET.SubElement(root, "extension", {"name": "Concept", "prefix": "concept",
                                      "uri": "http://www.xes-standard.org/concept.xesext"})
    ET.SubElement(root, "string", {"key": "source", "value":
                  "dcrgraphs.net simulator, manual runs, transcribed from trace view"})
    for case, activities in SIMULATION_RUNS.items():
        trace = ET.SubElement(root, "trace")
        ET.SubElement(trace, "string", {"key": "concept:name", "value": case})
        for activity in activities:
            event = ET.SubElement(trace, "event")
            ET.SubElement(event, "string", {"key": "concept:name", "value": activity})
            ET.SubElement(event, "string", {"key": "inputMode", "value": "manual"})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    add_roles(path)


def model_roles():
    """DCR role of every activity, as declared in the executed graph."""
    root = ET.parse(SOURCE_MODEL).getroot()
    return {e.get("id"): e.findtext("./custom/roles/role")
            for e in root.findall("./specification/resources/events/event")}


def add_roles(path):
    """ProcessDoctor checks the performer against the model's roles.

    P4's graph assigns roles (Integration, QualityOfficer, Receiver), so every event
    carries its DCR role as org:resource and org:role; the person or system that
    performed it is kept as `actor`.
    """
    ET.register_namespace("", "http://www.xes-standard.org/")
    tree = ET.parse(path)
    root = tree.getroot()
    ns = "{http://www.xes-standard.org/}"
    roles = model_roles()
    if not any(x.get("prefix") == "org" for x in root.findall(f"{ns}extension")):
        last = max(i for i, x in enumerate(root) if x.tag == f"{ns}extension")
        root.insert(last + 1, ET.Element(f"{ns}extension", {
            "name": "Organization", "prefix": "org",
            "uri": "http://www.xes-standard.org/org.xesext"}))
    for event in root.iter(f"{ns}event"):
        fields = {x.get("key"): x for x in event.findall(f"{ns}string")}
        role = roles[fields["concept:name"].get("value")]
        resource = fields.get("org:resource")
        if resource is not None:
            if "actor" not in fields:
                ET.SubElement(event, f"{ns}string", {"key": "actor",
                                                    "value": resource.get("value")})
            resource.set("value", role)
        else:
            ET.SubElement(event, f"{ns}string", {"key": "org:resource", "value": role})
        if "org:role" in fields:
            fields["org:role"].set("value", role)
        else:
            ET.SubElement(event, f"{ns}string", {"key": "org:role", "value": role})
    ET.indent(root, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def live_cases(evidence):
    cases = {}
    for case in evidence["cases"]:
        events = [e for e in case["events"] if e.get("process") == "P4"
                  and e.get("incidentId") == case["incidentId"]
                  and e["eventType"] in DCR_ACTIVITIES]
        cases[f"{case['shipmentId']}/{case['incidentId']}"] = events
    return cases


def run_offline_tests():
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "stage_tests", "-p",
         "test_p4.py"], cwd=ROOT, capture_output=True, text=True)
    summary = [line for line in result.stderr.splitlines() if line.startswith("Ran ")]
    return {"command": ".venv/bin/python -m unittest discover -s stage_tests -p test_p4.py",
            "result": "PASS" if result.returncode == 0 else "FAIL",
            "summary": summary[-1] if summary else result.stderr[-300:]}


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", type=Path,
                        help="evidence JSON from stage_tests/check_p4_investigation.py")
    args = parser.parse_args()
    if RULES.read_text() != without_roles(SOURCE_MODEL.read_text()):
        raise SystemExit("compliance/p4/rules.xml must be dcr/p4_quality_investigation.xml "
                         "without <roles>")
    report_path = HERE / "verification.json"
    previous = json.loads(report_path.read_text()) if report_path.exists() else {}

    simulation_log = HERE / "dcrgraphs-simulation-log.xes"
    write_simulation_log(simulation_log)
    simulation = {case: replay(RULES, events)
                  for case, events in read_traces(simulation_log).items()}
    for case, result in simulation.items():
        expected_pending = EXPECTED_PARTIAL.get(case, [])
        if not result["valid"] or sorted(result.get("pending", [])) != expected_pending:
            raise SystemExit(f"simulation run {case} does not replay as recorded: {result}")

    invalid = json.loads((HERE / "invalid-traces.json").read_text())
    forbidden = {name: replay(RULES, trace) for name, trace in invalid.items()
                 if isinstance(trace, list)}
    if any(result["valid"] for result in forbidden.values()):
        raise SystemExit(f"a forbidden trace was accepted: {forbidden}")

    live = previous.get("liveExecution", {"status": "PENDING", "reason": (
        "Not yet run. Start a fresh stack and run stage_tests/check_p4_investigation.py, "
        "then this builder with --live.")})
    if args.live:
        evidence = json.loads(args.live.read_text())
        cases = live_cases(evidence)
        export_xes(HERE / "execution-log.xes", cases)
        add_roles(HERE / "execution-log.xes")
        replays = {case: replay(RULES, [e["eventType"] for e in events])
                   for case, events in cases.items()}
        if not all(r["valid"] and r["accepting"] for r in replays.values()):
            raise SystemExit(f"live log does not conform: {replays}")
        live = {
            "status": "PASS",
            "command": ".venv/bin/python stage_tests/check_p4_investigation.py",
            "capturedAt": evidence["capturedAt"], "runId": evidence["runId"],
            "dcrEngine": evidence["health"]["integrations"]["p4DcrEngine"],
            "source": evidence["source"],
            "cases": [{k: c[k] for k in (
                "scenario", "shipmentId", "incidentId", "trigger", "evidence", "decision",
                "unappliedDecisions", "reassessedVersions", "receiptWhileHeld",
                "monitoringFinalization", "qualityStatus", "p3Status", "refusedActions",
                "processInstanceKeys") if k in c}
                      for c in evidence["cases"]],
            "replay": replays,
            "log": "execution-log.xes (org:resource/org:role = DCR role; actor = performer)",
        }
        old = previous.get("liveExecution", {})
        if old.get("runId") == evidence["runId"]:
            for key in ("command", "durationSeconds", "environment", "notes"):
                if key in old:
                    live[key] = old[key]

    tracked = [RULES, SOURCE_MODEL, simulation_log, HERE / "invalid-traces.json",
               ROOT / "deployer/quality.py", ROOT / "deployer/dcr_client.py",
               ROOT / "stage_tests/test_p4.py",
               ROOT / "stage_tests/check_p4_investigation.py"]
    if (HERE / "execution-log.xes").exists():
        tracked.append(HERE / "execution-log.xes")
    report = {
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "testedCommit": git_commit(),
        "process": "P4 Quality incident investigation (DCR)",
        "owner": "Abid Hasan",
        "contract": "foundation P4 assessment contract v1 (docs/P4-FOUNDATION-HANDOVER.md)",
        "builderEnvironment": {"python": platform.python_version(),
                               "host": f"{platform.system()} {platform.machine()}"},
        "sha256": {str(p.relative_to(ROOT)): sha256(p) for p in tracked},
        "model": {"events": 16, "flatRelations": 83, "drawnArrows": 34, "groups": 3,
                  "reachableMarkings": 3425,
                  "deadlockFree": "every reachable marking can reach an accepting one",
                  "noDeadActivities": True,
                  "checkedBy": "stage_tests/test_p4.py::P4Graph"},
        "offlineTests": run_offline_tests(),
        "dcrgraphsSimulation": {
            "status": "PASS",
            "where": "dcrgraphs.net simulator, one user holding all three roles",
            "evidence": "screenshots/p4_trace_run*.png, p4_sim_*.png",
            "note": "Event times were not exported; run 4 is intentionally partial.",
            "replay": simulation,
        },
        "forbiddenTraces": {name: result["reason"] for name, result in forbidden.items()},
        "liveExecution": live,
        "historical": previous.get("historical"),
        "dcrRepositoryApi": previous.get("dcrRepositoryApi", {
            "status": "PENDING",
            "reason": "Needs the team's dcrgraphs.net credentials in an untracked .env; "
                      "the repository export/API was not available on our subscription."}),
        "processDoctor": previous.get("processDoctor", {
            "status": "PENDING",
            "reason": "Upload rules.xml with the live execution-log.xes once it exists."}),
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("offlineTests", "liveExecution")} | {
        "simulationRuns": len(simulation), "forbiddenTracesRejected": len(forbidden)},
        indent=2)[:2000])


if __name__ == "__main__":
    main()
