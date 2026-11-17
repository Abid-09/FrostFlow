"""Small HTTP adapters for transport telemetry, TomTom ETA and Siddhi."""

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


# Scripted demo geography. Every route is a corridor from the depot (or, after an
# approved change, from the vehicle's current position) through its own via point
# to the RECIPIENT's location, so changing route never changes the destination.
DEPOT = [48.8566, 2.3522]
DESTINATIONS = {
    "C-01": [48.8698, 2.2947],
    "C-02": [48.8347, 2.2872],
    "C-03": [48.8763, 2.2989],
}
CORRIDOR_VIA = {
    "route-01": [48.8650, 2.3250],  # northern corridor
    "route-02": [48.8450, 2.3200],  # southern corridor
    "route-03": [48.8600, 2.3330],  # central corridor
}
LEGS_TO_VIA = 3
LEGS_TO_DESTINATION = 4


def _between(start, end, fraction):
    return [round(start[0] + (end[0] - start[0]) * fraction, 6),
            round(start[1] + (end[1] - start[1]) * fraction, 6)]


def plan_route(route_id, recipient_id, start=None):
    """Waypoints from start (default: depot) via the route's corridor to the recipient.

    Raises ValueError for an unknown route or recipient, so an incompatible
    route cannot be planned at all.
    """
    if route_id not in CORRIDOR_VIA:
        raise ValueError(f"unknown route {route_id}")
    if recipient_id not in DESTINATIONS:
        raise ValueError(f"no delivery location is defined for recipient {recipient_id}")
    start = list(start or DEPOT)
    via, destination = CORRIDOR_VIA[route_id], DESTINATIONS[recipient_id]
    points = [_between(start, via, i / LEGS_TO_VIA) for i in range(LEGS_TO_VIA + 1)]
    points += [_between(via, destination, j / LEGS_TO_DESTINATION) for j in range(1, LEGS_TO_DESTINATION + 1)]
    points[0], points[-1] = start, list(destination)
    return points


def post_json(url, payload, headers=None, timeout=5):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}


class TransportIntegrations:
    """Uses configured services when present and deterministic demo defaults otherwise."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        registry = self.environ.get("REGISTRY_PATH")
        # Provenance log for live TomTom responses; lives beside the (git-ignored)
        # registry and never contains the API key.
        self.tomtom_log = (
            os.path.join(os.path.dirname(registry) or ".", "tomtom-responses.jsonl")
            if registry else None
        )

    def route(self, route_id, recipient_id, start=None):
        return plan_route(route_id, recipient_id, start)

    def eta(self, remaining_points, route_id, route_index):
        """ETA along the remaining waypoints (current position first) to the destination."""
        points = [list(p) for p in remaining_points]
        if len(points) < 2:
            # Already at the last route point: nothing left to route.
            return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "at-destination"
        key = self.environ.get("TOMTOM_API_KEY")
        if key and len(points) >= 2:
            seconds = self._tomtom_seconds(key, points, route_id, route_index)
            if seconds is None:
                # Failed or unusable provider data is reported as unavailable,
                # never replaced by an estimate that could look on time.
                return None, "tomtom-unavailable"
            source = "tomtom-traffic"
        else:
            meters = sum(self._meters(a, b) for a, b in zip(points, points[1:]))
            seconds = max(30, round(meters / 8.3))
            source = "local-demo-estimate"
        eta = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return eta.isoformat().replace("+00:00", "Z"), source

    def _tomtom_seconds(self, key, points, route_id, route_index):
        """Return traffic-aware travel seconds, or None when TomTom is unavailable."""
        locations = ":".join(f"{lat},{lon}" for lat, lon in points)
        query = urllib.parse.urlencode({
            "key": key, "traffic": "true", "computeTravelTimeFor": "all"
        })
        url = f"https://api.tomtom.com/routing/1/calculateRoute/{locations}/json?{query}"
        entry = {
            "requestedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provider": "tomtom-routing-v1", "routeId": route_id,
            "routeIndex": route_index, "waypoints": points,
        }
        seconds = None
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                entry["httpStatus"] = response.status
                summary = json.load(response)["routes"][0]["summary"]
            entry["summary"] = {k: summary.get(k) for k in (
                "lengthInMeters", "travelTimeInSeconds", "noTrafficTravelTimeInSeconds",
                "trafficDelayInSeconds", "departureTime", "arrivalTime")}
            value = summary.get("travelTimeInSeconds")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                seconds = round(value)
            else:
                entry["error"] = "response has no usable travelTimeInSeconds"
        except Exception as error:  # network, HTTP 4xx/5xx, malformed JSON
            entry["error"] = f"{type(error).__name__}: {error}".replace(key, "<redacted>")
            if hasattr(error, "code"):
                entry["httpStatus"] = error.code
        entry["result"] = "ok" if seconds is not None else "unavailable"
        self._log_tomtom(entry)
        return seconds

    def _log_tomtom(self, entry):
        if not self.tomtom_log:
            return
        try:
            with open(self.tomtom_log, "a", encoding="utf-8") as log:
                log.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # provenance logging must never break the journey

    def route_command(self, command):
        """Send a route command through Siddhi and require its simulator acknowledgement."""
        url = self.environ.get("SIDDIHI_COMMAND_URL")
        if not url:
            return {"commandId": command["commandId"], "simulatorAcknowledged": True,
                    "mode": "local-demo"}
        result = post_json(
            url, {"event": command},
            {"Idempotency-Key": command["commandId"]}, timeout=15,
        )
        result = result.get("event", result)
        if result.get("commandId") != command["commandId"] or result.get("simulatorAcknowledged") is not True:
            raise RuntimeError("Siddhi did not return a matching simulator acknowledgement")
        return result

    @staticmethod
    def _meters(left, right):
        lat1, lon1 = map(math.radians, left)
        lat2, lon2 = map(math.radians, right)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371000 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))

