"""Check that the P4 graph runs in the dcrgraphs.net Active Repository (repository mode).

Reads DCR_GRAPH_ID, DCR_USERNAME and DCR_PASSWORD from the environment or from the
untracked .env file in the repository root (never commit it, never print it). It starts
one simulation of the graph through the repository API, walks the release path of the
investigation and compares, after every step, the enabled and pending activities with
P4's local engine on dcr/p4_quality_investigation.xml. It also tries one forbidden
action, which the repository must refuse. Standard library only:

    python3 stage_tests/check_dcr_repository.py

Writes runtime/p4-compliance/dcr-repository.json (no credentials).
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deployer"))
from dcr_client import DCRClient, DCRUnavailable, LocalDCR, model_activities  # noqa: E402

MODEL = ROOT / "dcr/p4_quality_investigation.xml"
PATH = ["OpenIncident", "ReviewWeatherData", "RequestCoolingAdjustment",
        "ReviewTemperatureLog", "RecordCoolingAcknowledgement", "RecordRecoveryEvidence",
        "AssessEvidence", "DecideRelease", "CloseInvestigation"]
ROLES = {"OpenIncident": "Integration", "RecordCoolingAcknowledgement": "Integration",
         "RecordRecoveryEvidence": "Integration"}


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def state(engine, sim):
    data = engine.events(sim)
    enabled = sorted(e["id"] for e in data["events"] if e["enabled"])
    pending = sorted(e["id"] for e in data["events"] if e["pending"] and e.get("included", True))
    return dict(enabled=enabled, pending=pending, accepting=data["isAccepting"])


def execute(client, sim, activity):
    try:
        client.execute(sim, activity)
        return "executed"
    except DCRUnavailable as first:
        role = ROLES.get(activity, "QualityOfficer")
        try:
            client.execute(sim, activity, role=role)
            return f"executed with role {role}"
        except DCRUnavailable:
            raise first


def main():
    load_env()
    client = DCRClient(activities=model_activities(MODEL))  # ignores nesting groups
    missing = [k for k in ("DCR_GRAPH_ID", "DCR_USERNAME", "DCR_PASSWORD") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Fill in {', '.join(missing)} in .env first")
    result = dict(checkedAt=datetime.now(timezone.utc).isoformat(), graphId=client.graph_id,
                  api=client.base_url, steps=[], status="FAIL")
    out = ROOT / "runtime/p4-compliance/dcr-repository.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as directory:
            local = LocalDCR(MODEL, Path(directory) / "local.sqlite")
            sim, lsim = client.create_simulation("FrostFlow P4 API check"), local.create_simulation()
            result["simulationId"] = sim
            remote = state(client, sim)
            expected = state(local, lsim)
            result["initial"] = dict(repository=remote, local=expected)
            if remote["enabled"] != expected["enabled"]:
                raise SystemExit(f"initial enabled activities differ: {remote} vs {expected}")
            try:
                client.execute(sim, "CloseInvestigation")
                result["forbiddenCloseAtStart"] = "EXECUTED (should have been refused)"
            except DCRUnavailable as error:
                result["forbiddenCloseAtStart"] = f"refused: {str(error)[:120]}"
            for activity in PATH:
                how = execute(client, sim, activity)
                local.execute(lsim, activity)
                remote, expected = state(client, sim), state(local, lsim)
                same = remote["enabled"] == expected["enabled"] and \
                    remote["pending"] == expected["pending"]
                result["steps"].append(dict(activity=activity, how=how, matches=same,
                                            repository=remote, local=expected))
                print(("OK  " if same else "DIFF"), activity, "|", how, flush=True)
                if not same:
                    raise SystemExit(f"after {activity}: repository {remote} vs local {expected}")
            refused = result["forbiddenCloseAtStart"].startswith("refused")
            result["status"] = "PASS" if remote["accepting"] and refused else "FAIL"
    except DCRUnavailable as error:
        result["error"] = str(error)[:300]
        out.write_text(json.dumps(result, indent=2) + "\n")
        raise SystemExit(f"DCR repository not usable: {error}\n(401/403 = account has no API access)")
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("status", "graphId", "simulationId",
                                              "forbiddenCloseAtStart")}, indent=2))
    print(f"Details: {out}")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
