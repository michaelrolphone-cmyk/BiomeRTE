"""Detached source transfer using existing authenticated checkpoint chunk transport.

No source package is a runtime checkpoint, installed head, or effect admission.
The original controller signs its own row inventory and compressed byte stream.
Run packing beside its frozen private backup; run merge/pull on the larger host.
"""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import sys
import zlib
from urllib.parse import quote

HERE = pathlib.Path(__file__).resolve().parent
ROOT = str(HERE.parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SPEC = importlib.util.spec_from_file_location("source_transfer_migration", HERE / "migrate.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)
from biomerte import identity

FORMAT = "biomerte-controller-source-chunks/1"
BLOCK_BYTES = 262144
BINDING_FIELDS = ("service_id", "manifest_revision", "worker_id",
                  "worker_incarnation", "boot_epoch", "grant_token")
Error = migration.MigrationError


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise Error("Exact SHA256 required")
    return value


def _block(raw, expected=None):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= BLOCK_BYTES:
        raise Error("Bounded nonempty source block required")
    result = {"digest": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if expected is not None and result != expected:
        raise Error("Source block bytes differ from signed manifest")
    return result


def verify_packet(packet, expected_source=None):
    if not isinstance(packet, dict) or set(packet) != {"payload", "signature", "public_key"}:
        raise Error("Signed detached source packet required")
    payload = packet["payload"]
    required = {"format", "encoding", "source", "chunks", "compressed_bytes",
                "compressed_sha256", "decoded_bytes", "decoded_sha256"}
    if (not isinstance(payload, dict) or set(payload) != required or payload["format"] != FORMAT
            or payload["encoding"] != "gzip-json-lines/1" or not isinstance(payload["source"], dict)):
        raise Error("Detached source format required")
    source = migration.node_id(payload["source"].get("source_node"))
    if expected_source is not None and source != migration.node_id(expected_source):
        raise Error("Source packet belongs to another controller")
    if identity.fingerprint(packet["public_key"]) != source:
        raise Error("Source signing key differs from its original controller")
    identity.verify(packet["public_key"], packet["signature"], payload)
    if (not isinstance(payload["chunks"], list) or not payload["chunks"]
            or any(type(payload[k]) is not int or payload[k] <= 0
                   for k in ("compressed_bytes", "decoded_bytes"))):
        raise Error("Nonempty bounded source stream required")
    for key in ("compressed_sha256", "decoded_sha256"):
        _sha(payload[key])
    total = 0
    for part in payload["chunks"]:
        if (not isinstance(part, dict) or set(part) != {"digest", "bytes"}
                or type(part["bytes"]) is not int or not 0 < part["bytes"] <= BLOCK_BYTES):
            raise Error("Invalid source block descriptor")
        _sha(part["digest"])
        total += part["bytes"]
    if total != payload["compressed_bytes"]:
        raise Error("Source compressed length does not match its blocks")
    return payload


def _backup_file(directory, metadata):
    name = metadata.get("backup_file") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name or pathlib.Path(name).name != name:
        raise Error("Private copied backup filename required")
    path = pathlib.Path(directory) / name
    size, hasher = 0, hashlib.sha256()
    with path.open("rb") as stream:
        for raw in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(raw)
            hasher.update(raw)
    if size != metadata.get("backup_bytes") or hasher.hexdigest() != metadata.get("backup_sha256"):
        raise Error("Private backup bytes differ from their source manifest")
    return path


def pack_backup(manifest_path, home, chunks):
    """Stream a source's copied SQLite rows directly into compressed blocks."""
    manifest_path, home = pathlib.Path(manifest_path), pathlib.Path(home)
    metadata = migration.load_json(manifest_path)
    config = migration.load_json(home / "node.json")
    if (metadata.get("source_node") != config.get("node") or metadata.get("site") != config.get("site")
            or identity.fingerprint(config["public_key"]) != config["node"]):
        raise Error("Backup must be signed by its original controller identity")
    event_metadata = metadata.get("event_delivery_backup")
    if not isinstance(event_metadata, dict) or event_metadata.get("source_node") != config["node"]:
        raise Error("Observed event delivery backup required")
    database = _backup_file(manifest_path.parent, metadata)
    events_database = _backup_file(manifest_path.parent, event_metadata)
    compressor = zlib.compressobj(level=6, wbits=31)
    compressed_hash, decoded_hash = hashlib.sha256(), hashlib.sha256()
    compressed_bytes, decoded_bytes = 0, 0
    buffered, parts = bytearray(), []

    def compressed(raw):
        nonlocal compressed_bytes
        if not raw:
            return
        compressed_hash.update(raw)
        compressed_bytes += len(raw)
        buffered.extend(raw)
        while len(buffered) >= BLOCK_BYTES:
            block = bytes(buffered[:BLOCK_BYTES])
            del buffered[:BLOCK_BYTES]
            descriptor = _block(block)
            chunks.put(block, expected_digest=descriptor["digest"])
            parts.append(descriptor)

    def line(value):
        nonlocal decoded_bytes
        for raw in migration.json_chunks(value):
            decoded_hash.update(raw)
            decoded_bytes += len(raw)
            compressed(compressor.compress(raw))
        decoded_hash.update(b"\n")
        decoded_bytes += 1
        compressed(compressor.compress(b"\n"))

    connection = sqlite3.connect("file:" + quote(str(database.resolve()), safe="/") + "?mode=ro", uri=True)
    events = sqlite3.connect("file:" + quote(str(events_database.resolve()), safe="/") + "?mode=ro", uri=True)
    try:
        for current in (connection, events):
            current.execute("BEGIN")
            if current.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise Error("Source backup integrity check failed")

        def rows():
            for kind, key, raw in connection.execute("SELECT kind,id,value FROM records ORDER BY kind,id"):
                value = json.loads(raw)
                line(["record", kind, key, value])
                yield [kind, key, value]
        summary = migration.summarize_rows(rows(), config["node"], config["site"])
        for key, value in summary.items():
            if metadata.get(key) != value:
                raise Error("Source records changed from their copied backup manifest")
        event_hash, table_counts, first = hashlib.sha256(b"{"), {}, True
        for table in sorted(migration.EVENT_TABLES):
            if not first:
                event_hash.update(b",")
            first = False
            event_hash.update(migration.canonical(table) + b":[")
            line(["event_table", table])
            count = 0
            for row in migration.event_rows(events, table):
                if count:
                    event_hash.update(b",")
                event_hash.update(migration.canonical(row))
                line(["event", table, row])
                count += 1
            event_hash.update(b"]")
            table_counts[table] = count
        event_hash.update(b"}")
        observed = {"digest": event_hash.hexdigest(), "tables": table_counts}
        if any(event_metadata.get(key) != value for key, value in observed.items()):
            raise Error("Event rows changed from their copied backup manifest")
        summary["event_delivery"] = observed
    finally:
        events.close()
        connection.close()
    compressed(compressor.flush())
    if buffered:
        raw = bytes(buffered)
        descriptor = _block(raw)
        chunks.put(raw, expected_digest=descriptor["digest"])
        parts.append(descriptor)
    payload = {"format": FORMAT, "encoding": "gzip-json-lines/1", "source": summary,
               "chunks": parts, "compressed_bytes": compressed_bytes,
               "compressed_sha256": compressed_hash.hexdigest(),
               "decoded_bytes": decoded_bytes, "decoded_sha256": decoded_hash.hexdigest()}
    packet = identity.signed(str(home / "node.key"), payload)
    packet["public_key"] = config["public_key"]
    verify_packet(packet, config["node"])
    return packet


def _lines(payload, get_chunk):
    decompressor = zlib.decompressobj(wbits=31)
    compressed_hash, decoded_hash = hashlib.sha256(), hashlib.sha256()
    compressed_bytes, decoded_bytes = 0, 0
    buffered = bytearray()
    for part in payload["chunks"]:
        raw = get_chunk(part["digest"])
        _block(raw, part)
        compressed_hash.update(raw)
        compressed_bytes += len(raw)
        while True:
            decoded = decompressor.decompress(raw, 1024 * 1024)
            raw = decompressor.unconsumed_tail
            if decompressor.unused_data:
                raise Error("Trailing compressed source data is not allowed")
            decoded_hash.update(decoded)
            decoded_bytes += len(decoded)
            if decoded_bytes > payload["decoded_bytes"]:
                raise Error("Source decompression exceeds its signed byte count")
            buffered.extend(decoded)
            while True:
                position = buffered.find(b"\n")
                if position < 0:
                    break
                line = bytes(buffered[:position])
                del buffered[:position + 1]
                value = json.loads(line)
                if migration.canonical(value) != line:
                    raise Error("Noncanonical source transfer row")
                yield value
            if not raw and len(decoded) < 1024 * 1024:
                break
    if (not decompressor.eof or buffered or compressed_bytes != payload["compressed_bytes"]
            or compressed_hash.hexdigest() != payload["compressed_sha256"]
            or decoded_bytes != payload["decoded_bytes"] or decoded_hash.hexdigest() != payload["decoded_sha256"]):
        raise Error("Incomplete or changed source transfer stream")


def read_packet(packet, get_chunk, expected_source=None):
    """Verify every byte before returning source rows for the Mac-side planner."""
    payload = verify_packet(packet, expected_source)
    source = payload["source"]
    snapshot = {"format": migration.FORMAT, "source_node": source["source_node"],
                "site": source["site"], "records": [], "event_delivery": {}}
    previous, table, seen = None, None, []
    for value in _lines(payload, get_chunk):
        if not isinstance(value, list) or not value:
            raise Error("Invalid source transfer row")
        if value[0] == "record":
            if len(value) != 4 or table is not None or not all(isinstance(v, str) and v for v in value[1:3]):
                raise Error("Invalid canonical source transfer record")
            pair = tuple(value[1:3])
            if previous is not None and pair <= previous:
                raise Error("Source records must be uniquely sorted")
            previous = pair
            snapshot["records"].append(value[1:])
        elif value[0] == "event_table":
            if len(value) != 2 or value[1] not in migration.EVENT_TABLES or value[1] in seen:
                raise Error("Invalid source event table observation")
            table = value[1]
            seen.append(table)
            snapshot["event_delivery"][table] = []
        elif value[0] == "event":
            if len(value) != 3 or table is None or value[1] != table:
                raise Error("Source delivery row does not follow its observed table")
            snapshot["event_delivery"][table].append(value[2])
        else:
            raise Error("Unknown source transfer row type")
    if seen != sorted(migration.EVENT_TABLES):
        raise Error("Every source delivery table must be observed in order")
    if migration.inventory(snapshot) != source:
        raise Error("Source canonical inventory differs from its signed manifest")
    return snapshot


def _directory(path, chunks_path):
    path, chunks_path = pathlib.Path(path), pathlib.Path(chunks_path)
    if path.resolve().parent != chunks_path.resolve().parent:
        raise Error("Source manifest and chunk directory must share a private parent")
    return chunks_path.name


def write_manifest(path, packet, chunks_path):
    name = _directory(path, chunks_path)
    migration.private_json(path, {"format": FORMAT, "packet": packet, "chunk_directory": name})


def load_manifest(path):
    path = pathlib.Path(path)
    wrapper = migration.load_json(path)
    name = wrapper.get("chunk_directory") if isinstance(wrapper, dict) else None
    if (not isinstance(wrapper, dict) or wrapper.get("format") != FORMAT or not isinstance(name, str) or not name
            or pathlib.Path(name).name != name or not (path.parent / name).is_dir()):
        raise Error("Source manifest needs its private sibling chunk directory")
    from biomerte.checkpoint_chunks import ChunkStore
    chunks = ChunkStore(path.parent / name)
    return read_packet(wrapper["packet"], chunks.get)


async def _remote_block(transport, service_id, worker, binding, key):
    expected = {field: binding.get(field) for field in BINDING_FIELDS}
    if (any(not isinstance(value, str) or not value for value in expected.values())
            or expected["service_id"] != service_id
            or set(worker) != {"worker_id", "worker_incarnation"}
            or any(not isinstance(value, str) or not value for value in worker.values())):
        raise Error("Exact live preparation and source helper identity required")
    response = await transport.request("/api/service-checkpoints/chunk-get", {
        "service_id": service_id, "worker": worker, "binding": expected, "chunk": {"digest": _sha(key)}})
    if (not isinstance(response, dict) or response.get("type") != "checkpoint_result"
            or response.get("status") != 200 or response.get("binding") != expected
            or response.get("worker_id") != worker["worker_id"]
            or response.get("worker_incarnation") != worker["worker_incarnation"]
            or response.get("boot_epoch") != expected["boot_epoch"]):
        raise Error("Stale or mismatched source helper reply")
    result = response.get("result")
    if not isinstance(result, dict) or result.get("digest") != key or type(result.get("bytes")) is not int:
        raise Error("Source helper returned another block")
    try:
        raw = base64.b64decode(result["data"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise Error("Invalid source block encoding") from exc
    _block(raw, {"digest": key, "bytes": result["bytes"]})
    return raw


async def pull_packet(manifest_digest, expected_source, chunks, transport, service_id, worker, binding):
    """Resumable private copy. Never install a checkpoint or change a grant."""
    manifest_raw = await _remote_block(transport, service_id, worker, binding, manifest_digest)
    packet = json.loads(manifest_raw)
    if migration.canonical(packet) != manifest_raw:
        raise Error("Noncanonical detached source packet")
    payload = verify_packet(packet, expected_source)
    fetched, reused = 0, 0
    for part in payload["chunks"]:
        if chunks.has(part["digest"]):
            raw = chunks.get(part["digest"])
            _block(raw, part)
            reused += 1
        else:
            raw = await _remote_block(transport, service_id, worker, binding, part["digest"])
            _block(raw, part)
            chunks.put(raw, expected_digest=part["digest"])
            fetched += 1
    # Verify structure, both stream digests, every original source row and every
    # observed event table before writing a successful transfer receipt.
    read_packet(packet, chunks.get, expected_source)
    summary = payload["source"]
    return packet, {"format": FORMAT, "source_node": expected_source,
                    "manifest_digest": manifest_digest, "source_digest": summary["digest"],
                    "event_delivery": summary["event_delivery"],
                    "records": summary["records"], "compressed_bytes": payload["compressed_bytes"],
                    "fetched_blocks": fetched, "reused_blocks": reused,
                    "runtime_checkpoint_installed": False, "effect_admission_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--home", required=True)
    pack.add_argument("--source", required=True)
    pack.add_argument("--chunks", required=True)
    pack.add_argument("--output", required=True)
    pull = commands.add_parser("pull")
    pull.add_argument("--manifest-digest", required=True)
    pull.add_argument("--source-node", required=True)
    pull.add_argument("--config", required=True)
    pull.add_argument("--worker", required=True)
    pull.add_argument("--binding", required=True)
    pull.add_argument("--chunks", required=True)
    pull.add_argument("--output", required=True)
    pull.add_argument("--receipt", required=True)
    args = parser.parse_args()
    _directory(args.output, args.chunks)
    from biomerte.checkpoint_chunks import ChunkStore
    chunks = ChunkStore(args.chunks)
    if args.command == "pack":
        packet = pack_backup(args.source, args.home, chunks)
        raw = migration.canonical(packet)
        descriptor = _block(raw)
        chunks.put(raw, expected_digest=descriptor["digest"])
        write_manifest(args.output, packet, args.chunks)
        print(json.dumps({"source_node": packet["payload"]["source"]["source_node"],
            "source_digest": packet["payload"]["source"]["digest"],
            "manifest_chunk": descriptor, "blocks": len(packet["payload"]["chunks"]),
            "compressed_bytes": packet["payload"]["compressed_bytes"]}, sort_keys=True))
    else:
        import aiohttp
        from biomerte.service_daemon import Transport
        config = migration.load_json(args.config)
        async def run():
            async with aiohttp.ClientSession() as session:
                return await pull_packet(args.manifest_digest, args.source_node, chunks,
                    Transport(session, config), config["service_id"],
                    migration.load_json(args.worker), migration.load_json(args.binding))
        packet, receipt = asyncio.run(run())
        write_manifest(args.output, packet, args.chunks)
        migration.private_json(args.receipt, receipt)
        print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
