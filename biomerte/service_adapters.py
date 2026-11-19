"""Bounded stateful service preparation and named host-local bindings.

Adapters are independently available Docker services. They own checkpoint
verification and deny application effects until exact placement plus physical
fencing permits activation. This module never turns cached state into authority.
"""
from __future__ import print_function
import hashlib
import ipaddress
import json
import math
import os
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PROTOCOL = "biomerte-service-adapter/1"
BINDING_FIELDS = ("service_id", "manifest_revision", "worker_id",
                  "worker_incarnation", "boot_epoch", "grant_token")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
ROLES = frozenset(("config", "private", "state", "cache"))
MAX_REPLY_BYTES = 262144


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def staged(manifest):
    return manifest.get("activation") is not None


def validate(manifest):
    activation = manifest.get("activation")
    if activation is not None:
        if (not isinstance(activation, dict) or
                set(activation) != {"protocol", "binding"} or
                activation.get("protocol") != PROTOCOL or
                not isinstance(activation.get("binding"), str) or
                not NAME.match(activation["binding"])):
            raise ValueError("Activation requires the exact service-adapter protocol and named binding")
        if (manifest.get("state_policy") != "externally_managed" or
                manifest.get("effect_policy") != "externally_fenced"):
            raise ValueError("Staged activation requires externally managed state and external fencing")
    targets = set()
    for mount in manifest.get("mounts", []):
        if not isinstance(mount, dict):
            raise ValueError("Mount must be an object")
        target = mount.get("target")
        if not isinstance(target, str) or not os.path.isabs(target) or ":" in target or "\x00" in target:
            raise ValueError("Mount target must be an absolute Docker path")
        if target in targets:
            raise ValueError("Duplicate mount target")
        targets.add(target)
        if "read_only" in mount and type(mount["read_only"]) is not bool:
            raise ValueError("Mount read_only must be boolean")
        if "binding" in mount:
            if ("source" in mount or not isinstance(mount["binding"], str) or
                    not NAME.match(mount["binding"]) or mount.get("role") not in ROLES):
                raise ValueError("Named mount requires one binding and an explicit role")
            role = mount["role"]
            if role in ("private", "config") and not mount.get("read_only", True):
                raise ValueError("Private and configuration bindings must be read-only")
            if role == "state":
                if not staged(manifest) or mount.get("cache_only"):
                    raise ValueError("Authoritative state requires a staged adapter, never cache_only")
            elif not mount.get("read_only", True) and role != "cache":
                raise ValueError("Writable binding requires state or cache role")
        else:
            source = mount.get("source")
            if (not isinstance(source, str) or not os.path.isabs(source) or
                    ":" in source or "\x00" in source):
                raise ValueError("Mount source must be an absolute Docker-host path")
            if not mount.get("read_only", True) and not mount.get("cache_only", False):
                raise ValueError("Writable local mounts require explicit disposable cache_only")
    return manifest


def resolve_mounts(manifest, bindings):
    result = []
    for mount in manifest.get("mounts", []):
        resolved = dict(mount)
        if "binding" in mount:
            local = bindings.get(mount["binding"])
            if (not isinstance(local, dict) or local.get("role") != mount["role"] or
                    local.get("service_id") != manifest["id"]):
                raise ValueError("Host binding role or service identity mismatch")
            source = local.get("source")
            if (not isinstance(source, str) or not os.path.isabs(source) or
                    ":" in source or "\x00" in source):
                raise ValueError("Host binding requires an absolute Docker-host source")
            if local.get("read_only") is True and not mount.get("read_only", True):
                raise ValueError("Host binding forbids writable access")
            resolved["source"] = source
        result.append(resolved)
    return result


def binding(grant):
    value = {}
    for name in BINDING_FIELDS:
        item = grant.get(name)
        if not isinstance(item, str) or not item:
            raise ValueError("Incomplete service grant binding")
        value[name] = item
    if not DIGEST.match(value["manifest_revision"]):
        raise ValueError("Invalid service manifest revision")
    return value


def request(operation, grant, **extra):
    if operation not in ("prepare", "activate"):
        raise ValueError("Unknown service adapter operation")
    bound = binding(grant)
    identity = hashlib.sha256(canonical(
        [PROTOCOL, operation, bound]).encode("utf-8")).hexdigest()
    value = dict(protocol=PROTOCOL, operation=operation, id=identity, binding=bound)
    value.update(extra)
    return value


def response_matches(request_body, value):
    return (isinstance(value, dict) and value.get("protocol") == PROTOCOL and
            value.get("id") == request_body["id"] and
            value.get("binding") == request_body["binding"])


def prepared(request_body, value):
    if not response_matches(request_body, value) or value.get("phase") != "prepared":
        raise ValueError("Preparation reply does not match the exact request binding")
    state = value.get("state")
    if (not isinstance(state, dict) or state.get("ready") is not True or
            state.get("fresh") is not True or state.get("custody_complete") is not True or
            not isinstance(state.get("checkpoint"), str) or not DIGEST.match(state["checkpoint"])):
        raise ValueError("Checkpoint unavailable, stale, or custody incomplete")
    return dict(protocol=PROTOCOL, binding=request_body["binding"],
                checkpoint=state["checkpoint"], fresh=True, custody_complete=True, ready=True)


def proof_matches(value, grant):
    try:
        return (isinstance(value, dict) and value.get("protocol") == PROTOCOL and
                value.get("binding") == binding(grant) and value.get("ready") is True and
                value.get("fresh") is True and value.get("custody_complete") is True and
                isinstance(value.get("checkpoint"), str) and bool(DIGEST.match(value["checkpoint"])))
    except ValueError:
        return False


def fence_matches(value, grant, state_proof):
    return (proof_matches(state_proof, grant) and isinstance(value, dict) and
            value.get("fenced") is True and value.get("binding") == state_proof["binding"] and
            value.get("boot_epoch") == grant["boot_epoch"] and
            value.get("grant_token") == grant["grant_token"] and
            value.get("checkpoint") == state_proof["checkpoint"])


def activated(request_body, value):
    if (not response_matches(request_body, value) or value.get("phase") != "active" or
            value.get("checkpoint") != request_body["checkpoint"]):
        raise ValueError("Activation reply does not match the exact request and checkpoint")
    return dict(protocol=PROTOCOL, binding=request_body["binding"],
                checkpoint=value["checkpoint"], active=True)


def local_http(host):
    if host in ("localhost", "host.docker.internal", "gateway.docker.internal"):
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_loopback or address.is_private
    except ValueError:
        return False


class AdapterClient:
    def __init__(self, config, manifest, opener=None):
        activation = manifest["activation"]
        self.settings = config.get("adapters", {}).get(activation["binding"])
        if (not isinstance(self.settings, dict) or
                self.settings.get("service_id") != manifest["id"]):
            raise ValueError("Host adapter binding does not match this service")
        url = self.settings.get("url", "")
        parsed = urlsplit(url)
        if (not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment):
            raise ValueError("Adapter endpoint requires a credential-free URL")
        permitted_http = (config.get("allow_http_for_testing") or
                          (self.settings.get("allow_local_http") is True and local_http(parsed.hostname)))
        if parsed.scheme != "https" and not (parsed.scheme == "http" and permitted_http):
            raise ValueError("Adapter requires HTTPS or explicitly configured private local HTTP")
        timeout = self.settings.get("timeout_s", config.get("request_timeout_s", 3))
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Adapter timeout must be finite and positive")
        token_file = self.settings.get("token_file")
        if token_file is not None and (not isinstance(token_file, str) or not os.path.isabs(token_file)):
            raise ValueError("Adapter token_file must name a private absolute runner path")
        self.url, self.timeout, self.opener = url, timeout, opener or urlopen

    def call(self, body):
        headers = {"Content-Type": "application/json"}
        if self.settings.get("token_file"):
            with open(self.settings["token_file"]) as source:
                token = source.read().strip()
            if not token:
                raise ValueError("Adapter credential file is empty")
            headers["Authorization"] = "Bearer " + token
        native = Request(self.url, canonical(body).encode("utf-8"), headers)
        with self.opener(native, timeout=self.timeout) as reply:
            raw = reply.read(MAX_REPLY_BYTES + 1)
        if len(raw) > MAX_REPLY_BYTES:
            raise ValueError("Adapter reply exceeds the bounded protocol")
        return json.loads(raw.decode("utf-8"))
