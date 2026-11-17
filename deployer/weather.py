"""Bounded Open-Meteo current-condition adapter. Call from a worker thread."""
import hashlib
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from monitoring import seconds

FIELDS = ("temperature_2m", "precipitation", "wind_speed_10m", "weather_code")
UNITS = {"temperature_2m": "°C", "precipitation": "mm", "wind_speed_10m": "km/h", "weather_code": "wmo code"}


class WeatherAdapter:
    def __init__(self, log_path=None, fetch=None):
        self.cache = {}
        self.attempts = {}
        self.log_path = log_path
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(latitude, longitude):
        query = urllib.parse.urlencode({
            "latitude": latitude, "longitude": longitude,
            "current": ",".join(FIELDS), "timezone": "UTC",
            "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
        })
        with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + query, timeout=5) as response:
            return json.load(response)

    def current(self, journey_id, position, at=None):
        at = at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        tick = seconds(at)
        if tick - self.attempts.get(journey_id, float("-inf")) < 60:
            return None  # no refresh attempted; caller retains previous context
        self.attempts[journey_id] = tick
        grid = tuple(round(v / 0.05) for v in position)
        cached = self.cache.get(grid)
        if cached and 0 <= tick - seconds(cached["retrievedAt"]) < 600:
            return {**cached, "cached": True, "requestedCoordinates": list(position)}
        try:
            raw = self.fetch(*position)
            current, units = raw["current"], raw["current_units"]
            if any(units.get(k) != UNITS[k] for k in FIELDS):
                raise ValueError("unexpected weather units")
            if any(isinstance(current.get(k), bool) or not isinstance(current.get(k), (int, float))
                   or not math.isfinite(current[k]) for k in FIELDS):
                raise ValueError("incomplete current conditions")
            provider_time = current["time"]
            if not provider_time.endswith(("Z", "+00:00")):
                provider_time += "Z"  # API timezone explicitly requested as UTC
            seconds(provider_time)
            coordinates = [float(raw["latitude"]), float(raw["longitude"])]
            if not (-90 <= coordinates[0] <= 90 and -180 <= coordinates[1] <= 180):
                raise ValueError("invalid provider coordinates")
            identity = {"coordinates": coordinates, "providerTime": provider_time,
                        "conditions": {k: current[k] for k in FIELDS}}
            entry = {**identity, "observationId": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
                     "requestedCoordinates": list(position), "retrievedAt": at, "units": {k: units[k] for k in FIELDS},
                     "provider": "open-meteo", "source": "model-based-current-conditions",
                     "inputMode": "live-api", "available": True, "cached": False}
            self.cache[grid] = entry
        except Exception as error:
            entry = {"available": False, "provider": "open-meteo", "inputMode": "live-api",
                     "requestedCoordinates": list(position), "retrievedAt": at,
                     "error": type(error).__name__}
        if self.log_path:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as log:
                log.write(json.dumps(entry) + "\n")
        return entry


def eligible(context, position, settings, at):
    if not context or not context.get("available"):
        return False
    from transport_integrations import TransportIntegrations
    age = seconds(at) - seconds(context["providerTime"])
    return (0 <= age <= settings["weatherMaxAgeSeconds"]
            and TransportIntegrations._meters(context["coordinates"], position) <= settings["weatherMaxDistanceM"])
