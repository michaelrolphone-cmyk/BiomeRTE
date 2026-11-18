#!/usr/bin/env python3
"""Prepare and verify a scoped native-controller migration without live mutations.

This command never invokes a native operation. Execute the emitted commands
through the existing authenticated control plane only after the caller's
declared-scope ingress has been installed. Keep source denial in place.
The resulting bootstrap authorization proves native admission cutoff; it does
not claim physical quiescence. A new physical fence acknowledgement is still
required before the destination may admit effects.

Python 3.4 compatible, so the read-only exporter can run on the existing Pi.
"""
from __future__ import print_function
import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from urllib.parse import quote

PLAN = "biomerte-scoped-migration-plan/1"
CUTOFF = "biomerte-native-admission-cutoff/1"
EXPORT = "biomerte-scoped-migration-export/1"
SCOPE_FIELDS = ("service_id", "authority_id", "site", "resources", "providers", "contracts")
ACTIVE = frozenset(("queued", "claimed", "uncertain", "bound", "fencing", "pending"))
LIMITS = {"rows_per_kind": 512, "rows": 2048, "row_bytes": 262144, "bytes": 4194304}
CORE = ("offers", "leases", "actions", "_stream_grants", "activations", "_submissions",
        "_completion_receipts", "authorization_grants")


class MigrationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def scope_digest(scope):
    raw = json.dumps({key: scope[key] for key in SCOPE_FIELDS}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_scope(scope):
    if not isinstance(scope, dict) or any(key not in scope for key in SCOPE_FIELDS):
        raise MigrationError("Complete explicit destination scope required")
    value = {key: copy.deepcopy(scope[key]) for key in SCOPE_FIELDS}
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$", value["service_id"]):
        raise MigrationError("Invalid service identity")
    if not re.match(r"^[a-f0-9]{64}$", value["authority_id"]):
        raise MigrationError("Fresh logical authority identity required")
    if not isinstance(value["site"], str) or not value["site"]:
        raise MigrationError("Explicit site required")
    for key in ("resources", "providers", "contracts"):
        items = value[key]
        if (not isinstance(items, list) or not 0 < len(items) <= 64 or
                any(not isinstance(item, str) or not item for item in items) or
                len(set(items)) != len(items)):
            raise MigrationError("Explicit distinct bounded " + key + " required")
    return value


class Source:
    """Bounded current-state reads only; no journal, receipt, or history scan."""
    def __init__(self, database, controller, site, limits=None):
        self.database, self.controller, self.site = database, controller, site
        self.limits = dict(LIMITS)
        if limits:
            self.limits.update(limits)
        self.db = sqlite3.connect("file:" + quote(os.path.abspath(database), safe="/") +
                                  "?mode=ro", uri=True, timeout=5, isolation_level=None)
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("BEGIN")
        self.cache = {}
        self.read_bytes = 0

    def close(self):
        self.db.execute("ROLLBACK")
        self.db.close()

    def _decode(self, raw, size):
        if size > self.limits["row_bytes"]:
            raise MigrationError("Canonical record exceeds byte bound")
        self.read_bytes += size
        if self.read_bytes > self.limits["bytes"]:
            raise MigrationError("Bounded migration read budget exhausted")
        return json.loads(raw)

    def get(self, kind, key, required=False):
        marker = (kind, key)
        if marker not in self.cache:
            row = self.db.execute(
                "SELECT length(CAST(value AS BLOB)),CASE WHEN length(CAST(value AS BLOB))<=? "
                "THEN value ELSE NULL END FROM records WHERE kind=? AND id=?",
                (self.limits["row_bytes"], kind, key)).fetchone()
            self.cache[marker] = self._decode(row[1], row[0]) if row else None
        value = self.cache[marker]
        if value is None and required:
            raise MigrationError("Required native record missing: " + kind + "/" + key)
        return copy.deepcopy(value)

    def scan(self, kind):
        if kind in ("_receipt", "_retention", "events", "revisions"):
            raise MigrationError("History/receipt scans are forbidden")
        rows = list(self.db.execute("SELECT id,length(CAST(value AS BLOB)) FROM records "
                                    "WHERE kind=? ORDER BY id LIMIT ?",
                                    (kind, self.limits["rows_per_kind"] + 1)))
        if len(rows) > self.limits["rows_per_kind"]:
            raise MigrationError("Current-state kind exceeds row bound: " + kind)
        return [(key, self.get(kind, key, True)) for key, unused in rows]

    def position(self):
        row = self.db.execute("SELECT seq,hash FROM position WHERE id=1").fetchone()
        return {"sequence": row[0], "hash": row[1]} if row else None


def references(value):
    if isinstance(value, str):
        return set((value,))
    if isinstance(value, list):
        out = set()
        for item in value:
            out.update(references(item))
        return out
    if isinstance(value, dict):
        out = set()
        for item in value.values():
            out.update(references(item))
        return out
    return set()


def inventory(source, scope, consumers):
    resources, providers, contracts = (set(scope[key]) for key in
                                       ("resources", "providers", "contracts"))
    selected = {}
    def add(kind, key, value):
        selected[(kind, key)] = copy.deepcopy(value)
        if len(selected) > source.limits["rows"]:
            raise MigrationError("Scoped export row budget exhausted")
    offers = [(key, value) for key, value in source.scan("offers")
              if value.get("resource") in resources]
    if not offers or any(value.get("provider") not in providers or
                         value.get("contract") not in contracts for key, value in offers):
        raise MigrationError("Native offers do not fit the exact approved scope")
    if {value["resource"] for key, value in offers} != resources:
        raise MigrationError("A resource has no native offer")
    if {value["contract"] for key, value in offers} != contracts:
        raise MigrationError("Approved contracts differ from native resource offers")
    for key, value in offers:
        add("offers", key, value)
        for kind in ("_offer_usage",):
            item = source.get(kind, key)
            if item is not None:
                add(kind, key, item)
    for resource in resources:
        for kind in ("guards", "_fence_generations", "_capacity", "_enforcers"):
            value = source.get(kind, resource)
            if value is not None:
                add(kind, resource, value)
    for key, value in source.scan("guards"):
        if value.get("resource") in resources:
            add("guards", key, value)
    for contract in contracts:
        add("contracts", contract, source.get("contracts", contract, True))
        for kind in ("_contract_usage", "_builtin_contracts"):
            value = source.get(kind, contract)
            if value is not None:
                add(kind, contract, value)
    for actor in providers | set(consumers):
        add("peers", actor, source.get("peers", actor, True))
    leases = [(key, value) for key, value in source.scan("leases")
              if value.get("resource") in resources or value.get("requested_resource") in resources]
    lease_ids = {key for key, value in leases}
    for key, value in leases:
        if value.get("resource") not in resources or value.get("contract") not in contracts:
            raise MigrationError("Lease crosses the approved service scope")
        add("leases", key, value)
    action_ids = set()
    for kind in ("actions", "_stream_grants", "activations", "_completion_receipts"):
        for key, value in source.scan(kind):
            if value.get("resource") not in resources and value.get("lease") not in lease_ids:
                continue
            if value.get("resource") not in resources:
                raise MigrationError("Cross-resource dependent record")
            if value.get("contract") and value["contract"] not in contracts:
                raise MigrationError("Dependent record has another contract")
            add(kind, key, value)
            if kind == "actions":
                action_ids.add(key)
    selected_refs = resources | lease_ids | action_ids | {key for key, value in offers}
    for kind in ("_submissions", "authorization_grants"):
        for key, value in source.scan(kind):
            if key in action_ids or references(value).intersection(selected_refs):
                add(kind, key, value)
    for key, value in source.scan("_lease_queue"):
        if value in lease_ids:
            add("_lease_queue", key, value)
    # Ordered request slots have fixed, exact keys. Preserve their replay floors
    # without copying a result belonging to another resource.
    for actor in providers | set(consumers):
        for slot in range(16):
            key = digest([actor, slot])
            value = source.get("_receipt_stream", key)
            if value is None:
                continue
            if references(value.get("result")).intersection(selected_refs):
                add("_receipt_stream", key, value)
            else:
                add("_receipt_stream", key, {field: value[field] for field in
                                             ("sequence", "signature") if field in value})
    return selected


def rows(selected):
    return [[kind, key, selected[(kind, key)]] for kind, key in sorted(selected)]


def live(selected):
    result = {"actions": [], "leases": [], "streams": [], "activations": [], "submissions": []}
    for (kind, key), value in selected.items():
        state = value.get("state") if isinstance(value, dict) else None
        if kind == "actions" and state in ("queued", "claimed", "uncertain"):
            result["actions"].append(key)
        elif kind == "leases" and state in ("queued", "bound", "fencing"):
            result["leases"].append(key)
        elif kind == "_stream_grants" and state == "open":
            result["streams"].append(key)
        elif kind == "activations" and state == "pending":
            result["activations"].append(key)
        # Native _submissions are immutable request metadata, without a state
        # field. Actual custody lives in the referenced lease/action above.
        # Missing/pruned custody is unknown history, never permission to replay.
    return result


def submission_origins(selected):
    result = []
    for (kind, key), value in sorted(selected.items()):
        if kind != "_submissions":
            continue
        lease = selected.get(("leases", value.get("lease", "")))
        action = selected.get(("actions", key))
        result.append({"operation": key, "lease": value.get("lease"),
                       "lease_state": lease.get("state") if lease else None,
                       "action_state": action.get("state") if action else None,
                       "result_known": bool(action and action.get("state") in
                                            ("succeeded", "failed", "abandoned")),
                       "replay_allowed": False})
    return result


def command(run_id, role, operation, arguments, actor="admin"):
    suffix = digest([operation, arguments])[:20]
    request_id = run_id + "-" + role + "-" + suffix
    if len(request_id) > 128 or not re.match(r"^[A-Za-z0-9_.:-]+$", request_id):
        raise MigrationError("Run ID produces an invalid native request identity")
    return {"operation": operation, "arguments": arguments,
            "request_id": request_id, "actor": actor}


def offer_arguments(value, state):
    return {"provider": value["provider"], "contract": value["contract"],
            "resource": value["resource"], "state": state,
            "service": value.get("service", ""),
            "configuration": copy.deepcopy(value.get("configuration", {}))}


def drain_commands(plan, selected):
    active = live(selected)
    if active["actions"] or active["activations"] or active["submissions"]:
        raise MigrationError("Unresolved physical/submission work must be preserved and reconciled; no replay")
    result = []
    leases = {key: value for (kind, key), value in selected.items() if kind == "leases"}
    # Cancel old queued work before returning any occupied guard.
    for key in sorted(active["leases"]):
        if leases[key]["state"] == "queued":
            result.append(command(plan["run_id"], "drain", "cap.release", {"lease": key}))
    for session in sorted(active["streams"]):
        grant = selected[("_stream_grants", session)]
        members = [key for key, value in leases.items()
                   if value["state"] == "bound" and
                   (key == grant["lease"] or value.get("shared_parent") == grant["lease"])]
        members.sort(key=lambda key: (key == grant["lease"], key))
        if not members:
            raise MigrationError("Open stream has no releasable scoped consumer")
        for key in members:
            result.append(command(plan["run_id"], "drain", "stream.close",
                                  {"session": session, "lease": key}))
    for key in sorted(active["leases"], key=lambda item: (not bool(leases[item].get("shared_parent")), item)):
        if leases[key]["state"] != "queued":
            result.append(command(plan["run_id"], "drain", "cap.release", {"lease": key}))
    return result


def validate_plan(plan, scope):
    if not isinstance(plan, dict) or plan.get("protocol") != PLAN:
        raise MigrationError("A concrete native migration plan is required")
    body = dict(plan)
    expected = body.pop("plan_digest", None)
    if expected != digest(body) or plan.get("scope") != validate_scope(scope):
        raise MigrationError("Migration plan or destination scope changed")
    if plan.get("scope_digest") != scope_digest(scope):
        raise MigrationError("Target scope digest mismatch")
    originals = plan.get("before_records")
    if not isinstance(originals, list) or any(not isinstance(row, list) or len(row) != 3 for row in originals):
        raise MigrationError("Exact original scoped records required")
    before = {(kind, key): value for kind, key, value in originals}
    if len(before) != len(originals):
        raise MigrationError("Duplicate original record")
    expected_cutoff = []
    for actor in scope["providers"]:
        peer = before.get(("peers", actor), {})
        if peer.get("enabled") is not True or peer.get("id") != actor:
            raise MigrationError("Original enabled provider proof is missing")
        expected_cutoff.append(command(plan["run_id"], "cutoff", "peer.revoke", {"peer": actor}))
    native_offers = [(key, value) for (kind, key), value in sorted(before.items()) if kind == "offers"]
    if (not native_offers or
            {value.get("resource") for key, value in native_offers} != set(scope["resources"]) or
            {value.get("contract") for key, value in native_offers} != set(scope["contracts"])):
        raise MigrationError("Original offers do not cover the exact scope")
    for key, value in native_offers:
        if (value.get("provider") not in scope["providers"] or
                key != digest([value["provider"], value["contract"], value["resource"]])):
            raise MigrationError("Original offer identity mismatch")
        expected_cutoff.append(command(plan["run_id"], "cutoff", "cap.offer",
                                       offer_arguments(value, "offline")))
    if plan.get("cutoff_commands") != expected_cutoff:
        raise MigrationError("Native cutoff commands are incomplete or reordered")
    expected_drains = {canonical(item) for item in drain_commands(plan, before)}
    supplied_drains = {canonical(item) for item in plan.get("drain_commands", [])}
    if expected_drains != supplied_drains:
        raise MigrationError("Native drain commands do not match original scoped custody")
    return plan


def make_plan(source, scope, consumers, run_id):
    scope = validate_scope(scope)
    if source.site != scope["site"] or source.controller == scope["authority_id"]:
        raise MigrationError("Source site/identity does not fit a fresh scoped destination")
    selected = inventory(source, scope, consumers)
    active = live(selected)
    if active["actions"] or active["activations"] or active["submissions"]:
        raise MigrationError("Unresolved scoped work blocks automatic drain; preserve its original identity")
    cutoff, restore = [], []
    for actor in scope["providers"]:
        peer = selected[("peers", actor)]
        if peer.get("enabled") is not True:
            raise MigrationError("Capture the original enabled provider before native cutoff")
        if set(peer) != set(("id", "key", "label", "scopes", "enabled")):
            raise MigrationError("Peer schema needs an exact restore review")
        resource_scopes = {entry[9:] for entry in peer["scopes"] if entry.startswith("resource:")}
        if not resource_scopes or not resource_scopes.issubset(set(scope["resources"])):
            raise MigrationError("Provider also holds an unrelated resource scope")
        cutoff.append(command(run_id, "cutoff", "peer.revoke", {"peer": actor}))
        restore.append(command(run_id, "restore", "peer.trust",
                               {"public_key": peer["key"], "label": peer["label"], "scopes": peer["scopes"]}))
    for (kind, key), value in sorted(selected.items()):
        if kind != "offers":
            continue
        cutoff.append(command(run_id, "cutoff", "cap.offer", offer_arguments(value, "offline")))
        restore.insert(0, command(run_id, "restore", "cap.offer", offer_arguments(value, value["state"])))
    plan = {"protocol": PLAN, "run_id": run_id, "source_controller": source.controller,
            "site": source.site, "source_database": os.path.abspath(source.database),
            "scope": scope, "scope_digest": scope_digest(scope), "consumers": sorted(set(consumers)),
            "observed_at": time.time(), "source_position": source.position(),
            "before_records": rows(selected), "submission_origins": submission_origins(selected),
            "cutoff_commands": cutoff,
            "restore_commands": restore,
            "restore_policy": "Exact review only; never restore native admission after destination activation",
            "physical_quiescence_proven": False}
    plan["drain_commands"] = drain_commands(plan, selected)
    plan["plan_digest"] = digest(plan)
    return plan


def native_receipt(source, operation):
    key = digest([operation["actor"], operation["request_id"]])
    value = source.get("_receipt", key, True)
    if (not isinstance(value, dict) or value.get("format") != 2 or
            value.get("operation") != operation["operation"] or
            value.get("signature") != digest([operation["operation"], operation["arguments"]]) or
            any(value.get(field) for field in ("external_pending", "external_error", "result_expired")) or
            not isinstance(value.get("result"), dict)):
        raise MigrationError("Native command receipt does not prove the exact operation")
    return {"request": copy.deepcopy(operation), "receipt_key": key,
            "receipt_digest": digest(value), "record": value}


def verify_cutoff(source, scope, plan):
    validate_plan(plan, scope)
    if source.controller != plan["source_controller"] or source.site != plan["site"]:
        raise MigrationError("Different native source")
    original = {(kind, key): value for kind, key, value in plan["before_records"]}
    receipts = []
    for operation in plan["cutoff_commands"]:
        proof = native_receipt(source, operation)
        result = proof["record"]["result"]
        if operation["operation"] == "peer.revoke":
            key = operation["arguments"]["peer"]
            expected = dict(original[("peers", key)], enabled=False)
            if source.get("peers", key, True) != expected or result != expected:
                raise MigrationError("Native provider denial differs from reviewed cutoff")
        elif operation["operation"] == "cap.offer":
            args = operation["arguments"]
            key = digest([args["provider"], args["contract"], args["resource"]])
            expected = dict(original[("offers", key)], state="offline")
            if source.get("offers", key, True) != expected or any(result.get(k) != v for k, v in expected.items()):
                raise MigrationError("A native scoped offer is not durably offline")
        else:
            raise MigrationError("Unexpected source-cutoff operation")
        receipts.append(proof)
    return receipts


def refresh_drain(source, scope, plan):
    verify_cutoff(source, scope, plan)
    selected = inventory(source, scope, plan["consumers"])
    body = dict(plan)
    body.pop("plan_digest")
    known = {digest(item) for item in body["drain_commands"]}
    for item in drain_commands(plan, selected):
        if digest(item) not in known:
            body["drain_commands"].append(item)
    prior = {(kind, key) for kind, key, value in body["before_records"]}
    # Retain newly observed old-source operations as provenance, never as new work.
    for kind, key, value in rows(selected):
        if kind in ("leases", "actions", "_stream_grants", "_submissions") and (kind, key) not in prior:
            body["before_records"].append([kind, key, value])
    body["before_records"].sort(key=lambda row: (row[0], row[1]))
    body["plan_digest"] = digest(body)
    return body


def export_state(source, scope, plan):
    cutoff = verify_cutoff(source, scope, plan)
    selected = inventory(source, scope, plan["consumers"])
    active = live(selected)
    if any(active.values()):
        raise MigrationError("Native scoped custody is not drained: " + canonical(active))
    drains = []
    for operation in plan["drain_commands"]:
        proof = native_receipt(source, operation)
        result = proof["record"]["result"]
        if operation["operation"] == "cap.release":
            if result.get("id") != operation["arguments"]["lease"] or result.get("state") != "released":
                raise MigrationError("Release receipt is not a settled native lease")
            marker = ("leases", result["id"])
            if marker not in selected:
                selected[marker] = copy.deepcopy(result)
        elif operation["operation"] == "stream.close":
            if result.get("id") != operation["arguments"]["session"]:
                raise MigrationError("Stream-close receipt identifies another grant")
            if result.get("resource") not in scope["resources"]:
                raise MigrationError("Stream-close receipt crosses service scope")
            marker = ("_stream_grants", result["id"])
            if marker not in selected and result.get("state") != "open":
                selected[marker] = copy.deepcopy(result)
        else:
            raise MigrationError("Unexpected native drain operation")
        drains.append(proof)
    if any(value.get("holder") for (kind, key), value in selected.items() if kind == "guards"):
        raise MigrationError("Native resource guard is still held")
    before = {(kind, key): value for kind, key, value in plan["before_records"]}
    # These are destination copies. Source rows remain revoked/offline.
    for marker, value in list(selected.items()):
        if marker[0] == "offers" or marker[0] == "peers" and marker[1] in scope["providers"]:
            if marker not in before:
                raise MigrationError("An unreviewed provider/offer appeared")
            selected[marker] = copy.deepcopy(before[marker])
    for actor in plan["consumers"]:
        if source.get("peers", actor, True) != before[("peers", actor)]:
            raise MigrationError("Shared consumer authority changed; review instead of transferring it")
    source_rows = rows(inventory(source, scope, plan["consumers"]))
    proof = {"protocol": CUTOFF, "source_controller": source.controller,
             "site": source.site, "target_authority": scope["authority_id"],
             "service_id": scope["service_id"], "scope_digest": scope_digest(scope),
             "plan_digest": plan["plan_digest"], "observed_at": time.time(),
             "source_position": source.position(), "source_records_digest": digest(source_rows),
             "native_cutoff_receipts": cutoff, "native_drain_receipts": drains,
             "retained_submission_origins": submission_origins(selected),
             "admission": {"provider_credentials": "revoked", "offers": "offline",
                           "live_scoped_actions": 0, "live_scoped_leases": 0,
                           "open_scoped_streams": 0,
                           "source_reactivation_requires_explicit_operator_action": True},
             "physical_quiescence_proven": False,
             "next_required_evidence": "A new physical generation acknowledged by the real provider"}
    proof["receipt_digest"] = digest(proof)
    former_map = {(kind, key): value for kind, key, value in plan["before_records"]
                  if kind in ("leases", "actions", "_stream_grants", "_submissions")}
    for marker, value in selected.items():
        if marker[0] == "_submissions" and marker not in former_map:
            former_map[marker] = copy.deepcopy(value)
    former = rows(former_map)
    origins = submission_origins(selected)
    # Native submit can reacquire even when immutable request metadata exists.
    # Keep original requests inert; ScopedAuthority denies their original IDs.
    # No active submission index is ever imported.
    for marker in list(selected):
        if marker[0] in ("_submissions", "_submission_active", "_submission_leases"):
            del selected[marker]
    selected[("_service_source", "initial")] = {
        "source_controller": source.controller, "target_authority": scope["authority_id"],
        "scope_digest": scope_digest(scope), "cutoff_receipt": proof,
        "former_native_records": former, "submission_origins": origins,
        "replay_allowed": False,
        "origin_status_policy": "Inspect original native operation IDs; never infer success or replay"}
    state = {"authority": scope["authority_id"], "site": scope["site"], "records": rows(selected)}
    if len(canonical(state).encode("utf-8")) > source.limits["bytes"]:
        raise MigrationError("Destination scoped state exceeds byte budget")
    return {"protocol": EXPORT, "scope": validate_scope(scope),
            "scope_digest": scope_digest(scope), "state": state,
            "state_digest": digest(state), "authorization": {"source_receipt": proof},
            "physical_quiescence_proven": False}


def write_private(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(value, sort_keys=True, indent=2))
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        raise
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "drain-plan", "export"))
    parser.add_argument("--database", required=True)
    scope_group = parser.add_mutually_exclusive_group(required=True)
    scope_group.add_argument("--scope")
    scope_group.add_argument("--scope-json")
    parser.add_argument("--source-home", required=True)
    parser.add_argument("--source-controller", required=True)
    parser.add_argument("--consumer", action="append", default=[])
    parser.add_argument("--run-id")
    parser.add_argument("--plan")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    scope = validate_scope(json.loads(args.scope_json) if args.scope_json else
                           json.load(open(args.scope)))
    node = json.load(open(os.path.join(args.source_home, "node.json")))
    if node.get("node") != args.source_controller or node.get("site") != scope["site"]:
        raise MigrationError("Native identity file differs from requested source")
    if os.path.realpath(args.database) != os.path.realpath(os.path.join(args.source_home, "site.sqlite")):
        raise MigrationError("Database is not the source identity's site store")
    source = Source(args.database, args.source_controller, scope["site"])
    try:
        if args.mode == "plan":
            if not args.run_id or not args.consumer:
                raise MigrationError("Explicit run ID and scoped consumer identities required")
            value = make_plan(source, scope, args.consumer, args.run_id)
        else:
            if not args.plan:
                raise MigrationError("Reviewed concrete migration plan required")
            plan = json.load(open(args.plan))
            value = refresh_drain(source, scope, plan) if args.mode == "drain-plan" else export_state(source, scope, plan)
    finally:
        source.close()
    write_private(args.output, value)
    print(json.dumps({"mode": args.mode, "output": os.path.abspath(args.output),
                      "protocol": value["protocol"], "scope_digest": value["scope_digest"],
                      "digest": value.get("state_digest", value.get("plan_digest")),
                      "live_mutations": False, "physical_quiescence_proven": False}, sort_keys=True))


if __name__ == "__main__":
    main()
