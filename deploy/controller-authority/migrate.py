"""Offline, provenance-preserving controller-state migration.

This utility never starts an authority or edits a source database. Sources must
be stopped or running the freeze guard before the final snapshot. A marker is
an admission fence, not evidence that an old, unpatched process has stopped.
Snapshot/export payloads contain private application data: keep them on the
authorized hosts, and print only inventories/collision reports.
"""
import argparse
import base64
import copy
import hashlib
import json
import os
import pathlib
import re
import sqlite3
from urllib.parse import quote

FORMAT = "biomerte-controller-migration/1"
ARCHIVE = "_controller_migration_archive"
RECEIPT_CONFLICTS = "_controller_receipt_conflicts"
STREAM_CONFLICTS = "_controller_receipt_stream_conflicts"
EVENT_KINDS = frozenset(("events", "delegation_events"))
EVENT_META = frozenset(("events", "delegation_sequence", "last_run",
                         "operational_retention", "operational_retention/history"))
TERMINAL_RUNS = frozenset(("completed", "cancelled", "failed"))

class MigrationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def json_chunks(value):
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
    return (part.encode("utf-8") for part in encoder.iterencode(value))


def digest(value):
    hasher = hashlib.sha256()
    for chunk in json_chunks(value):
        hasher.update(chunk)
    return hasher.hexdigest()


def encoded_size(value):
    return sum(len(chunk) for chunk in json_chunks(value))


def node_id(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise MigrationError("Exact controller fingerprint required")
    return value


def rows_of(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("format") != FORMAT:
        raise MigrationError("Controller snapshot format required")
    node_id(snapshot.get("source_node"))
    if not isinstance(snapshot.get("site"), str) or not snapshot["site"]:
        raise MigrationError("Source site required")
    rows = snapshot.get("records")
    if not isinstance(rows, list):
        raise MigrationError("Canonical source records required")
    result = {}
    for row in rows:
        if (not isinstance(row, list) or len(row) != 3
                or not all(isinstance(v, str) and v for v in row[:2])):
            raise MigrationError("Invalid canonical source row")
        key = tuple(row[:2])
        if key in result:
            raise MigrationError("Duplicate source record identity")
        canonical(row[2])
        result[key] = row[2]
    return result


def summarize_rows(rows, source_node, site):
    """Bound memory by one canonical row, including on the small Pi."""
    kinds, count, size = {}, 0, 2
    hasher = hashlib.sha256(b"[")
    previous = None
    for kind, key, value in rows:
        pair = (kind, key)
        if previous is not None and pair <= previous:
            raise MigrationError("Canonical rows must be strictly sorted")
        previous = pair
        raw = canonical([kind, key, value])
        if count:
            hasher.update(b",")
            size += 1
        hasher.update(raw)
        size += len(raw)
        count += 1
        entry = kinds.setdefault(kind, {"count": 0, "value_bytes": 0, "states": {}})
        entry["count"] += 1
        entry["value_bytes"] += encoded_size(value)
        if isinstance(value, dict) and isinstance(value.get("state"), str):
            state = value["state"]
            entry["states"][state] = entry["states"].get(state, 0) + 1
    hasher.update(b"]")
    return {"source_node": node_id(source_node), "site": site,
            "records": count, "canonical_bytes": size,
            "digest": hasher.hexdigest(), "kinds": kinds}


def inventory(snapshot):
    rows = rows_of(snapshot)
    result = summarize_rows(([kind, key, value] for (kind, key), value in sorted(rows.items())),
                            snapshot["source_node"], snapshot["site"])
    if "event_delivery" in snapshot:
        delivery = validated_event_delivery(snapshot["event_delivery"])
        result["event_delivery"] = {"digest": digest(delivery),
                                   "tables": {table: len(delivery[table]) for table in sorted(EVENT_TABLES)}}
    return result


def database_inventory(database, source_node, site):
    path = pathlib.Path(database).resolve()
    if not path.is_file():
        raise MigrationError("Source SQLite file is missing")
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("Source SQLite integrity check failed")
        return summarize_rows(([kind, key, json.loads(raw)]
            for kind, key, raw in connection.execute(
                "SELECT kind,id,value FROM records ORDER BY kind,id")), source_node, site)
    finally:
        connection.close()


def snapshot_file(database, source_node, site, output):
    """Stream a consistent snapshot to a new private file without retaining it."""
    path = pathlib.Path(database).resolve()
    if not path.is_file():
        raise MigrationError("Source SQLite file is missing")
    target = pathlib.Path(output)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("Source SQLite integrity check failed")
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b'{"format":' + canonical(FORMAT) + b',"records":[')
            first = True
            def records():
                nonlocal first
                for kind, key, raw in connection.execute(
                        "SELECT kind,id,value FROM records ORDER BY kind,id"):
                    row = [kind, key, json.loads(raw)]
                    if not first:
                        stream.write(b",")
                    stream.write(canonical(row))
                    first = False
                    yield row
            summary = summarize_rows(records(), source_node, site)
            stream.write(b'],"site":' + canonical(site) + b',"source_node":' +
                         canonical(node_id(source_node)) + b"}\n")
            stream.flush()
            os.fsync(stream.fileno())
        return summary
    finally:
        connection.close()


def read_snapshot(database, source_node, site):
    path = pathlib.Path(database).resolve()
    if not path.is_file():
        raise MigrationError("Source SQLite file is missing")
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("Source SQLite integrity check failed")
        rows = [[kind, key, json.loads(raw)] for kind, key, raw in connection.execute(
            "SELECT kind,id,value FROM records ORDER BY kind,id")]
        value = {"format": FORMAT, "source_node": node_id(source_node),
                 "site": site, "records": rows}
        inventory(value)
        return value
    finally:
        connection.close()


def private_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            for chunk in json_chunks(value):
                stream.write(chunk)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Leave any partial file visible for operator inspection; never replace
        # an earlier migration artifact or claim the partial file is valid.
        raise


def _backup_sqlite(database, output):
    source = pathlib.Path(database).resolve()
    target = pathlib.Path(output).resolve()
    if not source.is_file():
        raise MigrationError("Source SQLite file is missing")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    src = sqlite3.connect("file:" + quote(str(source), safe="/") + "?mode=ro", uri=True)
    if not hasattr(src, "backup"):
        src.close()
        raise MigrationError("SQLite backup API requires Python 3.7+; use the pinned runtime container")
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with target.open("rb") as stream:
        sha = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return {"backup_sha256": sha.hexdigest(), "backup_bytes": target.stat().st_size,
            "backup_file": target.name}


def backup(database, output, source_node, site, events_database=None, events_output=None):
    """SQLite backup captures WAL state; optional event sidecar is separately bound."""
    if events_database is not None and not pathlib.Path(events_database).is_file():
        raise MigrationError("Event delivery sidecar is missing")
    if events_output is not None and events_database is None:
        raise MigrationError("Event backup output requires its source sidecar")
    if (events_output is not None and
            pathlib.Path(events_output).resolve().parent != pathlib.Path(output).resolve().parent):
        raise MigrationError("Site and event backups must share the manifest directory")
    copied = _backup_sqlite(database, output)
    result = database_inventory(output, source_node, site)
    result.update(copied)
    if events_database is not None:
        target = pathlib.Path(events_output) if events_output else pathlib.Path(output).with_name(
            pathlib.Path(output).stem + ".events.sqlite")
        event_copy = _backup_sqlite(events_database, target)
        event_copy.update(event_inventory(target), source_node=source_node)
        result["event_delivery_backup"] = event_copy
    return result


EVENT_TABLES = {"subscriptions": ("id",), "receiver_receipts": ("id",),
                "failed_events": ("sub", "event")}


def event_rows(connection, table):
    present = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"subscriptions", "receiver_receipts"} <= present:
        raise MigrationError("Event delivery sidecar schema is missing")
    if table not in present:
        return
    cursor = connection.execute("SELECT * FROM " + table + " ORDER BY " + ",".join(EVENT_TABLES[table]))
    columns = [item[0] for item in cursor.description]
    for row in cursor:
        value = dict(zip(columns, row))
        if any(not isinstance(value.get(key), str) or not value[key] for key in EVENT_TABLES[table]):
            raise MigrationError("Invalid event delivery row identity")
        yield value


def read_event_delivery(database):
    path = pathlib.Path(database).resolve()
    if not path.is_file():
        raise MigrationError("Event delivery sidecar is missing")
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("Event delivery SQLite integrity check failed")
        return {table: list(event_rows(connection, table)) for table in sorted(EVENT_TABLES)}
    finally:
        connection.close()


def event_inventory(database):
    """Read/hash one sidecar row at a time; never print callback URLs or keys."""
    path = pathlib.Path(database).resolve()
    if not path.is_file():
        raise MigrationError("Event delivery sidecar is missing")
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("Event delivery SQLite integrity check failed")
        hasher, counts, first_table = hashlib.sha256(b"{"), {}, True
        for table in sorted(EVENT_TABLES):
            if not first_table:
                hasher.update(b",")
            first_table = False
            hasher.update(canonical(table) + b":[")
            count = 0
            for row in event_rows(connection, table):
                if count:
                    hasher.update(b",")
                hasher.update(canonical(row))
                count += 1
            hasher.update(b"]")
            counts[table] = count
        hasher.update(b"}")
        return {"digest": hasher.hexdigest(), "tables": counts}
    finally:
        connection.close()


def validated_event_delivery(value):
    if not isinstance(value, dict) or set(value) != set(EVENT_TABLES):
        raise MigrationError("All three event delivery tables must be observed explicitly")
    for table, keys in EVENT_TABLES.items():
        rows = value[table]
        if not isinstance(rows, list):
            raise MigrationError("Event delivery table rows must be a list")
        previous = None
        for row in rows:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k] for k in keys):
                raise MigrationError("Invalid event delivery row")
            identity = tuple(row[k] for k in keys)
            if previous is not None and identity <= previous:
                raise MigrationError("Event delivery rows must be uniquely sorted")
            previous = identity
            canonical(row)
    return value


def target_spec(target, sources):
    if not isinstance(target, dict):
        raise MigrationError("Explicit logical authority target required")
    result = {key: target.get(key) for key in (
        "service_id", "authority_id", "site", "controller_aliases")}
    if not isinstance(result["service_id"], str) or not result["service_id"]:
        raise MigrationError("Logical service ID required")
    node_id(result["authority_id"])
    aliases = result["controller_aliases"]
    if (not isinstance(aliases, list) or len(set(aliases)) != len(aliases)
            or set(aliases) != set(sources)):
        raise MigrationError("Target aliases must exactly cover all source controllers")
    for alias in aliases:
        node_id(alias)
    if result["authority_id"] in sources:
        raise MigrationError("Logical authority must not clone a physical controller identity")
    if not isinstance(result["site"], str) or not result["site"]:
        raise MigrationError("Target site required")
    result["controller_aliases"] = sorted(aliases)
    return result


def event_domain_blockers(rows):
    blockers = []
    for (kind, key), value in sorted(rows.items()):
        if kind == "subscriptions":
            blockers.append([kind, key, "subscription cursor requires explicit migration"])
        elif kind == "runs" and value.get("state") not in TERMINAL_RUNS:
            blockers.append([kind, key, "nonterminal run may own event cursors"])
        elif kind == "waits":
            blockers.append([kind, key, "wait requires explicit migration"])
        elif kind.startswith("assistant_") and kind not in ("assistant_config",):
            blockers.append([kind, key, "assistant event domain requires explicit migration"])
    return blockers


def _event_local(kind, key, value):
    return (kind in EVENT_KINDS or kind == "_meta" and key in EVENT_META
            or kind == "_retention" and isinstance(value, dict)
            and value.get("kind") in EVENT_KINDS)


def _equivalent_receipts(values):
    ignored = {"created_at", "expires_at"}
    return len({digest({k: v for k, v in value.items() if k not in ignored})
                for value in values}) == 1


def _artifact_merge(key, values):
    for value in values:
        if not isinstance(value, dict) or value.get("id") != key:
            raise MigrationError("Artifact identity mismatch")
        try:
            raw = base64.b64decode(value["data"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise MigrationError("Invalid artifact bytes") from exc
        if hashlib.sha256(raw).hexdigest() != key:
            raise MigrationError("Artifact content digest mismatch")
    same = [{k: v for k, v in value.items() if k != "expires_at"} for value in values]
    if len({digest(value) for value in same}) != 1:
        return None
    result = copy.deepcopy(values[0])
    if any(value.get("expires_at") is None for value in values):
        result.pop("expires_at", None)
    else:
        result["expires_at"] = max(value["expires_at"] for value in values)
    return result


def _build(snapshots, baseline, target, migration_id, policy):
    if not isinstance(migration_id, str) or not re.fullmatch("[A-Za-z0-9_.:-]{1,128}", migration_id):
        raise MigrationError("Stable migration ID required")
    if not isinstance(policy, dict) or set(policy) - {"retire_event_sources", "resolutions"}:
        raise MigrationError("Unknown migration policy field")
    sources = {}
    for snapshot in snapshots:
        source = snapshot.get("source_node")
        if source in sources:
            raise MigrationError("Duplicate controller source")
        sources[source] = (snapshot, rows_of(snapshot))
    if len(sources) < 2 or baseline not in sources:
        raise MigrationError("At least two source controllers and an exact baseline required")
    target = target_spec(target, sources)
    if any(snapshot["site"] != target["site"] for snapshot, unused in sources.values()):
        raise MigrationError("Sources and target must belong to the same site")
    retired = policy.get("retire_event_sources", [])
    if (not isinstance(retired, list) or len(set(retired)) != len(retired)
            or baseline in retired or set(retired) - set(sources)):
        raise MigrationError("Only explicit secondary event domains can be retired")
    decisions = policy.get("resolutions", [])
    if not isinstance(decisions, list):
        raise MigrationError("Explicit collision resolution list required")
    selectors, used = {}, set()
    for entry in decisions:
        if (not isinstance(entry, dict) or set(entry) != {"kind", "id", "source"}
                or not isinstance(entry["kind"], str) or not isinstance(entry["id"], str)
                or entry["source"] not in sources):
            raise MigrationError("Resolution must bind exact kind, ID and observed source")
        key = (entry["kind"], entry["id"])
        if key in selectors:
            raise MigrationError("Duplicate collision resolution")
        selectors[key] = entry["source"]
    report = {"format": FORMAT, "migration_id": migration_id, "baseline": baseline,
              "target": target, "policy": copy.deepcopy(policy),
              "sources": [inventory(sources[source][0]) for source in sorted(sources)],
              "collisions": [], "blocked": [], "archive_records": 0}
    for source, (snapshot, rows) in sorted(sources.items()):
        if "event_delivery" not in snapshot:
            report["blocked"].append({"source": source, "kind": "event_delivery", "id": "",
                "reason": "Explicit event delivery sidecar observation is required"})
        for (kind, key), value in sorted(rows.items()):
            if (kind in ("_session_scope", "_ownership_pending")
                    or kind == "_coordination" and key in ("active", "pending")
                    or kind == "_receipt" and isinstance(value, dict)
                    and value.get("external_pending") is True):
                report["blocked"].append({"source": source, "kind": kind, "id": key,
                    "reason": "Unreconciled source coordination protocol must finish before migration"})
    for source in retired:
        report["blocked"].extend({"source": source, "kind": kind, "id": key, "reason": reason}
                                 for kind, key, reason in event_domain_blockers(sources[source][1]))
    candidates = {}
    archived = {}
    def preserve(source, pair, value, reason):
        archive_id = digest([migration_id, source, pair[0], pair[1], digest(value)])
        archived.setdefault((ARCHIVE, archive_id), {
            "id": archive_id, "migration_id": migration_id, "source_controller": source,
            "kind": pair[0], "key": pair[1], "record": value,
            "value_digest": digest(value), "disposition": reason})
    for source, (snapshot, rows) in sorted(sources.items()):
        for pair, value in sorted(rows.items()):
            if source in retired and _event_local(*pair, value):
                preserve(source, pair, value, "retired_secondary_event_domain")
            else:
                candidates.setdefault(pair, []).append((source, value))
    merged = {}
    for pair, entries in sorted(candidates.items()):
        kind, key = pair
        entries.sort(key=lambda entry: (entry[0] != baseline, entry[0]))
        values = [value for unused, value in entries]
        fingerprints = {digest(value) for value in values}
        if len(fingerprints) == 1:
            merged[pair] = values[0]
            continue
        detail = {"kind": kind, "id": key,
                  "sources": [{"source": source, "digest": digest(value)}
                              for source, value in entries]}
        chosen, method = None, None
        if kind in ("_writers", "_fence_generations") and all(type(value) is int and value >= 0 for value in values):
            chosen, method = max(values), "monotonic_floor"
        elif kind == "artifacts":
            chosen = _artifact_merge(key, values)
            if chosen is not None:
                method = "identical_content_durable_lifetime"
        elif kind == "documents":
            # Heads are rebuilt after all immutable revisions have been merged.
            chosen, method = {"id": key, "heads": sorted(set(
                head for value in values for head in value["heads"]))}, "revision_dag_heads"
        elif kind == "_receipt" and all(isinstance(value, dict) for value in values):
            if _equivalent_receipts(values):
                chosen, method = copy.deepcopy(values[0]), "equivalent_receipt"
            else:
                chosen = {field: copy.deepcopy(values[0][field])
                          for field in ("signature", "operation", "format", "created_at")
                          if field in values[0]}
                chosen.update(result_expired=True, migration_conflict=migration_id)
                merged[(RECEIPT_CONFLICTS, key)] = {
                    "id": key, "migration_id": migration_id, "replay_allowed": False,
                    "sources": detail["sources"]}
                method = "ambiguous_receipt_blocked"
        elif kind == "_receipt_stream" and all(
                isinstance(value, dict) and type(value.get("sequence")) is int for value in values):
            floor = max(value["sequence"] for value in values)
            latest = [value for value in values if value["sequence"] == floor]
            chosen = copy.deepcopy(latest[0])
            if len({digest(value) for value in latest}) != 1:
                chosen.pop("result", None)
                chosen["migration_conflict"] = migration_id
                merged[(STREAM_CONFLICTS, key)] = {
                    "id": key, "migration_id": migration_id, "floor": floor,
                    "replay_allowed": False, "sources": detail["sources"]}
                method = "ambiguous_ordered_receipt_blocked"
            else:
                method = "ordered_receipt_floor"
        elif pair in selectors:
            selected = [(source, value) for source, value in entries if source == selectors[pair]]
            if not selected:
                raise MigrationError("Resolution source does not own the collision")
            chosen, method = copy.deepcopy(selected[0][1]), "explicit_source"
            detail["selected_source"] = selected[0][0]
            used.add(pair)
        if method is None:
            detail["resolution"] = "required"
            report["blocked"].append({"kind": kind, "id": key,
                                     "reason": "Ambiguous canonical record requires explicit source resolution"})
        else:
            merged[pair] = chosen
            detail["resolution"] = method
        report["collisions"].append(detail)
        for source, value in entries:
            if chosen is None or digest(value) != digest(chosen):
                preserve(source, pair, value, method or "unresolved_collision")
    for pair in selectors.keys() - used:
        raise MigrationError("Resolution does not select an unresolved mutable collision: " + repr(pair))
    # Reject missing DAG dependencies; never invent a knowledge revision.
    revisions = {key: value for (kind, key), value in merged.items() if kind == "revisions"}
    for pair, value in list(merged.items()):
        if pair[0] != "documents":
            continue
        if not isinstance(value, dict) or not isinstance(value.get("heads"), list):
            report["blocked"].append({"kind": pair[0], "id": pair[1], "reason": "Malformed document heads"})
            continue
        heads = set(value["heads"])
        if any(head not in revisions or revisions[head].get("key") != pair[1] for head in heads):
            report["blocked"].append({"kind": pair[0], "id": pair[1], "reason": "Document head revision missing or mismatched"})
            continue
        parents = {parent for revision in revisions.values() if revision.get("key") == pair[1]
                   for parent in revision.get("parents", [])}
        heads.difference_update(parents)
        merged[pair] = dict(value, heads=sorted(heads), conflict=len(heads) > 1)
        for source, original in candidates[pair]:
            if digest(original) != digest(merged[pair]):
                preserve(source, pair, original, "revision_dag_heads")
    # Preserve each producer's original numeric cursor lineage, and import
    # delivery state separately for each original controller alias.
    def adopt(pair, value):
        if pair in merged and digest(merged[pair]) != digest(value):
            report["blocked"].append({"kind": pair[0], "id": pair[1],
                                     "reason": "Derived migration record conflicts with retained state"})
        else:
            merged[pair] = value
    for source, (snapshot, source_rows) in sorted(sources.items()):
        event_high = source_rows.get(("_meta", "events"), 0)
        delegation_high = source_rows.get(("_meta", "delegation_sequence"), 0)
        logical_high = merged.get(("_meta", "events"), 0)
        logical_delegation_high = merged.get(("_meta", "delegation_sequence"), 0)
        retention = source_rows.get(("_meta", "operational_retention"), {})
        event_floor = retention.get("event_floor", 0) if isinstance(retention, dict) else 0
        numbers = (event_high, delegation_high, logical_high, logical_delegation_high, event_floor)
        if any(type(value) is not int or value < 0 for value in numbers) or event_floor > event_high:
            report["blocked"].append({"source": source, "kind": "_meta", "id": "events",
                                     "reason": "Invalid original or logical feed cursor"})
            continue
        adopt(("_controller_source_feeds", source), {
            "source_controller": source,
            "events": {"high": event_high, "logical_high": logical_high, "floor": event_floor},
            "delegation_events": {"high": delegation_high, "logical_high": logical_delegation_high, "floor": 0}})
        for (kind, key), value in sorted(source_rows.items()):
            if kind not in EVENT_KINDS:
                continue
            high = event_high if kind == "events" else delegation_high
            if (not isinstance(value, dict) or type(value.get("seq")) is not int
                    or not 1 <= value["seq"] <= high or key != "%020d" % value["seq"]):
                report["blocked"].append({"source": source, "kind": kind, "id": key,
                                         "reason": "Original feed row does not match its cursor lineage"})
                continue
            target_kind = "_controller_source_" + kind
            adopt((target_kind, source + ":" + key), value)
        if "event_delivery" not in snapshot:
            continue
        delivery = validated_event_delivery(snapshot["event_delivery"])
        local_subscriptions = set()
        for row in delivery["subscriptions"]:
            try:
                arguments = json.loads(row["args"])
            except (KeyError, TypeError, ValueError):
                report["blocked"].append({"source": source, "kind": "event_delivery.subscriptions", "id": row["id"],
                                         "reason": "Subscription filter JSON is invalid"})
                continue
            if not isinstance(arguments, dict) or arguments.get("realm", "site") not in ("site", "local"):
                report["blocked"].append({"source": source, "kind": "event_delivery.subscriptions", "id": row["id"],
                                         "reason": "Subscription realm is invalid"})
                continue
            if arguments.get("realm", "site") == "local":
                local_subscriptions.add(row["id"])
                preserve(source, ("event_delivery.subscriptions", row["id"]), row, "native_local_subscription")
                continue
            adopt(("_gateway_event_subscriptions", digest([source, row["id"]])),
                  {"source_controller": source, "record": row})
        for row in delivery["receiver_receipts"]:
            try:
                result = json.loads(row["result"])
            except (KeyError, TypeError, ValueError):
                result = None
            if isinstance(result, dict) and result.get("realm") == "local":
                preserve(source, ("event_delivery.receiver_receipts", row["id"]), row, "native_local_receipt")
                continue
            adopt(("_gateway_event_receipts", digest([source, row["id"]])),
                  {"source_controller": source, "record": row})
        for row in delivery["failed_events"]:
            if row["sub"] in local_subscriptions:
                preserve(source, ("event_delivery.failed_events", digest([row["sub"], row["event"]])),
                         row, "native_local_delivery_failure")
                continue
            adopt(("_gateway_event_failures", digest([source, row["sub"], row["event"]])),
                  {"source_controller": source, "record": row})

    # Preserve every source row exactly either in canonical state or provenance.
    for source, (snapshot, rows) in sources.items():
        for pair, value in rows.items():
            if pair not in merged or digest(merged[pair]) != digest(value):
                preserve(source, pair, value, "source_original")
    merged.update(archived)
    report["archive_records"] = len(archived)
    report["state_records"] = len(merged)
    report["ready"] = not report["blocked"]
    report["plan_digest"] = digest(report)
    return report, merged


def make_plan(snapshots, baseline, target, migration_id, policy=None):
    return _build(snapshots, baseline, target, migration_id, policy or {})[0]


def freeze_marker(snapshot, target, migration_id):
    sources = target.get("controller_aliases", [])
    target = target_spec(target, sources)
    summary = inventory(snapshot)
    if summary["source_node"] not in sources or summary["site"] != target["site"]:
        raise MigrationError("Freeze source differs from logical target")
    return dict(format=1, mode="frozen-source", source_node=summary["source_node"],
                site=summary["site"], service_id=target["service_id"],
                authority_id=target["authority_id"],
                controller_aliases=target["controller_aliases"],
                migration_id=migration_id, source_digest=summary["digest"])


def export_state(snapshots, plan, markers):
    expected, rows = _build(snapshots, plan["baseline"], plan["target"],
                            plan["migration_id"], plan["policy"])
    if expected != plan:
        raise MigrationError("Source or reviewed plan changed")
    if not plan["ready"]:
        raise MigrationError("Unresolved migration collisions or event-domain consumers")
    by_source = {}
    for marker in markers:
        if not isinstance(marker, dict) or marker.get("source_node") in by_source:
            raise MigrationError("Exactly one freeze marker per source required")
        by_source[marker.get("source_node")] = marker
    if set(by_source) != {snapshot["source_node"] for snapshot in snapshots}:
        raise MigrationError("All source admission fences required")
    for snapshot in snapshots:
        expected_marker = freeze_marker(snapshot, plan["target"], plan["migration_id"])
        if by_source[snapshot["source_node"]] != expected_marker:
            raise MigrationError("Freeze marker does not bind this exact source and migration")
    provenance = {"id": plan["migration_id"], "plan_digest": plan["plan_digest"],
                  "sources": plan["sources"], "policy": plan["policy"],
                  "collisions": plan["collisions"], "target": plan["target"],
                  "source_fences": [by_source[source] for source in sorted(by_source)],
                  "replay_allowed_for_archived_records": False}
    rows[("_controller_migrations", plan["migration_id"])] = provenance
    state = {"authority": plan["target"]["authority_id"], "site": plan["target"]["site"],
             "records": [[kind, key, value] for (kind, key), value in sorted(rows.items())]}
    authorization = {"source_receipt": {
        "format": FORMAT, "migration_id": plan["migration_id"],
        "plan_digest": plan["plan_digest"], "state_digest": digest(state),
        "target": plan["target"],
        "source_digests": {item["source_node"]: item["digest"] for item in plan["sources"]},
        "source_fence_digests": {source: digest(marker) for source, marker in sorted(by_source.items())},
        "physical_effect_outcomes_inferred": False}}
    return {"state": state, "authorization": authorization}


def chunk_state(state, chunks):
    """Package canonical rows once; at most one shard is serialized at a time."""
    if (not isinstance(state, dict) or not isinstance(state.get("records"), list)
            or not isinstance(state.get("authority"), str) or not isinstance(state.get("site"), str)):
        raise MigrationError("Canonical controller state required")
    buckets = {}
    previous = None
    for row in state["records"]:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(x, str) for x in row[:2]):
            raise MigrationError("Canonical controller row required")
        kind, key, unused = row
        pair = (kind, key)
        if previous is not None and pair <= previous:
            raise MigrationError("Controller rows must be strictly sorted")
        previous = pair
        prefix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:2]
        buckets.setdefault((kind, prefix), []).append(row)
    shards = []
    for (kind, prefix), rows in sorted(buckets.items()):
        shards.append(chunks.write_shard(kind, prefix, canonical(rows)))
    return {"format": "biomerte-controller-state/1", "authority": state["authority"],
            "site": state["site"], "shards": shards}


def bootstrap_controller(exported, config):
    """Create a new staging checkpoint and its local content-addressed blocks."""
    if (config.get("authority_kind") != "controller"
            or config.get("authority_id") != exported["state"]["authority"]
            or config.get("site") != exported["state"]["site"]):
        raise MigrationError("Bootstrap config differs from reviewed controller target")
    target = exported.get("authorization", {}).get("source_receipt", {}).get("target")
    if (not isinstance(target, dict) or config.get("service_id") != target.get("service_id")
            or sorted(config.get("controller_aliases", [])) != target.get("controller_aliases")):
        raise MigrationError("Bootstrap service scope differs from reviewed migration target")
    path = pathlib.Path(config["checkpoint_db"])
    if os.path.lexists(str(path)):
        raise MigrationError("Use a new staging checkpoint path, never replace live helper state")
    import sys
    root = str(pathlib.Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from biomerte.service_checkpoint import CheckpointStore
    from biomerte.service_daemon import scope_digest
    key = pathlib.Path(config["checkpoint_key_file"]).read_text(encoding="utf-8").strip().encode()
    store = CheckpointStore(str(path), config["service_id"], scope_digest(config), key)
    try:
        manifest = chunk_state(exported["state"], store.chunks)
        authorization = copy.deepcopy(exported["authorization"])
        authorization["source_receipt"]["checkpoint_state_digest"] = digest(manifest)
        return store.bootstrap(manifest, authorization)
    finally:
        store.close()


def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def load_source(path):
    """Accept a snapshot or a copied backup manifest beside its SQLite payload."""
    path = pathlib.Path(path)
    value = load_json(path)
    if isinstance(value, dict) and value.get("format") == "biomerte-controller-source-chunks/1":
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "controller_source_chunks", pathlib.Path(__file__).with_name("source_chunks.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.load_manifest(path)
    if (isinstance(value, dict) and value.get("format") == FORMAT
            and isinstance(value.get("records"), list)):
        return value
    filename = value.get("backup_file") if isinstance(value, dict) else None
    if not isinstance(filename, str) or not filename or pathlib.Path(filename).name != filename:
        raise MigrationError("Snapshot or private backup manifest required")
    database = path.parent / filename
    hasher = hashlib.sha256()
    with database.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != value.get("backup_sha256"):
        raise MigrationError("Backup file bytes differ from the retained manifest")
    snapshot = read_snapshot(database, value["source_node"], value["site"])
    if inventory(snapshot)["digest"] != value.get("digest"):
        raise MigrationError("Backup canonical records differ from the retained manifest")
    event_copy = value.get("event_delivery_backup")
    if event_copy is not None:
        event_file = event_copy.get("backup_file") if isinstance(event_copy, dict) else None
        if (not isinstance(event_file, str) or not event_file or pathlib.Path(event_file).name != event_file
                or event_copy.get("source_node") != snapshot["source_node"]):
            raise MigrationError("Event backup manifest does not bind the same source")
        event_path = path.parent / event_file
        event_hash = hashlib.sha256()
        with event_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                event_hash.update(chunk)
        if event_hash.hexdigest() != event_copy.get("backup_sha256"):
            raise MigrationError("Event backup bytes differ from the retained manifest")
        delivery = read_event_delivery(event_path)
        if digest(delivery) != event_copy.get("digest"):
            raise MigrationError("Event delivery records differ from the retained manifest")
        snapshot["event_delivery"] = delivery
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "backup"):
        command = sub.add_parser(name)
        command.add_argument("--home", required=True)
        command.add_argument("--database")
        command.add_argument("--output", required=True)
        if name == "backup":
            command.add_argument("--manifest", required=True)
            command.add_argument("--events-database")
            command.add_argument("--events-output")
    command = sub.add_parser("plan")
    command.add_argument("--source", action="append", required=True)
    command.add_argument("--baseline", required=True)
    command.add_argument("--target", required=True)
    command.add_argument("--migration-id", required=True)
    command.add_argument("--policy")
    command.add_argument("--output", required=True)
    command = sub.add_parser("marker")
    command.add_argument("--source", required=True)
    command.add_argument("--target", required=True)
    command.add_argument("--migration-id", required=True)
    command.add_argument("--output", required=True)
    for name in ("export", "bootstrap"):
        command = sub.add_parser(name)
        command.add_argument("--source", action="append", required=True)
        command.add_argument("--plan", required=True)
        command.add_argument("--marker", action="append", required=True)
        command.add_argument("--output", required=True)
        if name == "bootstrap":
            command.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.command in ("snapshot", "backup"):
        config = load_json(pathlib.Path(args.home) / "node.json")
        database = args.database or str(pathlib.Path(args.home) / "site.sqlite")
        if args.command == "snapshot":
            value = snapshot_file(database, config["node"], config["site"], args.output)
            print(json.dumps(value, sort_keys=True))
        else:
            if pathlib.Path(args.manifest).resolve().parent != pathlib.Path(args.output).resolve().parent:
                raise MigrationError("Backup manifest must be beside its SQLite payload")
            value = backup(database, args.output, config["node"], config["site"],
                           args.events_database, args.events_output)
            private_json(args.manifest, value)
            print(json.dumps(value, sort_keys=True))
    elif args.command == "plan":
        value = make_plan([load_source(p) for p in args.source], args.baseline,
                          load_json(args.target), args.migration_id,
                          load_json(args.policy) if args.policy else {})
        private_json(args.output, value)
        print(json.dumps({"ready": value["ready"], "plan_digest": value["plan_digest"],
                          "collisions": len(value["collisions"]), "blocked": value["blocked"],
                          "archive_records": value["archive_records"]}, sort_keys=True))
    elif args.command == "marker":
        value = freeze_marker(load_source(args.source), load_json(args.target), args.migration_id)
        private_json(args.output, value)
        print(json.dumps({"source_node": value["source_node"],
                          "source_digest": value["source_digest"]}, sort_keys=True))
    else:
        value = export_state([load_source(p) for p in args.source], load_json(args.plan),
                             [load_json(p) for p in args.marker])
        if args.command == "bootstrap":
            packet = bootstrap_controller(value, load_json(args.config))
            private_json(args.output, packet)
            manifest = packet["payload"]["state"]
            print(json.dumps({"checkpoint_digest": packet["digest"],
                "state_digest": digest(value["state"]),
                "records": len(value["state"]["records"]),
                "shards": len(manifest["shards"]),
                "root_packet_bytes": encoded_size(packet)}, sort_keys=True))
        else:
            private_json(args.output, value)
            print(json.dumps({"state_digest": digest(value["state"]),
                              "records": len(value["state"]["records"]),
                              "canonical_bytes": encoded_size(value["state"])}, sort_keys=True))


if __name__ == "__main__":
    main()
