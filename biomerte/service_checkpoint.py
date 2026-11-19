"""Durable scoped checkpoints and grant-bound service preparation.

Only the two hosts store payloads. The external witness retains transient
high-water metadata. A missing witness head requires all configured replicas;
a cached snapshot alone is never a freshness proof.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading

PROTOCOL = "biomerte-service-checkpoint/1"
ADAPTER_PROTOCOL = "biomerte-service-adapter/1"
BINDING_FIELDS = ("service_id", "manifest_revision", "worker_id",
                  "worker_incarnation", "boot_epoch", "grant_token")


class CheckpointError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def binding(value):
    if not isinstance(value, dict) or any(
            not isinstance(value.get(k), str) or not value[k]
            for k in BINDING_FIELDS):
        raise CheckpointError("Complete service grant binding required")
    return {k: value[k] for k in BINDING_FIELDS}


def checkpoint_ref(packet):
    body = packet["payload"]
    return {"service_id": body["service_id"], "sequence": body["sequence"],
            "digest": packet["digest"]}


def same_ref(left, right):
    return isinstance(left, dict) and isinstance(right, dict) and all(
        left.get(k) == right.get(k) for k in ("service_id", "sequence", "digest"))


def select_current(service_id, witness, heads, replicas, boot_epoch):
    """Select evidence, never infer freshness from a timestamp or availability.

    heads maps stable worker IDs to authenticated durable checkpoint references.
    An existing witnessed head is decisive. Without it every configured replica
    must be present, and the maximum signed sequence must be unambiguous.
    """
    if not replicas or len(set(replicas)) != len(replicas):
        raise CheckpointError("Explicit distinct replica identities required")
    def valid_ref(value):
        return (isinstance(value, dict) and value.get("service_id") == service_id
                and type(value.get("sequence")) is int and value["sequence"] >= 0
                and isinstance(value.get("digest"), str)
                and len(value["digest"]) == 64
                and all(c in "0123456789abcdef" for c in value["digest"]))
    if witness is not None:
        if witness.get("boot_epoch") != boot_epoch or not valid_ref(witness):
            raise CheckpointError("Invalid current witness head")
        matches = [host for host in replicas
                   if valid_ref(heads.get(host)) and same_ref(heads[host], witness)]
        if not matches:
            raise CheckpointError("Current checkpoint is unavailable")
        return {"head": {k: witness[k] for k in ("service_id", "sequence", "digest")},
                "source": matches[0], "reestablish": False}
    if any(not valid_ref(heads.get(host)) for host in replicas):
        raise CheckpointError("Witness restart requires every configured replica")
    maximum = max(heads[host]["sequence"] for host in replicas)
    latest = [(host, heads[host]) for host in replicas
              if heads[host]["sequence"] == maximum]
    if len({head["digest"] for _, head in latest}) != 1:
        raise CheckpointError("Conflicting durable checkpoint lineage")
    return {"head": {k: latest[0][1][k]
                     for k in ("service_id", "sequence", "digest")},
            "source": latest[0][0], "reestablish": True}


class CheckpointStore:
    """Local SQLite is the durable authority for packets and adapter receipts."""

    def __init__(self, path, service_id, scope_digest, key):
        if not isinstance(key, bytes) or len(key) < 32:
            raise CheckpointError("Private checkpoint key must contain at least32 bytes")
        self.path, self.service_id = path, service_id
        self.scope_digest, self.key = scope_digest, key
        self.lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS packets (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                sequence INTEGER NOT NULL, digest TEXT NOT NULL, packet TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lineage (
                sequence INTEGER NOT NULL, digest TEXT PRIMARY KEY,
                previous_digest TEXT NOT NULL, binding TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS floors (resource TEXT PRIMARY KEY,generation INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS archive (
                digest TEXT PRIMARY KEY, sequence INTEGER NOT NULL, packet TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS adapter (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, reply TEXT NOT NULL);
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def _sign(self, payload):
        raw = canonical(payload)
        return {"payload": payload, "digest": hashlib.sha256(raw).hexdigest(),
                "signature": hmac.new(self.key, raw, hashlib.sha256).hexdigest()}

    def verify(self, packet):
        if not isinstance(packet, dict) or not isinstance(packet.get("payload"), dict):
            raise CheckpointError("Invalid checkpoint packet")
        payload = packet["payload"]
        raw = canonical(payload)
        if (payload.get("protocol") != PROTOCOL
                or payload.get("service_id") != self.service_id
                or payload.get("scope_digest") != self.scope_digest
                or type(payload.get("sequence")) is not int
                or payload["sequence"] < 0
                or packet.get("digest") != hashlib.sha256(raw).hexdigest()
                or not hmac.compare_digest(
                    str(packet.get("signature", "")),
                    hmac.new(self.key, raw, hashlib.sha256).hexdigest())):
            raise CheckpointError("Checkpoint scope, digest or signature mismatch")
        if payload.get("binding") is not None:
            if binding(payload["binding"])["service_id"] != self.service_id:
                raise CheckpointError("Checkpoint belongs to another service")
        if not isinstance(payload.get("state"), dict):
            raise CheckpointError("Scoped checkpoint state required")
        return payload

    def packet(self):
        with self.lock:
            row = self.db.execute("SELECT packet FROM packets WHERE singleton=1").fetchone()
            return json.loads(row[0]) if row else None

    def head(self):
        packet = self.packet()
        return checkpoint_ref(packet) if packet else None

    def install(self, packet):
        """Commit before acknowledging. Same packet is idempotent; rollback is refused."""
        payload = self.verify(packet)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute("SELECT sequence,digest FROM packets WHERE singleton=1").fetchone()
                if old:
                    if payload["sequence"] < old[0]:
                        raise CheckpointError("Checkpoint rollback rejected")
                    if payload["sequence"] == old[0]:
                        if packet["digest"] != old[1]:
                            raise CheckpointError("Conflicting checkpoint at retained sequence")
                        self.db.execute("COMMIT")
                        return checkpoint_ref(packet)
                known = self.db.execute("SELECT digest FROM lineage WHERE sequence=?",
                                        (payload["sequence"],)).fetchone()
                if known and known[0] != packet["digest"]:
                    raise CheckpointError("Checkpoint conflicts with retained lineage")
                self.db.execute("INSERT OR IGNORE INTO archive VALUES(?,?,?)",
                                (packet["digest"],payload["sequence"],canonical(packet).decode()))
                self.db.execute("INSERT OR REPLACE INTO packets VALUES(1,?,?,?)",
                                (payload["sequence"], packet["digest"], canonical(packet).decode()))
                self.db.execute("INSERT OR IGNORE INTO lineage VALUES(?,?,?,?)",
                                (payload["sequence"], packet["digest"],
                                 payload.get("previous_digest", ""),
                                 canonical(payload.get("binding")).decode()))
                self._observe_floors(packet)
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        return checkpoint_ref(packet)

    def retained(self):
        with self.lock:
            return [{"service_id":self.service_id,"sequence":row[0],"digest":row[1]}
                    for row in self.db.execute(
                        "SELECT sequence,digest FROM archive ORDER BY sequence DESC,digest")]

    def get(self, reference):
        if reference.get("service_id") != self.service_id:
            raise CheckpointError("Wrong checkpoint service")
        with self.lock:
            row=self.db.execute("SELECT packet FROM archive WHERE digest=? AND sequence=?",
                                (reference.get("digest"),reference.get("sequence"))).fetchone()
            if not row:
                raise CheckpointError("Requested retained checkpoint is unavailable")
            return json.loads(row[0])

    def retain(self, packet):
        """A replica ack certifies durable bytes, never publication or freshness."""
        payload=self.verify(packet)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT OR IGNORE INTO archive VALUES(?,?,?)",
                    (packet["digest"],payload["sequence"],canonical(packet).decode()))
                self.db.execute("INSERT OR IGNORE INTO lineage VALUES(?,?,?,?)",
                    (payload["sequence"],packet["digest"],payload.get("previous_digest",""),
                     canonical(payload.get("binding")).decode()))
                old=self.db.execute("SELECT sequence FROM packets WHERE singleton=1").fetchone()
                if old is None or payload["sequence"] > old[0]:
                    self.db.execute("INSERT OR REPLACE INTO packets VALUES(1,?,?,?)",
                        (payload["sequence"],packet["digest"],canonical(packet).decode()))
                self._observe_floors(packet)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return checkpoint_ref(packet)

    def select_witnessed(self, reference):
        """Select an independently validated witness head, retaining newer drafts.

        Callers must validate the current grant and witness epoch before and after
        this operation. It does not certify freshness by itself. New checkpoints
        use a sequence above every observed draft, including a discarded branch.
        """
        with self.lock:
            packet=self.get(reference)
            self.db.execute("INSERT OR REPLACE INTO packets VALUES(1,?,?,?)",
                (reference["sequence"],reference["digest"],canonical(packet).decode()))
            return packet

    def _observe_floors(self, packet):
        found={}
        for row in packet["payload"]["state"].get("records",[]):
            if not isinstance(row,list) or len(row)!=3:
                continue
            kind,key,value=row
            resource,generation=None,None
            if kind=="_fence_generations":
                resource,generation=key,value
            elif kind in ("guards","leases") and isinstance(value,dict):
                resource=value.get("resource",key if kind=="guards" else None)
                generation=value.get("generation")
            if isinstance(resource,str) and type(generation) is int and generation>=0:
                found[resource]=max(found.get(resource,0),generation)
        for resource,generation in found.items():
            old=self.db.execute("SELECT generation FROM floors WHERE resource=?",
                                (resource,)).fetchone()
            self.db.execute("INSERT OR REPLACE INTO floors VALUES(?,?)",
                            (resource,max(generation,old[0] if old else 0)))

    def published(self, reference):
        """Bound payload storage after independently observed witness publication."""
        with self.lock:
            self.get(reference)
            phase=self.phase()
            keep=phase["checkpoint"] if phase else ""
            self.db.execute("DELETE FROM archive WHERE sequence<? AND digest!=? AND digest!=?",
                            (reference["sequence"]-1,reference["digest"],keep))

    def generation_floors(self, resources):
        floors={resource:0 for resource in resources}
        with self.lock:
            for resource,generation in self.db.execute("SELECT resource,generation FROM floors"):
                if resource in floors:
                    floors[resource]=max(floors[resource],generation)
            for (raw,) in self.db.execute("SELECT packet FROM archive"):
                state=json.loads(raw)["payload"]["state"]
                for row in state.get("records",[]):
                    if not isinstance(row,list) or len(row)!=3:
                        continue
                    kind,key,value=row
                    if kind=="_fence_generations" and key in floors and type(value) is int:
                        floors[key]=max(floors[key],value)
                    elif kind in ("guards","leases") and isinstance(value,dict):
                        resource=value.get("resource",key if kind=="guards" else None)
                        generation=value.get("generation")
                        if resource in floors and type(generation) is int:
                            floors[resource]=max(floors[resource],generation)
        return floors

    def bootstrap(self, state, authorization):
        """One-time scoped migration receipt, never implicit empty-state adoption."""
        if not isinstance(authorization, dict) or not authorization.get("source_receipt"):
            raise CheckpointError("Explicit initial custody receipt required")
        with self.lock:
            if self.head() is not None:
                raise CheckpointError("Checkpoint already initialized")
            packet = self._sign({
                "protocol": PROTOCOL, "service_id": self.service_id,
                "scope_digest": self.scope_digest, "sequence": 0,
                "previous_digest": "", "binding": None,
                "authorization": authorization, "state": state})
            self.install(packet)
            return packet

    def advance(self, state, current_binding):
        current_binding = binding(current_binding)
        if current_binding["service_id"] != self.service_id:
            raise CheckpointError("Wrong checkpoint service")
        with self.lock:
            old = self.packet()
            if old is None:
                raise CheckpointError("Initial custody is missing")
            if old["payload"]["state"] == state and old["payload"]["binding"] == current_binding:
                return old
            packet = self._sign({
                "protocol": PROTOCOL, "service_id": self.service_id,
                "scope_digest": self.scope_digest,
                "sequence": self.db.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM lineage").fetchone()[0],
                "previous_digest": old["digest"], "binding": current_binding,
                "state": state})
            self.install(packet)
            return packet

    def phase(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM adapter WHERE singleton=1").fetchone()
            return json.loads(row[0]) if row else None

    def _phase_write(self, value):
        self.db.execute("INSERT OR REPLACE INTO adapter VALUES(1,?)",
                        (canonical(value).decode(),))

    def receipt(self, request):
        key = request.get("id")
        if not isinstance(key, str) or not key:
            raise CheckpointError("Stable adapter request ID required")
        with self.lock:
            row = self.db.execute("SELECT request_digest,reply FROM receipts WHERE id=?",
                                  (key,)).fetchone()
            if row:
                if row[0] != digest(request):
                    raise CheckpointError("Adapter request identity reused with different payload")
                return json.loads(row[1])

    def prepare(self, request, witness_head):
        b = binding(request.get("binding"))
        if request.get("protocol") != ADAPTER_PROTOCOL or request.get("operation") != "prepare":
            raise CheckpointError("Invalid preparation request")
        with self.lock:
            head = self.head()
            if b["service_id"] != self.service_id or not same_ref(head, witness_head):
                raise CheckpointError("Prepared state does not match current witnessed checkpoint")
            prior = self.receipt(request)
            phase = self.phase()
            if prior:
                if not phase or phase["binding"] != b:
                    raise CheckpointError("Preparation receipt has been superseded")
                return prior
            proof = {"checkpoint": head["digest"], "fresh": True,
                     "custody_complete": True, "ready": True}
            response = {"protocol": ADAPTER_PROTOCOL, "id": request["id"],
                        "binding": b, "phase": "prepared", "state": proof}
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._phase_write({"binding": b, "phase": "prepared",
                                   "checkpoint": head["digest"], "fence": None})
                self.db.execute("INSERT INTO receipts VALUES(?,?,?)",
                                (request["id"], digest(request), canonical(response).decode()))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            return response

    def record_fence(self, current_binding, checkpoint, evidence):
        b = binding(current_binding)
        if not isinstance(evidence, dict) or evidence.get("fenced") is not True:
            raise CheckpointError("Observed physical fence evidence required")
        with self.lock:
            phase = self.phase()
            if not phase or phase["binding"] != b or phase["checkpoint"] != checkpoint:
                raise CheckpointError("Fence belongs to superseded preparation")
            acknowledgement = dict(evidence, binding=b, checkpoint=checkpoint,
                                   boot_epoch=b["boot_epoch"], grant_token=b["grant_token"])
            phase["fence"] = acknowledgement
            self._phase_write(phase)
            return acknowledgement

    def activate(self, request):
        b = binding(request.get("binding"))
        if request.get("protocol") != ADAPTER_PROTOCOL or request.get("operation") != "activate":
            raise CheckpointError("Invalid activation request")
        with self.lock:
            phase = self.phase()
            if (not phase or phase["binding"] != b
                    or phase["checkpoint"] != request.get("checkpoint")
                    or not phase.get("fence")
                    or phase["fence"] != request.get("fence_ack")):
                raise CheckpointError("Activation lacks the retained exact physical fence")
            prior = self.receipt(request)
            if prior:
                return prior
            response = {"protocol": ADAPTER_PROTOCOL, "id": request["id"],
                        "binding": b, "phase": "active", "checkpoint": phase["checkpoint"]}
            self.db.execute("BEGIN IMMEDIATE")
            try:
                phase["phase"] = "active"
                self._phase_write(phase)
                self.db.execute("INSERT INTO receipts VALUES(?,?,?)",
                                (request["id"], digest(request), canonical(response).decode()))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            return response
