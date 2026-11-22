"""Publish a controller root with only newly changed, bounded block uploads."""
import asyncio
import base64
import hashlib

from .checkpoint_chunks import (BLOCK_BYTES, STATE_FORMAT, UPLOAD_BATCH_BYTES, UPLOAD_BATCH_BLOCKS,
                                describe_shard, state_blocks)
from .service_checkpoint import CheckpointError

CHECKPOINT_UPLOAD_CONCURRENCY = 4


async def read_shard(daemon, shard):
    raw = await asyncio.to_thread(daemon.authority.snapshot_chunk, shard["digest"])
    if (not isinstance(raw, bytes) or len(raw) != shard["bytes"] or
            hashlib.sha256(raw).hexdigest() != shard["digest"]):
        raise CheckpointError("Controller shard changed during publication")
    return raw


def encoded_block(block, raw):
    return {"digest": block["digest"], "data": base64.b64encode(raw).decode("ascii")}


def check_receipt(block, reply):
    if (not isinstance(reply, dict) or reply.get("digest") != block["digest"] or reply.get("bytes") != block["bytes"]
            or reply.get("durable") is not True):
        raise CheckpointError("Helper did not durably acknowledge exact block")


async def upload_block(daemon, block, raw):
    reply = await daemon.helper("chunk_put", block=encoded_block(block, raw))
    check_receipt(block, reply)


async def upload_batch(daemon, batch):
    if getattr(daemon, "checkpoint_bulk_upload", False):
        reply = await daemon.helper("chunk_put_many", blocks=[encoded_block(b, raw) for b, raw in batch])
        receipts = reply.get("receipts") if isinstance(reply, dict) else None
        if not isinstance(receipts, list) or len(receipts) != len(batch):
            raise CheckpointError("Incomplete checkpoint block batch acknowledgement")
        for (block, _), receipt in zip(batch, receipts):
            check_receipt(block, receipt)
        return
    # Content-addressed blocks are independent. Finish all started writes
    # before retrying an uncertain root and never publish on a partial ACK.
    results = await asyncio.gather(*(upload_block(daemon, block, raw) for block, raw in batch),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def publish_controller(daemon):
    try:
        state = await asyncio.to_thread(daemon.authority.snapshot)
        if state.get("format") != STATE_FORMAT:
            raise CheckpointError("Controller authority did not return a shard root")
        cache = getattr(daemon, "_controller_shard_blocks", {})
        current, shards, unknown = {}, [], {}
        # Describe one shard at a time without retaining its body across RPCs.
        # A cold child must batch across the root, not validate the same grant
        # remotely once per shard when the helper already has every block.
        for shard in state["shards"]:
            key = shard["digest"]
            blocks = cache.get(key)
            if blocks is None:
                blocks = current.get(key)
                if blocks is None:
                    blocks = shard.get("blocks")
                    if blocks is None:
                        raw = await read_shard(daemon, shard)
                        blocks = describe_shard(shard["kind"], shard["prefix"], raw)["blocks"]
                        del raw
                for block in blocks:
                    unknown.setdefault(block["digest"], block["bytes"])
            current[key] = blocks
            shards.append(dict(shard, blocks=blocks))
        manifest = dict(state, shards=shards)
        state_blocks(manifest)

        keys, missing = list(unknown), set()
        for offset in range(0, len(keys), 256):
            asked = keys[offset:offset + 256]
            reply = await daemon.helper("chunk_missing", digests=asked)
            values = reply.get("missing")
            if (not isinstance(values, list) or
                    any(not isinstance(value, str) for value in values) or
                    len(values) != len(set(values)) or
                    not set(values).issubset(asked)):
                raise CheckpointError("Invalid helper missing-block response")
            missing.update(values)

        # Re-read only shards needed for an upload, and recheck their exact
        # identity before sending any bytes. Shared blocks upload only once.
        batch = []
        batch_bytes = 0
        bulk = getattr(daemon, "checkpoint_bulk_upload", False)
        batch_limit = UPLOAD_BATCH_BLOCKS if bulk else CHECKPOINT_UPLOAD_CONCURRENCY
        for shard in shards:
            if not any(block["digest"] in missing for block in shard["blocks"]):
                continue
            raw = await read_shard(daemon, shard)
            for index, block in enumerate(shard["blocks"]):
                if block["digest"] not in missing:
                    continue
                block_bytes = block["bytes"]
                if batch and (len(batch) == batch_limit or batch_bytes + block_bytes > UPLOAD_BATCH_BYTES):
                    await upload_batch(daemon, batch)
                    batch.clear()
                    batch_bytes = 0
                batch.append((block, raw[index * BLOCK_BYTES:(index + 1) * BLOCK_BYTES]))
                batch_bytes += block_bytes
                missing.remove(block["digest"])
            del raw
        if missing:
            raise CheckpointError("Controller root still has unacknowledged blocks")
        if batch:
            await upload_batch(daemon, batch)
        result = await daemon.helper("publish", state=manifest)
        daemon._controller_shard_blocks = current
        return result
    except BaseException:
        # A lost response or helper restart cannot be concealed by this local
        # optimization. Rebuild descriptors and reconcile missing blocks next time.
        daemon._controller_shard_blocks = {}
        raise
