"""Publish a controller root with only newly changed, bounded block uploads."""
import asyncio
import base64
import hashlib

from .checkpoint_chunks import BLOCK_BYTES, STATE_FORMAT, describe_shard, state_blocks
from .service_checkpoint import CheckpointError


async def publish_controller(daemon):
    state = await asyncio.to_thread(daemon.authority.snapshot)
    if state.get("format") != STATE_FORMAT:
        raise CheckpointError("Controller authority did not return a shard root")
    cache = getattr(daemon, "_controller_shard_blocks", {})
    current, shards = {}, []
    try:
        for shard in state["shards"]:
            key = shard["digest"]
            blocks = cache.get(key)
            if blocks is None:
                raw = await asyncio.to_thread(daemon.authority.snapshot_chunk, key)
                if (not isinstance(raw, bytes) or len(raw) != shard["bytes"] or
                        hashlib.sha256(raw).hexdigest() != key):
                    raise CheckpointError("Controller shard changed during publication")
                described = describe_shard(shard["kind"], shard["prefix"], raw)
                blocks = described["blocks"]
                keys = list(dict.fromkeys(block["digest"] for block in blocks))
                missing = set()
                for offset in range(0, len(keys), 256):
                    asked = keys[offset:offset + 256]
                    reply = await daemon.helper("chunk_missing", digests=asked)
                    values = reply.get("missing")
                    if (not isinstance(values, list) or len(values) != len(set(values)) or
                            not set(values).issubset(asked)):
                        raise CheckpointError("Invalid helper missing-block response")
                    missing.update(values)
                for index, block in enumerate(blocks):
                    if block["digest"] not in missing:
                        continue
                    value = raw[index * BLOCK_BYTES:(index + 1) * BLOCK_BYTES]
                    reply = await daemon.helper("chunk_put", block={
                        "digest": block["digest"], "data": base64.b64encode(value).decode("ascii")})
                    if (reply.get("digest") != block["digest"] or
                            reply.get("bytes") != block["bytes"] or reply.get("durable") is not True):
                        raise CheckpointError("Helper did not durably acknowledge exact block")
                    missing.discard(block["digest"])
                del raw
            current[key] = blocks
            shards.append(dict(shard, blocks=blocks))
        manifest = dict(state, shards=shards)
        state_blocks(manifest)
        result = await daemon.helper("publish", state=manifest)
        daemon._controller_shard_blocks = current
        return result
    except BaseException:
        # A lost response or helper restart cannot be concealed by this local
        # optimization. Rebuild descriptors and reconcile missing blocks next time.
        daemon._controller_shard_blocks = {}
        raise
