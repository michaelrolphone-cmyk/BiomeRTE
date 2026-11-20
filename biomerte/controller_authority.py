"""One fenced logical owner for the complete canonical site controller state.

Physical provider generations and assistant ownership epochs belong to their
existing protocols. Moving this database never invents physical quiescence or
replays an uncertain command. The daemon validates its current grant before
dispatch and again after durably publishing the resulting state.

Only changed hash buckets are read for checkpoints. The local bucket index and
dirty set are disposable projections, not a shared or historical journal.
"""
import hashlib
import json
import threading

from . import session_api
from .store import Fault, digest
from .service_checkpoint import binding, canonical

FORMAT = "biomerte-controller-state/1"


def bucket(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:2]


class ControllerAuthority:
    def __init__(self, app, config, current_binding):
        self.app, self.config, self.binding = app, config, binding(current_binding)
        self.lock = threading.RLock()
        if config.get("authority_kind") != "controller" or not app.controller_runtime:
            raise Fault("Explicit managed controller runtime required", 403)
        if app.stores["site"].remote:
            raise Fault("Controller authority requires local durable SQLite")
        if app.node != config["authority_id"] or app.site != config["site"]:
            raise Fault("Controller private identity differs from logical authority", 403)
        if self.binding["service_id"] != config["service_id"]:
            raise Fault("Wrong controller service grant binding", 403)
        self.shards = {}
        self._by_digest = {}
        self.snapshot_reads = 0
        self._initialize_checkpoint_index()
        from .assistant_support import seed_controller_control
        self.app.stores["site"].execute(lambda tx: seed_controller_control(tx, self.app))

    def _initialize_checkpoint_index(self):
        # Triggers capture direct canonical SQL changes as well as Records.put,
        # including deletions and changes committed before a process crashes.
        with self.app.stores["site"].connection() as db:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS controller_checkpoint_rows(
                    kind TEXT,id TEXT,prefix TEXT,PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS controller_checkpoint_prefix
                    ON controller_checkpoint_rows(kind,prefix,id);
                CREATE TABLE IF NOT EXISTS controller_checkpoint_dirty(
                    kind TEXT,id TEXT,PRIMARY KEY(kind,id));
                CREATE TRIGGER IF NOT EXISTS controller_checkpoint_insert
                    AFTER INSERT ON records BEGIN
                    INSERT OR REPLACE INTO controller_checkpoint_dirty VALUES(NEW.kind,NEW.id);
                    END;
                CREATE TRIGGER IF NOT EXISTS controller_checkpoint_update_old
                    AFTER UPDATE ON records BEGIN
                    INSERT OR REPLACE INTO controller_checkpoint_dirty VALUES(OLD.kind,OLD.id);
                    INSERT OR REPLACE INTO controller_checkpoint_dirty VALUES(NEW.kind,NEW.id);
                    END;
                CREATE TRIGGER IF NOT EXISTS controller_checkpoint_delete
                    AFTER DELETE ON records BEGIN
                    INSERT OR REPLACE INTO controller_checkpoint_dirty VALUES(OLD.kind,OLD.id);
                    END;
                DELETE FROM controller_checkpoint_rows;
                INSERT OR REPLACE INTO controller_checkpoint_dirty SELECT kind,id FROM records;
                COMMIT;
            """)

    def _read_shard(self, tx, kind, prefix):
        rows = [[k, key, json.loads(raw)] for k, key, raw in tx.db.execute(
            "SELECT r.kind,r.id,r.value FROM controller_checkpoint_rows i "
            "JOIN records r ON r.kind=i.kind AND r.id=i.id "
            "WHERE i.kind=? AND i.prefix=? ORDER BY r.id", (kind, prefix))]
        self.snapshot_reads += 1
        return canonical(rows), len(rows)

    def snapshot(self):
        with self.lock:
            updates = {}
            def read(tx):
                dirty = list(tx.db.execute(
                    "SELECT kind,id FROM controller_checkpoint_dirty ORDER BY kind,id"))
                affected = set()
                for kind, key in dirty:
                    prefix = bucket(key)
                    affected.add((kind, prefix))
                    tx.db.execute("DELETE FROM controller_checkpoint_rows WHERE kind=? AND id=?",
                                  (kind, key))
                    tx.db.execute("INSERT INTO controller_checkpoint_rows "
                                  "SELECT kind,id,? FROM records WHERE kind=? AND id=?",
                                  (prefix, kind, key))
                for kind, prefix in sorted(affected):
                    raw, count = self._read_shard(tx, kind, prefix)
                    updates[(kind, prefix)] = (dict(kind=kind, prefix=prefix,
                        digest=hashlib.sha256(raw).hexdigest(), bytes=len(raw)) if count else None)
                tx.db.execute("DELETE FROM controller_checkpoint_dirty")
            self.app.stores["site"].execute(read)
            for key, entry in updates.items():
                if entry is None:
                    self.shards.pop(key, None)
                else:
                    self.shards[key] = entry
            self._by_digest = {entry["digest"]: key for key, entry in self.shards.items()}
            return dict(format=FORMAT, authority=self.app.node, site=self.app.site,
                        shards=[dict(self.shards[key]) for key in sorted(self.shards)])

    def snapshot_chunk(self, shard_digest):
        """Return one exact current shard while the daemon's dispatch lock is held."""
        with self.lock:
            key = self._by_digest.get(shard_digest)
            if key is None:
                raise Fault("Checkpoint shard is not in the current root", 409)
            raw, unused = self.app.stores["site"].execute(
                lambda tx: self._read_shard(tx, *key), read_only=True)
            if hashlib.sha256(raw).hexdigest() != shard_digest:
                raise Fault("Checkpoint shard changed before publication", 409)
            return raw

    def maintain(self):
        with self.lock:
            return self.app.tick("site")

    def _check(self, envelope, preparing):
        if preparing:
            raise Fault("Controller effect admission is unavailable during preparation", 503)
        if not isinstance(envelope, dict):
            raise Fault("Signed envelope required", 403)
        auth, args = envelope.get("auth"), envelope.get("arguments")
        request = envelope.get("id")
        if (envelope.get("site") != self.app.site or envelope.get("realm") != "site"
                or not isinstance(args, dict) or not isinstance(auth, dict)
                or not isinstance(auth.get("actor"), str)
                or not isinstance(auth.get("signature"), str)
                or not (isinstance(request, str) and request or isinstance(request, dict))):
            raise Fault("Exact signed site envelope required", 403)
        if set(envelope) - {"site","realm","operation","arguments","id","auth","controller"}:
            raise Fault("Unexpected signed envelope fields", 403)
        if envelope.get("controller") not in (None, self.app.node) and \
                envelope.get("controller") not in self.app.controller_aliases:
            raise Fault("Request addressed to another controller authority", 403)
        if envelope.get("operation") in session_api.EXTERNAL:
            raise Fault("External ownership sessions cannot move a managed controller domain", 409)
        return envelope["operation"], args, auth

    def call(self, envelope, preparing):
        with self.lock:
            name, args, auth = self._check(envelope, preparing)
            return self.app.call(name, args, envelope["id"], "site", auth=auth,
                                 controller=envelope.get("controller"))

    def pair(self, operation, payload, preparing=False):
        if preparing:
            raise Fault("Controller pairing is unavailable during preparation", 503)
        if not isinstance(payload, dict):
            raise Fault("Exact pairing payload required")
        with self.lock:
            if operation == "begin" and set(payload) == {"public_key","label"}:
                if not all(isinstance(payload[k], str) for k in payload):
                    raise Fault("Pairing public key and label must be strings")
                return self.app.begin_pair(payload["public_key"],payload["label"])
            if operation == "finish" and set(payload) == {"proof"}:
                proof = payload["proof"]
                if (not isinstance(proof, dict) or set(proof) != {"key","signature"}
                        or not all(isinstance(proof[k], str) for k in proof)):
                    raise Fault("Exact signed pairing proof required")
                return self.app.finish_pair(proof)
            raise Fault("Only pairing begin and finish are public operations", 403)

    def inspect(self, session):
        with self.lock:
            return self.app.call("stream.inspect", {"session": session},
                                 "controller-inspect-" + session, "site")

    def prepare_fences(self, prepared_checkpoint, retained_floors=None):
        # This is logical write ownership, not a physical resource-generation
        # fence. Existing action/lease/stream and assistant epoch rows are intact.
        def prepare(tx):
            old = tx.get("_controller_authority", "current")
            if old and old["binding"] == self.binding:
                if old["checkpoint"] != prepared_checkpoint:
                    raise Fault("Controller prepared checkpoint changed", 409)
                return old
            return tx.put("_controller_authority", "current",
                dict(binding=self.binding, checkpoint=prepared_checkpoint, phase="prepared"))
        with self.lock:
            return self.app.stores["site"].execute(prepare)

    def finish_fences(self):
        def finish(tx):
            row = tx.need("_controller_authority", "current")
            if row["binding"] != self.binding:
                raise Fault("Controller preparation belongs to another service grant", 409)
            evidence = dict(fenced=True, fence_kind="logical-controller",
                authority=self.app.node, resources={}, binding=self.binding,
                checkpoint=row["checkpoint"], boot_epoch=self.binding["boot_epoch"],
                grant_token=self.binding["grant_token"])
            if row["phase"] != "fenced":
                row.update(phase="fenced", evidence=evidence)
                tx.put("_controller_authority", "current", row)
            return evidence
        with self.lock:
            return self.app.stores["site"].execute(finish)
