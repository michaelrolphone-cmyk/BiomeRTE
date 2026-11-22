"""Authenticated access to host-owned routes and sensor transports.

Host clock samples are intervals, not a shared monotonic epoch. No method opens
native SQLite, copies the route registry, or infers an address from placement.
"""
from __future__ import annotations

import base64
import copy
import http.client
import ipaddress
import json
import math
from pathlib import Path
import ssl
import struct
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener

PROTOCOL = "biomerte-host-io/1"
MAX_JSON = 1024 * 1024
MAX_FRAME = 16 * 1024 * 1024
INTERVAL_FIELDS = frozenset(("capture_host_monotonic_interval_s",
                             "capture_host_interval_s"))
POINT_FIELDS = frozenset(("received_monotonic", "received_monotonic_s",
                          "request_started_monotonic", "measurement_monotonic_s",
                          "processed_monotonic", "host_receive_monotonic_s",
                          "receive_decode_monotonic_s"))

def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def _private(host):
    if host in ("localhost", "host.docker.internal", "gateway.docker.internal"):
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_loopback or address.is_private
    except ValueError:
        return False

def read_exact(stream, size):
    chunks = []
    while size:
        block = stream.read(size)
        if not block:
            raise EOFError("host stream closed within a frame")
        chunks.append(block)
        size -= len(block)
    return b"".join(chunks)

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, source, code, message, headers, new_url):
        source.close()
        raise HTTPError(request.full_url, code, "host I/O redirects are not permitted", headers, None)

class ClockBounds:
    """An NTP-style bound: host-minus-client monotonic offset is [low, high]."""
    def __init__(self, clock_id, offset_low, offset_high, sampled_at, *,
                 max_age_s=10., max_uncertainty_s=.25, drift_ppm=500.,
                 wall_offset_low=0., wall_offset_high=0., clock=time.monotonic, host_id=None):
        self.clock_id = clock_id
        self._invalid = False
        self.host_id = host_id
        self.offset_low, self.offset_high = float(offset_low), float(offset_high)
        self.sampled_at = float(sampled_at)
        self.max_age_s, self.max_uncertainty_s = max_age_s, max_uncertainty_s
        self.drift_ppm, self._clock = drift_ppm, clock
        self.wall_offset_low, self.wall_offset_high = wall_offset_low, wall_offset_high
        if (not isinstance(clock_id, str) or not clock_id or
                not all(_finite(v) for v in (offset_low, offset_high, sampled_at,
                    max_age_s, max_uncertainty_s, drift_ppm, wall_offset_low, wall_offset_high)) or
                offset_low > offset_high or max_age_s <= 0 or max_uncertainty_s <= 0 or
                drift_ppm < 0 or wall_offset_low > wall_offset_high):
            raise ValueError("invalid host clock bounds")

    def invalidate(self):
        self._invalid = True

    def _check(self, source_clock_id=None, now=None):
        if self._invalid:
            raise ValueError("host clock binding was invalidated")
        now = self._clock() if now is None else now
        age = now - self.sampled_at
        drift = max(0., age) * self.drift_ppm / 1e6
        if (source_clock_id is not None and source_clock_id != self.clock_id):
            raise ValueError("host clock identity changed")
        if not 0 <= age <= self.max_age_s:
            raise ValueError("host clock sample expired")
        if self.offset_high - self.offset_low + 2 * drift > self.max_uncertainty_s:
            raise ValueError("host clock uncertainty exceeds policy")
        # UTC timestamps remain unchanged. Reject an unqualified wall-clock
        # relationship rather than silently restamping delayed measurements.
        if max(abs(self.wall_offset_low), abs(self.wall_offset_high)) > self.max_uncertainty_s:
            raise ValueError("host UTC clock relationship is unqualified")
        return now, drift

    @property
    def valid(self):
        try:
            self._check()
            return True
        except ValueError:
            return False

    def to_local_interval(self, interval, source_clock_id=None, now=None):
        if (not isinstance(interval, (list, tuple)) or len(interval) != 2 or
                not all(_finite(v) for v in interval) or interval[0] > interval[1]):
            raise ValueError("invalid host monotonic interval")
        _, drift = self._check(source_clock_id, now)
        return [interval[0] - self.offset_high - drift,
                interval[1] - self.offset_low + drift]

    def to_host_interval(self, interval, now=None):
        if (not isinstance(interval, (list, tuple)) or len(interval) != 2 or
                not all(_finite(v) for v in interval) or interval[0] > interval[1]):
            raise ValueError("invalid consumer monotonic interval")
        _, drift = self._check(now=now)
        return [interval[0] + self.offset_low - drift,
                interval[1] + self.offset_high + drift]

    def age(self, host_monotonic, source_clock_id=None, now=None):
        if not _finite(host_monotonic):
            raise ValueError("invalid host monotonic timestamp")
        now = self._clock() if now is None else now
        low, high = self.to_local_interval([host_monotonic, host_monotonic],
                                           source_clock_id, now)
        return now - high, now - low

    def metadata(self, value, source_clock_id):
        """Preserve source fields and widen intervals into the consumer epoch."""
        self._check(source_clock_id)
        result = copy.deepcopy(value)
        originals, converted = {}, {}
        def visit(node, path):
            if isinstance(node, dict):
                for key, item in list(node.items()):
                    address = ".".join(path + [key])
                    if key in INTERVAL_FIELDS:
                        interval = self.to_local_interval(item, source_clock_id)
                        originals[address], converted[address] = item, interval
                        node[key] = interval
                    elif key in POINT_FIELDS and item is not None:
                        interval = self.to_local_interval([item, item], source_clock_id)
                        originals[address], converted[address] = item, interval
                        # The oldest possible observation is conservative for
                        # consumers that currently accept one scalar timestamp.
                        node[key] = interval[0]
                    else:
                        visit(item, path + [key])
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    visit(item, path + [str(index)])
        visit(result, [])
        result["_host_io_clock"] = dict(protocol=PROTOCOL, source_clock_id=source_clock_id,
            qualified=True, source_fields=originals, local_intervals=converted,
            sampled_at_local_monotonic=self.sampled_at,
            offset_interval_s=[self.offset_low, self.offset_high])
        return result

class HostIOClient:
    def __init__(self, config, *, opener=None, monotonic=time.monotonic, wall=time.time):
        self.config = dict(config)
        endpoint = self.config.get("url", "")
        parsed = urlsplit(endpoint)
        if (not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("host I/O URL must be a credential-free origin")
        if parsed.scheme != "https" and not (
                parsed.scheme == "http" and self.config.get("allow_local_http") is True
                and _private(parsed.hostname)):
            raise ValueError("host I/O requires HTTPS or explicitly configured private local HTTP")
        token_file = self.config.get("token_file")
        if not isinstance(token_file, str) or not Path(token_file).is_absolute():
            raise ValueError("host I/O requires a private absolute token_file")
        self.url = endpoint.rstrip("/")
        self.timeout = self.config.get("timeout_s", 3.)
        if not _finite(self.timeout) or self.timeout <= 0:
            raise ValueError("host I/O timeout must be finite and positive")
        self._monotonic, self._wall = monotonic, wall
        self._bounds = None
        self.context = ssl.create_default_context(cafile=self.config.get("ca_file"))
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect(),
                                               HTTPSHandler(context=self.context)).open

    @classmethod
    def from_config(cls, path, **kwargs):
        return cls(json.loads(Path(path).read_text()), **kwargs)

    def _request(self, path, body=None, timeout=None):
        token = Path(self.config["token_file"]).read_text().strip()
        if not token:
            raise ValueError("host I/O credential file is empty")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
                   "Content-Type": "application/json"}
        raw = None if body is None else json.dumps(
            body, allow_nan=False, separators=(",", ":")).encode()
        request = Request(self.url + path, raw, headers)
        kwargs = {"timeout": self.timeout if timeout is None else timeout}
        return self._opener(request, **kwargs)

    def _call(self, operation, **arguments):
        with self._request("/v1/host-io/call",
                           dict(protocol=PROTOCOL, operation=operation, arguments=arguments)) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError("host I/O response exceeds limit")
        value = json.loads(raw)
        if self.config.get("host_id") and value.get("host_id") != self.config["host_id"]:
            raise ValueError("host I/O identity does not match configured binding")
        if self._bounds is not None and value.get("clock_id") != self._bounds.clock_id:
            self._bounds.invalidate()
        if value.get("protocol") != PROTOCOL or "result" not in value:
            raise ValueError("invalid host I/O response")
        return value

    def clock(self, force=False):
        if (not force and self._bounds is not None and self._bounds.valid and
                self._monotonic() - self._bounds.sampled_at < 3.):
            return self._bounds
        before, wall_before = self._monotonic(), self._wall()
        value = self._call("clock")["result"]
        after, wall_after = self._monotonic(), self._wall()
        host, host_wall = value["monotonic"], value["wall_time"]
        if self.config.get("host_id") and value.get("host_id") != self.config["host_id"]:
            raise ValueError("host I/O identity does not match configured binding")
        if not _finite(host) or not _finite(host_wall) or after < before:
            raise ValueError("invalid clock exchange")
        self._bounds = ClockBounds(value["clock_id"], host - after, host - before, after,
            wall_offset_low=host_wall - wall_after, wall_offset_high=host_wall - wall_before,
            max_age_s=self.config.get("clock_max_age_s", 10.),
            max_uncertainty_s=self.config.get("clock_max_uncertainty_s", .25),
            drift_ppm=self.config.get("clock_drift_ppm", 500.), clock=self._monotonic,
            host_id=value.get("host_id"))
        self._bounds._check()
        return self._bounds

    def list_routes(self):
        return self._call("routes.list")["result"]

    def route(self, device_id):
        return self._call("routes.get", device_id=device_id)["result"]

    def origin(self, url, device_id):
        row = self.route(device_id)
        if row is None:
            raise OSError("registered device route unavailable")
        parsed = urlsplit(url)
        host = str(ipaddress.ip_address(row["host"]))
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        if "@" in parsed.netloc:
            host = parsed.netloc.rsplit("@", 1)[0] + "@" + host
        return urlunsplit(parsed._replace(netloc=host))

    def candidates(self, bssid):
        return self._call("routes.candidates", bssid=bssid)["result"]

    def observe_anchor(self, bssid, host):
        return self._call("routes.observe_anchor", bssid=bssid, host=host)["result"]

    def refresh_routes(self):
        return self._call("routes.refresh")["result"]

    def survey_zero(self, world_id, request_id):
        return self._call("survey.zero", world_id=world_id, request_id=request_id)["result"]

    def survey_estimator_zero(self, world_id, request_id):
        """Call a configured estimator endpoint and preserve its domain rejection."""
        body = dict(type="request", world_id=world_id, request_id=request_id)
        rejected = False
        try:
            response = self._request("/v1/survey-zero", body)
        except HTTPError as error:
            if error.code != 400:
                raise
            response = error
            rejected = True
        with response:
            raw = response.read(16385)
        if not raw or len(raw) > 16384:
            raise ValueError("invalid survey control response")
        result = json.loads(raw)
        if rejected and result != {"ok": False, "error": "survey_request_rejected"}:
            raise ValueError("invalid survey estimator rejection")
        return result

    def device_request(self, device_id, path, payload=None):
        return self._call("device.request", device_id=device_id, path=path, payload=payload)["result"]

    def send_responder(self, payload, address):
        value = json.loads(payload)
        return self._call("anchors.send", record=value, host=address[0], port=address[1])["result"]

    def health(self):
        return self._call("health")["result"]

    def vision_frames(self, stream="live"):
        if stream not in ("live", "rear", "map"):
            raise ValueError("unsupported vision stream")
        self.clock()
        with self._request("/v1/host-io/vision/" + stream, timeout=5.) as response:
            while True:
                total, size = struct.unpack("!II", read_exact(response, 8))
                if not 4 <= total <= MAX_FRAME or not 0 < size <= min(MAX_JSON, total - 4):
                    raise ValueError("invalid vision frame length")
                envelope = json.loads(read_exact(response, size))
                jpeg = read_exact(response, total - 4 - size)
                bounds = self.clock()
                metadata = bounds.metadata(envelope["metadata"], envelope["clock"]["clock_id"])
                yield metadata, jpeg

    def anchor_datagrams(self):
        self.clock()
        with self._request("/v1/host-io/anchors", timeout=5.) as response:
            while True:
                line = response.readline(32769)
                if not line or len(line) > 32768 or not line.endswith(b"\n"):
                    raise ValueError("invalid anchor transport frame")
                item = json.loads(line)
                if item.get("heartbeat"):
                    continue
                bounds = self.clock()
                interval = bounds.to_local_interval(
                    [item["received_monotonic"], item["received_monotonic"]],
                    item["clock_id"])
                payload = base64.b64decode(item["payload"], validate=True)
                if len(payload) > 16384:
                    raise ValueError("oversized anchor datagram")
                yield dict(payload=payload, peer=(item["peer"][0], int(item["peer"][1])),
                           received_interval_s=interval, sequence=item["sequence"],
                           dropped=item.get("dropped", 0), source_clock_id=item["clock_id"])
