"""Vision fanout keeps the bootstrap and the latest observation for each source."""
import asyncio
import json
from collections import OrderedDict

from .stream_router import decode, frame


class VisionMailbox:
    def __init__(self, allow_legacy=False):
        self.allow_legacy = allow_legacy
        self.versioned = False
        self.controls = {}
        self.seen = set()
        self.latest = OrderedDict()
        self.ready = asyncio.Event()
        self.sequence = -1

    def put_nowait(self, message):
        meta, _ = decode(message)
        kind = meta.get("type", "frame")
        if kind == "sensor_manifest" or str(meta.get('schema', '')).startswith('rover.'):
            self.versioned = True
            self.allow_legacy = False
        elif (kind in ('frame', 'sensor_tick') and not self.versioned and 'schema_version' not in meta):
            self.allow_legacy = True
        if kind in ("sensor_manifest", "world_snapshot"):
            self.controls[kind] = message
        else:
            self.latest[(meta.get("camera_id"), kind)] = message
        self.ready.set()

    def _next(self):
        order = ("sensor_manifest", "world_snapshot")
        if self.seen == {"sensor_manifest"} and not self.allow_legacy:
            order = ("world_snapshot",)
        for kind in order:
            if kind in self.controls:
                self.seen.add(kind)
                return self.controls.pop(kind)
            if kind not in self.seen and not self.allow_legacy:
                return None
        if self.latest:
            return self.latest.popitem(last=False)[1]

    async def get(self):
        while True:
            value = self._next()
            if value is not None:
                meta, data = decode(value)
                # Map and vision upstream have independent sequence spaces. Each
                # attachment presents a single monotonic vision sequence to its reader.
                if self.allow_legacy and "sequence" not in meta:
                    return value
                if meta["sequence"] <= self.sequence:
                    meta.setdefault("source_sequence", meta["sequence"])
                    meta["sequence"] = self.sequence + 1
                    value = json.dumps(meta) if data is None else frame(meta, data)
                self.sequence = meta["sequence"]
                return value
            self.ready.clear()
            await self.ready.wait()

    def discard_pending(self):
        self.controls.clear()
        self.latest.clear()
        self.ready.clear()

    def full(self):
        return False
