"""Four HC-SR04 readings, polled independently of vision and AHRS."""
import math
from camera_telemetry import SensorTelemetrySource, _integer, _text

DIRECTIONS = {"front", "left", "right", "rear"}


def validate_ultrasonic(value):
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("unsupported_ultrasonic_envelope")
    for key, maximum in (("receiver_id", 128), ("boot_id", 64)):
        _text(value.get(key), key, maximum)
    for key in ("uptime_ms", "sequence"):
        _integer(value.get(key), key)
    raw = value.get("ultrasonic")
    if not isinstance(raw, dict) or raw.get("schema") != "hcsr04.v1":
        raise ValueError("unsupported_ultrasonic_schema")
    if any(type(raw.get(k)) is not bool for k in ("paused", "worker_ready")):
        raise ValueError("invalid_ultrasonic_state")
    rows = raw.get("sensors")
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("four_ultrasonic_slots_required")
    seen = set()
    for s in rows:
        if not isinstance(s, dict) or s.get("id") not in DIRECTIONS or s["id"] in seen:
            raise ValueError("invalid_ultrasonic_identity")
        seen.add(s["id"])
        if type(s.get("valid")) is not bool:
            raise ValueError("invalid_ultrasonic_validity")
        _integer(s.get("sequence"), "sensor_sequence")
        _text(s.get("status"), "sensor_status", 64)
        for key in ("age_ms", "observed_uptime_ms"):
            if s.get(key) is not None:
                _integer(s[key], key)
        if s["valid"]:
            d = s.get("distance_m")
            if (type(d) not in (float, int) or not math.isfinite(d) or not .02 <= d <= 4
                    or raw["paused"] or not raw["worker_ready"] or s["status"] != "ok"
                    or s.get("age_ms") is None or s["age_ms"] > 1000):
                raise ValueError("invalid_ultrasonic_measurement")
            _integer(s.get("echo_us"), "echo_us", 118, 23529)
        elif s.get("distance_m") is not None or s.get("echo_us") is not None:
            raise ValueError("invalid_ultrasonic_null_measurement")
    return value


class UltrasonicSource(SensorTelemetrySource):
    def __init__(self, camera_config, config=None, **kwargs):
        settings = {"enabled": True, "poll_interval_s": .26,
                    "network_timeout_s": 1.0, "freshness_threshold_s": 1.0,
                    **(config or {})}
        super().__init__(camera_config, settings, **kwargs)

    def _read_snapshot(self):
        return validate_ultrasonic(self.client.get_ultrasonic())

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            # Old firmware remains usable; probe absent endpoint less often.
            delay = 5.0 if self.status()["supported"] is False else self.poll_interval_s
            self._stop.wait(delay)

    def _observations(self, value, elapsed):
        # Add request duration as a conservative age allowance, never relabel
        # HTTP or video receipt as a fresh sensor measurement.
        allowance = (elapsed + value["request_duration_s"]) * 1000.0
        return [{"id": s["id"], "sequence": s["sequence"],
                 "effective_age_ms": None if s["age_ms"] is None else s["age_ms"] + allowance}
                for s in value["telemetry"]["ultrasonic"]["sensors"]]
