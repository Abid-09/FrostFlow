"""Check the Siddhi delivery-risk persistence rule in isolation.

Starts a throwaway Siddhi runner with siddhi-apps/ColdChainTelemetry.siddhi,
sends crafted ETAUpdated readings and reads the COLD-CHAIN-RISK log sink.
Only Docker is required; the application stack does not need to run.

Rule under test: per shipment and route revision, three consecutive ETA
readings that are each late (after the deadline) and fresh (computed at most
30 s before evaluation), within 180 s, raise exactly one alert. An isolated
late reading, stale readings, or a run broken by an unavailable or on-time
reading must not alert. Readings from other shipments must not interfere.

Usage (repository root):  python stage_tests/check_siddhi_risk_rule.py
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONTAINER = "siddhi-risk-rule-check"
PORT = 18006
APPS = Path(__file__).resolve().parents[1] / "siddhi-apps"
IMAGE = "siddhiio/siddhi-runner-alpine:latest"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def logs():
    result = docker("logs", CONTAINER, check=False)
    return result.stdout + result.stderr


def send(event_id, shipment, revision, deadline, late=True, stale=False, unavailable=False):
    now = int(time.time() * 1000)
    payload = {
        "deliveryDeadline": "2026-11-16T12:00:00Z", "deliveryDeadlineMillis": deadline,
        "routeId": "route-01", "routeRevision": revision,
        "observedAtMillis": now - (120_000 if stale else 0),
    }
    if not unavailable:
        payload["eta"] = "2026-11-16T12:10:00Z"
        payload["etaMillis"] = deadline + (600_000 if late else -600_000)
    body = {"event": {"eventId": event_id, "eventType": "ETAUpdated", "shipmentId": shipment,
                      "journeyId": "J-" + shipment, "occurredAt": "2026-11-16T12:00:00Z",
                      "payload": payload}}
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/telemetry", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=5).read()
    time.sleep(0.3)


def main():
    docker("rm", "-f", CONTAINER, check=False)
    docker("run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{PORT}:8006",
           "-v", f"{APPS}:/apps:ro", IMAGE, "-Dapps=/apps/ColdChainTelemetry.siddhi")
    try:
        end = time.monotonic() + 120
        while "Siddhi Runner Distribution started" not in logs():
            if time.monotonic() > end:
                raise AssertionError("Siddhi runner did not start")
            time.sleep(2)
        assert "ColdChainTelemetry deployed successfully" in logs(), "Siddhi app failed to deploy"

        deadline = int(time.time() * 1000) - 60_000
        # 1. One isolated late reading, then on-time readings: no alert.
        send("ISO-1", "S-ISO", 0, deadline)
        send("ISO-2", "S-ISO", 0, deadline, late=False)
        send("ISO-3", "S-ISO", 0, deadline, late=False)
        # 2. Three late but stale readings: no alert.
        for i in range(3):
            send(f"STALE-{i}", "S-STALE", 0, deadline, stale=True)
        # 3. Late, unavailable, late, late: the gap breaks the run, no alert.
        send("GAP-1", "S-GAP", 0, deadline)
        send("GAP-2", "S-GAP", 0, deadline, unavailable=True)
        send("GAP-3", "S-GAP", 0, deadline)
        send("GAP-4", "S-GAP", 0, deadline)
        # Retries of one observation cannot satisfy the persistence count.
        for _ in range(4):
            send("DUP-1", "S-DUP", 0, deadline)
        time.sleep(1)
        assert "SIDDHI:RISK:DUP-1" not in logs(), logs()
        send("DUP-2", "S-DUP", 0, deadline)
        send("DUP-3", "S-DUP", 0, deadline)
        send("DUP-3", "S-DUP", 0, deadline)
        # 4. Three fresh late readings, another shipment interleaved: one alert.
        send("OK-1", "S-OK", 0, deadline)
        send("NOISE-1", "S-NOISE", 0, deadline, late=False)
        send("OK-2", "S-OK", 0, deadline)
        send("OK-3", "S-OK", 0, deadline)
        # 5. A fourth late reading on the same revision: still one alert.
        send("OK-4", "S-OK", 0, deadline)
        # 6. A new route revision with three fresh late readings: one more alert.
        for i in range(3):
            send(f"REV1-{i}", "S-OK", 1, deadline)
        time.sleep(4)

        alerts = re.findall(r"COLD-CHAIN-RISK : Event\{[^]]*data=\[([^,]+), ([^,]+),", logs())
        assert alerts == [("S-DUP", "SIDDHI:RISK:DUP-3"), ("S-OK", "SIDDHI:RISK:OK-3"), ("S-OK", "SIDDHI:RISK:REV1-2")], alerts
        print("PASS - duplicate, isolated, stale and gap-broken late ETAs raised no alert; three "
              "consecutive fresh late ETAs raised exactly one alert per route revision")
    finally:
        docker("rm", "-f", CONTAINER, check=False)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print("FAIL:", error)
        sys.exit(1)
