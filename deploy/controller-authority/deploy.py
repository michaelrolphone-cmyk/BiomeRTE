"""Local source-fence installation; no deployment or source database writes.

Execute only through independently proven maintenance. Old unpatched writers
must already be stopped. A marker receipt does not claim a running process
loaded the guard; verify its actual native admission rejection separately.
"""
import argparse
import importlib.util
import json
import os
import pathlib
import sqlite3
import uuid
from urllib.parse import quote

SPEC = importlib.util.spec_from_file_location(
    "controller_state_migration", pathlib.Path(__file__).with_name("migrate.py"))
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def atomic_private_marker(path, value):
    """Install complete private bytes atomically, refusing any prior marker."""
    path = pathlib.Path(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            for chunk in migration.json_chunks(value):
                stream.write(chunk)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(str(temporary), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink()


def install_source_marker(home, marker, database=None):
    home = pathlib.Path(home)
    config = migration.load_json(home / "node.json")
    required = {"format", "mode", "source_node", "site", "service_id", "authority_id",
                "controller_aliases", "migration_id", "source_digest"}
    if (not isinstance(marker, dict) or set(marker) != required
            or marker["format"] != 1 or marker["mode"] != "frozen-source"
            or marker["source_node"] != config["node"] or marker["site"] != config["site"]
            or not isinstance(marker["migration_id"], str) or not marker["migration_id"]):
        raise migration.MigrationError("Marker must bind this exact native source")
    target = {key: marker[key] for key in ("service_id", "authority_id", "site", "controller_aliases")}
    migration.target_spec(target, marker["controller_aliases"])
    if marker["source_node"] not in marker["controller_aliases"]:
        raise migration.MigrationError("Source is not a declared native alias")
    target_file = home / "controller-authority.json"
    if os.path.lexists(str(target_file)):
        if target_file.is_symlink() or migration.load_json(target_file) != marker:
            raise migration.MigrationError("Source already has a different marker")
    source = pathlib.Path(database) if database is not None else home / "site.sqlite"
    if not source.is_file():
        raise migration.MigrationError("Source SQLite database is missing")
    connection = sqlite3.connect("file:" + quote(str(source.resolve()), safe="/") + "?mode=rw",
                                 uri=True, isolation_level=None)
    created = False
    try:
        # Patched Store checks its write guard after acquiring this same lock.
        # No writer can pass between the canonical hash and marker installation.
        connection.execute("BEGIN IMMEDIATE")
        summary = migration.summarize_rows(
            ([kind, key, json.loads(raw)] for kind, key, raw in connection.execute(
                "SELECT kind,id,value FROM records ORDER BY kind,id")),
            config["node"], config["site"])
        if summary["digest"] != marker["source_digest"]:
            raise migration.MigrationError("Source changed after the reviewed backup; prepare a new plan")
        if not os.path.lexists(str(target_file)):
            atomic_private_marker(target_file, marker)
            created = True
        elif target_file.is_symlink() or migration.load_json(target_file) != marker:
            raise migration.MigrationError("Source marker changed concurrently")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return {"marker_installed": True, "created": created,
            "source_node": marker["source_node"], "source_digest": marker["source_digest"],
            "migration_id": marker["migration_id"], "marker_digest": migration.digest(marker),
            "runtime_admission_verified": False,
            "next": "Verify patched native site admission rejects writes through independent maintenance"}


def verify_replica(config, expected):
    """Verify current root and every canonical shard without installing state."""
    if config.get("authority_kind") != "controller":
        raise migration.MigrationError("Controller replica configuration required")
    path = pathlib.Path(config["checkpoint_db"])
    if not path.is_file():
        raise migration.MigrationError("Replica checkpoint database is missing")
    import sys
    root = str(pathlib.Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from biomerte.service_checkpoint import CheckpointStore, checkpoint_ref
    from biomerte.checkpoint_chunks import iter_records
    from biomerte.service_daemon import scope_digest
    if isinstance(expected, dict) and "payload" in expected:
        expected = checkpoint_ref(expected)
    if (not isinstance(expected, dict)
            or set(expected) != {"service_id", "sequence", "digest"}
            or expected["service_id"] != config["service_id"]):
        raise migration.MigrationError("Exact independently observed checkpoint reference required")
    key = pathlib.Path(config["checkpoint_key_file"]).read_text(encoding="utf-8").strip().encode()
    store = CheckpointStore(str(path), config["service_id"], scope_digest(config), key)
    try:
        packet = store.packet()
        if packet is None or checkpoint_ref(packet) != expected:
            raise migration.MigrationError("Replica does not hold the expected current checkpoint")
        payload = store.verify(packet)
        state = payload["state"]
        if (state.get("format") != "biomerte-controller-state/1"
                or state.get("authority") != config["authority_id"] or state.get("site") != config["site"]):
            raise migration.MigrationError("Replica state identity differs from controller configuration")
        store.require_chunks(state)
        counts = {}
        for kind, unused, value in iter_records(state, store.chunks.get):
            counts[kind] = counts.get(kind, 0) + 1
        return {"verified": True, "checkpoint": checkpoint_ref(packet),
                "records": sum(counts.values()), "kinds": counts,
                "shards": len(state["shards"]), "state_digest": migration.digest(state),
                "application_effects_executed": False}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--home", required=True)
    freeze.add_argument("--marker", required=True)
    freeze.add_argument("--database")
    freeze.add_argument("--receipt", required=True)
    verify = sub.add_parser("verify-replica")
    verify.add_argument("--config", required=True)
    verify.add_argument("--expected", required=True)
    verify.add_argument("--receipt", required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = install_source_marker(args.home, migration.load_json(args.marker), args.database)
    else:
        result = verify_replica(migration.load_json(args.config), migration.load_json(args.expected))
    migration.private_json(args.receipt, result)
    print(json.dumps(result, sort_keys=True))



if __name__ == "__main__":
    main()
