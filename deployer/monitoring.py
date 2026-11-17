"""Immutable demonstration profiles and container assessment predicates.

These thresholds request assessment; they do not establish product safety.
"""
import json
import math
import os
from datetime import datetime

DEFAULT_PROFILE = {
    "profileId": "CHILLED-DEMO-v1", "normalC": 3.0, "faultC": 10.0,
    "thresholdC": 5.0, "chickenThresholdC": 4.0, "sampleSeconds": 5,
    "spanSeconds": 10, "maxGapSeconds": 10, "freshSeconds": 15,
    "missingSeconds": 20, "finalizeSeconds": 20, "riseC": 0.5,
    "hotC": 30.0, "weatherMaxAgeSeconds": 1800, "weatherMaxDistanceM": 10000,
}
SCENARIOS = ("normal", "isolated-spike", "sustained-excursion", "missing-telemetry")


def seconds(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.timestamp()


def profile(product_id, environ=None):
    env = os.environ if environ is None else environ
    overrides = json.loads(env.get("MONITORING_PROFILE_JSON", "{}"))
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULT_PROFILE):
        raise ValueError("unknown monitoring profile setting")
    result = {**DEFAULT_PROFILE, **overrides}
    for key, value in result.items():
        if key == "profileId":
            if not isinstance(value, str) or not value:
                raise ValueError("profileId must be a nonempty string")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if key not in ("normalC", "faultC", "thresholdC", "chickenThresholdC", "hotC") and value <= 0:
            raise ValueError(f"{key} must be positive")
    if product_id == "PRD-004":
        result["thresholdC"] = result["chickenThresholdC"]
    return result


def evaluate(window, settings, at):
    """Return excursion and rising predicates for three consecutive samples."""
    if len(window) != 3:
        return False, False
    times = [seconds(o["occurredAt"]) for o in window]
    valid = (all(0 <= seconds(at) - t <= settings["freshSeconds"] for t in times)
             and all(0 < b - a <= settings["maxGapSeconds"] for a, b in zip(times, times[1:]))
             and times[-1] - times[0] >= settings["spanSeconds"])
    values = [o["celsius"] for o in window]
    return (valid and all(v > settings["thresholdC"] for v in values),
            valid and values[0] < values[1] < values[2]
            and values[2] - values[0] >= settings["riseC"])
