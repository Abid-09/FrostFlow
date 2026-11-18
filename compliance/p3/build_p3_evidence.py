"""Build and locally verify P3 DCR compliance evidence.

Usage from the repository root:

    python compliance/p3/build_p3_evidence.py rules
    python compliance/p3/build_p3_evidence.py log runtime/p3-compliance/evidence.json
    python compliance/p3/build_p3_evidence.py verify compliance/p3/execution-log.xes

The JSON input to ``log`` must be captured from real P3 executions by
stage_tests/check_p3_compliance.py. The log builder does not synthesize events.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compliance.check import read_traces, replay  # noqa: E402

HERE = Path(__file__).parent
RULES = HERE / "rules.xml"
LOG = HERE / "execution-log.xes"
VERIFICATION = HERE / "verification.json"

EVENTS = [
    ("ShipmentDispatched", 60, 170),
    ("ShipmentArrived", 320, 170),
    ("GoodsReceived", 580, 170),
    ("InspectionPassed", 840, 60),
    ("InspectionFailed", 840, 300),
    ("MonitoringFinalized", 1090, 60),
    ("StockAdmitted", 1360, 60),
    ("GoodsRejected", 1360, 300),
    ("QualityHoldRaised", 580, 520),
    ("StockAdmissionHeld", 840, 520),
    ("QualityHoldReleased", 1080, 520),
    ("QualityRejected", 1360, 520),
]
NAMES = [name for name, _, _ in EVENTS]

CONDITIONS = [
    ("MonitoringFinalized", "StockAdmitted"),
    ("ShipmentDispatched", "ShipmentArrived"),
    ("ShipmentArrived", "GoodsReceived"),
    ("GoodsReceived", "InspectionPassed"),
    ("GoodsReceived", "InspectionFailed"),
    ("InspectionPassed", "StockAdmitted"),
    ("InspectionPassed", "GoodsRejected"),
    ("InspectionFailed", "GoodsRejected"),
    ("QualityHoldRaised", "QualityHoldReleased"),
    ("QualityHoldRaised", "QualityRejected"),
    ("QualityHoldRaised", "StockAdmissionHeld"),
    ("InspectionPassed", "StockAdmissionHeld"),
    ("StockAdmissionHeld", "StockAdmitted"),
    ("QualityHoldReleased", "StockAdmitted"),
    ("QualityRejected", "GoodsRejected"),
]
RESPONSES = [
    ("ShipmentDispatched", "ShipmentArrived"),
    ("ShipmentArrived", "GoodsReceived"),
    ("GoodsReceived", "InspectionPassed"),
    ("GoodsReceived", "InspectionFailed"),
    ("InspectionPassed", "StockAdmitted"),
    ("InspectionPassed", "GoodsRejected"),
    ("InspectionFailed", "GoodsRejected"),
    ("QualityHoldRaised", "QualityHoldReleased"),
    ("QualityHoldRaised", "QualityRejected"),
    ("StockAdmissionHeld", "QualityHoldReleased"),
    ("StockAdmissionHeld", "QualityRejected"),
    ("QualityHoldReleased", "StockAdmitted"),
    ("QualityRejected", "GoodsRejected"),
]
INCLUDES = [
    ("QualityHoldRaised", "QualityHoldReleased"),
    ("QualityHoldRaised", "QualityRejected"),
    ("QualityHoldRaised", "StockAdmissionHeld"),
    ("QualityHoldReleased", "StockAdmitted"),
    ("QualityRejected", "GoodsRejected"),
    ("InspectionFailed", "GoodsRejected"),
]
EXCLUDES = [
    ("InspectionPassed", "InspectionFailed"),
    ("InspectionFailed", "InspectionPassed"),
    ("InspectionFailed", "StockAdmitted"),
    ("QualityHoldRaised", "StockAdmitted"),
    ("QualityHoldReleased", "QualityRejected"),
    ("QualityRejected", "QualityHoldReleased"),
    ("QualityRejected", "StockAdmitted"),
    ("StockAdmissionHeld", "StockAdmissionHeld"),
    ("StockAdmissionHeld", "StockAdmitted"),
    ("QualityHoldRaised", "QualityHoldRaised"),
    ("QualityHoldReleased", "QualityHoldReleased"),
    ("QualityRejected", "QualityRejected"),
    ("QualityHoldReleased", "StockAdmissionHeld"),
    ("QualityRejected", "StockAdmissionHeld"),
    ("StockAdmitted", "GoodsRejected"),
    ("StockAdmitted", "QualityHoldRaised"),
    ("StockAdmitted", "QualityHoldReleased"),
    ("StockAdmitted", "QualityRejected"),
    ("StockAdmitted", "InspectionFailed"),
    ("GoodsRejected", "StockAdmitted"),
    ("GoodsRejected", "QualityHoldRaised"),
    ("GoodsRejected", "QualityHoldReleased"),
    ("GoodsRejected", "QualityRejected"),
    ("GoodsRejected", "InspectionPassed"),
    ("GoodsRejected", "InspectionFailed"),
]

SCENARIOS = {
    "normal-receipt-admission": {"InspectionPassed", "StockAdmitted"},
    "quality-hold-release": {
        "QualityHoldRaised",
        "StockAdmissionHeld",
        "QualityHoldReleased",
        "InspectionPassed",
        "StockAdmitted",
    },
    "inspection-rejection": {"InspectionFailed", "GoodsRejected"},
    "quality-rejection": {
        "QualityHoldRaised",
        "StockAdmissionHeld",
        "QualityRejected",
        "InspectionPassed",
        "GoodsRejected",
    },
}


def build_rules(path=RULES):
    graph = ET.Element("dcrgraph", title="P3 receiving and stock admission compliance",
                       dataTypesStatus="hide")
    spec = ET.SubElement(graph, "specification")
    resources = ET.SubElement(spec, "resources")
    events = ET.SubElement(resources, "events")
    for name, x, y in EVENTS:
        event = ET.SubElement(events, "event", id=name)
        custom = ET.SubElement(event, "custom")
        ET.SubElement(custom, "eventData")
        visual = ET.SubElement(custom, "visualization")
        ET.SubElement(visual, "location", xLoc=str(x), yLoc=str(y))
        ET.SubElement(visual, "size", width="130", height="150")
    labels = ET.SubElement(resources, "labels")
    mappings = ET.SubElement(resources, "labelMappings")
    for name in NAMES:
        ET.SubElement(labels, "label", id=name)
        ET.SubElement(mappings, "labelMapping", eventId=name, labelId=name)
    ET.SubElement(resources, "subProcesses")
    constraints = ET.SubElement(spec, "constraints")
    for plural, singular, pairs in [
        ("conditions", "condition", CONDITIONS),
        ("responses", "response", RESPONSES),
        ("includes", "include", INCLUDES),
        ("excludes", "exclude", EXCLUDES),
    ]:
        group = ET.SubElement(constraints, plural)
        for source, target in pairs:
            ET.SubElement(group, singular, sourceId=source, targetId=target)
    marking = ET.SubElement(ET.SubElement(graph, "runtime"), "marking")
    ET.SubElement(marking, "executed")
    included = ET.SubElement(marking, "included")
    for name in NAMES:
        if name not in (
            "QualityHoldReleased",
            "QualityRejected",
            "StockAdmissionHeld",
        ):
            ET.SubElement(included, "event", id=name)
    ET.SubElement(marking, "pendingResponses")
    ET.indent(graph, space="  ")
    ET.ElementTree(graph).write(path, encoding="utf-8", xml_declaration=True)
    return Path(path)


def event_activity(event):
    event_type = event.get("eventType")
    payload = event.get("payload", {})
    if event_type == "GoodsInspected":
        return "InspectionPassed" if payload.get("passed") is True else "InspectionFailed"
    if event_type == "StockAdmissionHeld" and payload.get("qualityStatus") == "PENDING":
        return "StockAdmissionHeld"
    if event_type == "QualityHoldEstablished":
        return "QualityHoldRaised"
    if event_type == "QualityStatusChanged":
        return {
            "PENDING": "QualityHoldRaised",
            "RELEASED": "QualityHoldReleased",
            "REJECTED": "QualityRejected",
        }.get(payload.get("qualityStatus"))
    if event_type in {
        "ShipmentDispatched",
        "ShipmentArrived",
        "GoodsReceived",
        "StockAdmitted",
        "MonitoringFinalized",
        "GoodsRejected",
    }:
        return event_type
    return None


def export_p3_xes(evidence_path, path=LOG):
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    cases = evidence.get("cases")
    if (
        evidence.get("source")
        != "live HTTP integration test and P1/P2/P3 durable registry backed by Zeebe"
        or not evidence.get("runId")
        or not evidence.get("capturedAt")
        or not isinstance(cases, list)
    ):
        raise ValueError("evidence must be a timestamped capture from the live P3 runner")
    if len(cases) != len(SCENARIOS):
        raise ValueError("evidence must contain exactly one live run for each required scenario")
    found = set()
    root = ET.Element("log", {"xes.version": "1.0", "xmlns": "http://www.xes-standard.org/"})
    for prefix, uri, name in [
        ("concept", "http://www.xes-standard.org/concept.xesext", "Concept"),
        ("time", "http://www.xes-standard.org/time.xesext", "Time"),
        ("org", "http://www.xes-standard.org/org.xesext", "Organization"),
    ]:
        ET.SubElement(root, "extension", {"name": name, "prefix": prefix, "uri": uri})
    ET.SubElement(
        root,
        "string",
        {
            "key": "source",
            "value": "Accepted P4-test inputs and live P1/P2/P3 registry event history",
        },
    )
    for case in cases:
        scenario = case.get("scenario")
        shipment_id = case.get("shipmentId")
        source_events = case.get("events")
        if scenario not in SCENARIOS or not shipment_id or not isinstance(source_events, list):
            raise ValueError("each evidence case needs a known scenario, shipmentId and events list")
        if scenario in found:
            raise ValueError(f"duplicate execution scenario: {scenario}")
        if not all(case.get(key) for key in (
            "p1ProcessInstanceKey", "p2ProcessInstanceKey", "p3ProcessInstanceKey"
        )):
            raise ValueError(f"{scenario} lacks live P1/P2/P3 process instance keys")
        expected_status = (
            "ADMITTED"
            if scenario in ("normal-receipt-admission", "quality-hold-release")
            else "REJECTED"
        )
        if case.get("expectedFinalStatus") != expected_status:
            raise ValueError(f"{scenario} has an unexpected final process status")
        processes = {event.get("process") for event in source_events}
        if not {"P1", "P2", "P3"}.issubset(processes):
            raise ValueError(f"{scenario} lacks neighboring P1/P2/P3 events")
        all_events = source_events + case.get("acceptedIntegrationEvents", [])
        if scenario in ("quality-hold-release", "quality-rejection") and not any(
            event.get("eventType") == "QualityStatusChanged"
            and event.get("payload", {}).get("qualityStatus") == "PENDING"
            for event in all_events
        ):
            raise ValueError(f"{scenario} lacks the accepted PENDING quality event")
        found.add(scenario)
        trace = ET.SubElement(root, "trace")
        ET.SubElement(trace, "string", {"key": "concept:name", "value": f"{scenario}|{shipment_id}"})
        ET.SubElement(trace, "string", {"key": "shipmentId", "value": shipment_id})
        ET.SubElement(trace, "string", {"key": "scenario", "value": scenario})
        mapped = []
        for source_event in all_events:
            activity = event_activity(source_event)
            if activity is not None:
                mapped.append((source_event, activity))
        mapped.sort(key=lambda pair: (pair[0].get("occurredAt", ""), pair[0].get("recordedAt", "")))
        for source_event, activity in mapped:
            event = ET.SubElement(trace, "event")
            values = {
                "concept:name": activity,
                "time:timestamp": source_event.get("occurredAt", ""),
                "eventId": source_event.get("eventId", ""),
                "sourceEventType": source_event.get("eventType", ""),
                "process": source_event.get("process", ""),
                "processInstanceKey": source_event.get("processInstanceKey") or "",
                "shipmentId": shipment_id,
            }
            if source_event.get("actor"):
                values["org:resource"] = str(source_event["actor"])
            payload = source_event.get("payload", {})
            for key in ("qualityStatus", "incidentId", "productId", "batchId"):
                if payload.get(key) is not None:
                    values[key] = str(payload[key])
            for key, value in values.items():
                tag = "date" if key == "time:timestamp" else "string"
                ET.SubElement(event, tag, {"key": key, "value": str(value)})
    missing = set(SCENARIOS) - found
    if missing:
        raise ValueError("missing actual execution scenarios: " + ", ".join(sorted(missing)))
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return Path(path)


def verify_log(path=LOG, output=VERIFICATION):
    traces = read_traces(Path(path))
    rules = RULES
    if not rules.exists():
        build_rules(rules)
    replay_results = {case.split("|", 1)[0]: replay(rules, events) for case, events in traces.items()}
    if len(traces) != len(SCENARIOS) or set(replay_results) != set(SCENARIOS):
        raise ValueError("XES must contain exactly the four required P3 scenario names")
    scenario_coverage = {}
    for case, events in traces.items():
        scenario = case.split("|", 1)[0]
        missing = SCENARIOS[scenario] - set(events)
        if missing:
            raise ValueError(f"{scenario} lacks expected events: {', '.join(sorted(missing))}")
        if not replay_results[scenario]["valid"] or not replay_results[scenario]["accepting"]:
            raise ValueError(f"{scenario} does not conform: {replay_results[scenario]}")
        scenario_coverage[scenario] = {"events": events, "valid": True, "accepting": True}

    forbidden = replay(
        rules,
        [
            "ShipmentDispatched",
            "ShipmentArrived",
            "QualityHoldRaised",
            "GoodsReceived",
            "InspectionPassed",
            "StockAdmitted",
        ],
    )
    if forbidden["valid"] or forbidden.get("event") != "StockAdmitted":
        raise ValueError("forbidden admission during an active quality hold was not rejected")
    result = {
        "source": str(Path(path)),
        "localReplay": {"status": "passed", "scenarios": scenario_coverage},
        "forbiddenAdmission": {
            "status": "rejected-as-required",
            "attempt": "StockAdmitted while QualityHoldRaised remains unresolved",
            "result": forbidden,
        },
        "processDoctor": {
            "status": "pending",
            "note": "Import rules.xml and execution-log.xes into ProcessDoctor and record the actual result before marking P3 compliance complete.",
        },
    }
    Path(output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    if sys.argv[1:2] == ["rules"] and len(sys.argv) == 2:
        print(build_rules())
    elif sys.argv[1:2] == ["log"] and len(sys.argv) == 3:
        print(export_p3_xes(sys.argv[2]))
    elif sys.argv[1:2] == ["verify"] and len(sys.argv) in (3, 4):
        result = verify_log(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else VERIFICATION)
        print(json.dumps(result, indent=2))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
