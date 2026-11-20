"""Bounded immutable blocks for controller checkpoints.

Only the host helpers retain block bytes. A signed root names every shard and
block; an upload receipt alone never makes a root current or grants authority.
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time

BLOCK_BYTES = 256 * 1024
STATE_FORMAT = "biomerte-controller-state/1"
DIGEST = re.compile(r"[0-9a-f]{64}")
PREFIX = re.compile(r"[0-9a-f]{2}")


class ChunkError(ValueError):
    pass


def valid_digest(value):
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ChunkError("Exact SHA256 block digest required")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def describe_shard(kind, prefix, raw):
    if not isinstance(raw, bytes) or not raw:
        raise ChunkError("Nonempty canonical shard bytes required")
    return {"kind": kind, "prefix": prefix,
            "digest": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "blocks": [{"digest": hashlib.sha256(raw[pos:pos + BLOCK_BYTES]).hexdigest(),
                        "bytes": min(BLOCK_BYTES, len(raw) - pos)}
                       for pos in range(0, len(raw), BLOCK_BYTES)]}


def state_blocks(state):
    """Validate the complete signed descriptor, returning digest -> exact size."""
    if not isinstance(state, dict) or state.get("format") != STATE_FORMAT:
        return None
    if (not isinstance(state.get("authority"), str) or not state["authority"] or
            not isinstance(state.get("site"), str) or not state["site"] or
            not isinstance(state.get("shards"), list)):
        raise ChunkError("Complete controller state identity and shards required")
    refs, seen = {}, set()
    for shard in state["shards"]:
        if not isinstance(shard, dict):
            raise ChunkError("Controller shard descriptor required")
        kind, prefix = shard.get("kind"), shard.get("prefix")
        if (not isinstance(kind, str) or not kind or
                not isinstance(prefix, str) or PREFIX.fullmatch(prefix) is None or
                (kind, prefix) in seen):
            raise ChunkError("Distinct kind and hashed ID prefix required")
        seen.add((kind, prefix))
        valid_digest(shard.get("digest"))
        if (type(shard.get("bytes")) is not int or shard["bytes"] < 2 or
                not isinstance(shard.get("blocks"), list) or not shard["blocks"]):
            raise ChunkError("Exact shard byte count and block list required")
        total = 0
        for index, block in enumerate(shard["blocks"]):
            if not isinstance(block, dict):
                raise ChunkError("Block descriptor required")
            key = valid_digest(block.get("digest"))
            size = block.get("bytes")
            if (type(size) is not int or not 0 < size <= BLOCK_BYTES or
                    (index < len(shard["blocks"]) - 1 and size != BLOCK_BYTES)):
                raise ChunkError("Bounded canonical block sizes required")
            if key in refs and refs[key] != size:
                raise ChunkError("Conflicting block sizes")
            refs[key] = size
            total += size
        if total != shard["bytes"]:
            raise ChunkError("Shard block lengths do not match root")
    return refs


class ChunkStore:
    """Immutable content-addressed files, fsynced before acknowledgement.

    Cache only verified file identities, never payload bodies. A modified file
    is rehashed; after a restart each referenced file is verified once.
    """
    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.lock = threading.RLock()
        self.verified = {}

    def _path(self, key):
        return os.path.join(self.directory, valid_digest(key))

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _identity(st):
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def has(self, key, size=None):
        with self.lock:
            path = self._path(key)
            try:
                st = os.stat(path, follow_symlinks=False)
                if not __import__("stat").S_ISREG(st.st_mode):
                    return False
                if not 0 < st.st_size <= BLOCK_BYTES or (size is not None and st.st_size != size):
                    return False
                identity = self._identity(st)
                if self.verified.get(key) == identity:
                    return True
                with open(path, "rb") as source:
                    raw = source.read(BLOCK_BYTES + 1)
                if len(raw) != st.st_size or hashlib.sha256(raw).hexdigest() != key:
                    self.verified.pop(key, None)
                    return False
                # A concurrent external rewrite is not a verification receipt.
                if self._identity(os.stat(path, follow_symlinks=False)) != identity:
                    return False
                self.verified[key] = identity
                return True
            except FileNotFoundError:
                self.verified.pop(key, None)
                return False

    def put(self, raw, expected_digest=None):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= BLOCK_BYTES:
            raise ChunkError("Block must contain 1..262144 bytes")
        key = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None and key != valid_digest(expected_digest):
            raise ChunkError("Block digest mismatch")
        with self.lock:
            if not self.has(key, len(raw)):
                fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, self._path(key))
                    self._sync_directory()
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            # Duplicate PUTs also certify the actual durable stored bytes.
            if not self.has(key, len(raw)):
                raise ChunkError("Stored block did not verify")
        return {"digest": key, "bytes": len(raw), "durable": True}

    def get(self, key):
        with self.lock:
            if not self.has(key):
                raise ChunkError("Referenced controller block unavailable")
            with open(self._path(key), "rb") as source:
                raw = source.read(BLOCK_BYTES + 1)
            if len(raw) > BLOCK_BYTES or hashlib.sha256(raw).hexdigest() != key:
                self.verified.pop(key, None)
                raise ChunkError("Controller block corruption")
            return raw

    def missing(self, digests):
        if not isinstance(digests, (list, tuple, dict)):
            raise ChunkError("Explicit block digest list required")
        if isinstance(digests, dict):
            return [key for key, size in digests.items() if not self.has(key, size)]
        return [key for key in dict.fromkeys(digests) if not self.has(key)]

    def require(self, state):
        refs = state_blocks(state)
        if refs is not None and self.missing(refs):
            raise ChunkError("Controller root has missing or corrupt blocks")
        return refs

    def write_shard(self, kind, prefix, raw):
        descriptor = describe_shard(kind, prefix, raw)
        for index, block in enumerate(descriptor["blocks"]):
            self.put(raw[index * BLOCK_BYTES:(index + 1) * BLOCK_BYTES], block["digest"])
        return descriptor

    def prune(self, keep, grace_s=600):
        """Published roots protect their blocks; recent partial uploads get grace."""
        keep = set(keep)
        with self.lock:
            removed, cutoff = 0, time.time() - grace_s
            for name in os.listdir(self.directory):
                if name in keep or DIGEST.fullmatch(name) is None:
                    continue
                path = self._path(name)
                try:
                    if os.stat(path, follow_symlinks=False).st_mtime >= cutoff:
                        continue
                    os.unlink(path)
                    self.verified.pop(name, None)
                    removed += 1
                except FileNotFoundError:
                    pass
            if removed:
                self._sync_directory()
            return removed


def iter_records(state, read_chunk):
    """Verify one shard at a time; never materialize a complete controller state."""
    if state_blocks(state) is None:
        raise ChunkError("Controller shard root required")
    for shard in state["shards"]:
        raw = bytearray()
        for block in shard["blocks"]:
            value = read_chunk(block["digest"])
            if (not isinstance(value, bytes) or len(value) != block["bytes"] or
                    hashlib.sha256(value).hexdigest() != block["digest"]):
                raise ChunkError("Checkpoint block does not match signed root")
            raw.extend(value)
        if len(raw) != shard["bytes"] or hashlib.sha256(raw).hexdigest() != shard["digest"]:
            raise ChunkError("Checkpoint shard digest mismatch")
        try:
            rows = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ChunkError("Malformed canonical controller shard")
        if not isinstance(rows, list) or canonical(rows) != raw:
            raise ChunkError("Canonical controller record list required")
        del raw
        previous = None
        for row in rows:
            if (not isinstance(row, list) or len(row) != 3 or row[0] != shard["kind"] or
                    not isinstance(row[1], str) or not row[1] or
                    hashlib.sha256(row[1].encode("utf-8")).hexdigest()[:2] != shard["prefix"] or
                    (previous is not None and row[1] <= previous)):
                raise ChunkError("Record identity, shard custody or ordering mismatch")
            previous = row[1]
            yield row
