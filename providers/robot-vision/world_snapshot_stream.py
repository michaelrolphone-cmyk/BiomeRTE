"""Assemble the console's revision-pinned atlas pages for a map subscriber."""
import logging
from pathlib import Path
import threading

from leased_publish import packet


def snapshot_message(read, limit=500):
    data = read("robot.world.read", {"limit": limit})
    key = (data["map"]["map_id"], data["map"]["revision"])
    seen = set()
    cursor = data["next_cursor"]
    while cursor:
        if cursor in seen:
            raise ValueError("repeated world snapshot cursor")
        seen.add(cursor)
        page = read("robot.world.read", {"limit": limit, "cursor": cursor})
        if (page["map"]["map_id"], page["map"]["revision"]) != key:
            raise ValueError("world changed within pinned snapshot")
        data["entities"].extend(page["entities"])
        cursor = page["next_cursor"]
    data["next_cursor"] = None
    return {"type":"world_snapshot", "complete":True, "map_id":key[0], "revision":key[1], "data":data}


class WorldSnapshotFeed:
    def __init__(self, service):
        self.service = service
        self.stop = threading.Event()
        self.thread = None
        self.key = None
        self.local_key = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="world-snapshot-stream", daemon=True)
        self.thread.start()

    def version(self):
        atlas = self.service.atlas
        with atlas.lock:
            map_id, revision = atlas.db.execute("SELECT id,revision FROM world").fetchone()
        try:
            stat = (Path(self.service.state_dir) / "survey-world.json").stat()
            survey = stat.st_ino, stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            survey = None
        try:
            stat = (Path(self.service.state_dir) / "attitude-world-reference.json").stat()
            attitude = stat.st_ino, stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            attitude = None
        return map_id, revision, (survey, attitude)

    def refresh(self):
        local_key = (self.service.map_id, self.service.revision)
        if local_key != self.local_key:
            from rover_wire import map_snapshot
            local = map_snapshot(self.service.query, *local_key)
            self.service.live_maps.publish_local(packet(local))
            self.local_key = local_key
        key = self.version()
        if key == self.key:
            return
        with self.service.atlas.snapshot_reads():
            message = snapshot_message(self.service.query)
        if (message["map_id"], message["revision"]) != key[:2]:
            return
        # A survey edit need not advance the visual atlas clock.
        if self.version()[2] != key[2]:
            return
        self.service.live_maps.publish_world(packet(message))
        self.key = key

    def run(self):
        while not self.stop.is_set():
            try:
                if getattr(self.service, "inventory", None) is not None:
                    manifest = self.service.inventory.snapshot(self.service)
                    self.service.live_frames.publish_manifest(manifest)
                    self.service.rear_live_frames.publish_manifest(manifest)
                self.refresh()
            except Exception:
                logging.getLogger(__name__).exception("World snapshot refresh deferred")
            self.stop.wait(.5)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
