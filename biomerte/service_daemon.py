"""Docker entry points for replicated service state and scoped authorities."""
import argparse
import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import pathlib
import ssl
import time
import tempfile
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from .app import Application
from .store import Store, Fault
from .service_checkpoint import (
    ADAPTER_PROTOCOL, PROTOCOL, BINDING_FIELDS, CheckpointError,
    CheckpointStore, binding, canonical, checkpoint_ref, digest, same_ref,
    select_current)
from .service_authority import ScopedAuthority
from .controller_authority import ControllerAuthority
from .checkpoint_chunks import (ChunkError, STATE_FORMAT, BLOCK_BYTES, state_blocks,
                                valid_digest, iter_records)
from .docker_services import container_name

LOG = logging.getLogger("biomerte.service")
MAX_BODY = 32 * 1024 * 1024


def secret(path):
    value = pathlib.Path(path).read_text().strip()
    if len(value) < 32:
        raise CheckpointError("Private service credential is missing")
    return value


def env_binding():
    names = ("ID", "REVISION", "WORKER", "INCARNATION", "BOOT_EPOCH", "GRANT")
    return binding({key:os.environ.get("BIOME_SERVICE_"+name)
                    for key,name in zip(BINDING_FIELDS,names)})


def scope_digest(config):
    if config.get("authority_kind", "physical") == "controller":
        return digest(dict(authority_kind="controller",
            service_id=config["service_id"],authority_id=config["authority_id"],site=config["site"],
            controller_aliases=sorted(config.get("controller_aliases",[])),
            operator_actors=sorted(config.get("operator_actors",[])),
            operator_actor_aliases=config.get("operator_actor_aliases",{}),
            replication_policy=config.get("replication_policy","available-peers"),
            event_default_controller=config.get("event_default_controller")))
    if config.get("authority_kind", "physical") != "physical":
        raise CheckpointError("Unknown logical authority kind")
    # Preserve existing physical-service checkpoint identities byte for byte.
    return digest({k:config[k] for k in (
        "service_id","authority_id","site","resources","providers","contracts")})


class Transport:
    def __init__(self, session, config):
        self.session, self.config = session, config
        self.origin = config["coordinator_origin"].rstrip("/")
        parts = urlsplit(self.origin)
        if parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment:
            raise CheckpointError("HTTPS coordinator origin required")
        self.key = secret(config["coordinator_key_file"])

    async def request(self, path, body=None):
        async with self.session.request(
                "GET" if body is None else "POST", self.origin+path,
                json=body, headers={"Authorization":"Bearer "+self.key},
                allow_redirects=False,
                timeout=ClientTimeout(total=self.config.get("request_timeout_s",15))) as response:
            if response.status != 200:
                raise Fault("Service coordinator returned HTTP"+str(response.status),response.status)
            return await response.json()

    async def validate(self, current_binding, preparing=True):
        started = time.monotonic()
        value = await self.request("/api/service-coordinator/"+
            ("validate-preparation" if preparing else "validate"), binding(current_binding))
        ttl = value.get("lease_ttl_s",0)
        if (value.get("valid") is not True or value.get("boot_epoch") != current_binding["boot_epoch"]
                or type(ttl) not in (int,float) or time.monotonic() >= started+ttl):
            raise Fault("Service grant is stale or unavailable",503)
        return value

    async def candidates(self):
        status = await self.request("/api/service-coordinator/status")
        service = status.get("services",{}).get(self.config["service_id"],{})
        by_worker = {}
        ambiguous = set()
        for row in service.get("candidates",[]):
            worker = row["worker_id"]
            if worker in by_worker and by_worker[worker]["worker_incarnation"] != row["worker_incarnation"]:
                ambiguous.add(worker)
            by_worker[worker] = {key:row[key] for key in ("worker_id","worker_incarnation")}
        return status, {worker:row for worker,row in by_worker.items() if worker not in ambiguous}


async def local_rpc(session, url, token, body, timeout=15, ca_file=None):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http","https") or parsed.username or parsed.password:
        raise CheckpointError("Invalid private adapter URL")
    tls=ssl.create_default_context(cafile=ca_file) if parsed.scheme=="https" and ca_file else None
    async with session.post(url,json=body,headers={"Authorization":"Bearer "+token},
                            allow_redirects=False,timeout=ClientTimeout(total=timeout),ssl=tls) as response:
        if response.status != 200:
            raise Fault("Private adapter returned HTTP"+str(response.status),response.status)
        return await response.json()


def restore_state(home, private_dir, packet, config, chunk_reader=None):
    state = packet["payload"]["state"]
    if state.get("authority") != config["authority_id"] or state.get("site") != config["site"]:
        raise CheckpointError("Checkpoint private identity mismatch")
    controller = config.get("authority_kind") == "controller"
    if controller:
        if state.get("format") != STATE_FORMAT or chunk_reader is None:
            raise CheckpointError("Complete controller shard root and block reader required")
        state_blocks(state)
        rows = iter_records(state, chunk_reader)
    else:
        rows = state.get("records")
        if not isinstance(rows,list) or any(not isinstance(row,list) or len(row)!=3 for row in rows):
            raise CheckpointError("Canonical scoped records required")
    directory = pathlib.Path(home)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ("node.json","node.key"):
        target = directory/name
        expected = str(pathlib.Path(private_dir)/name)
        if target.is_symlink():
            if os.readlink(target) != expected:
                raise CheckpointError("Unexpected service private material link")
        elif target.exists():
            raise CheckpointError("Service private material must use its read-only binding")
        else:
            target.symlink_to(expected)
    marker = directory/"controller-authority.json"
    expected_marker = {"format":1, "mode":"managed-authority", "site":config["site"],
        "service_id":config["service_id"], "authority_id":config["authority_id"],
        "controller_aliases":config.get("controller_aliases", [])}
    if controller and marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text()) != expected_marker:
            raise CheckpointError("Controller authority marker differs from prepared identity")
    # The runner calls prepare before child creation. The helper owns this replace.
    store = Store(str(directory),"site")
    try:
        if controller:
            # Restore canonical values directly in one transaction. Using an
            # ordinary command's change accumulator would duplicate the entire
            # history in RAM and would apply new-write lifecycle defaults.
            with contextlib.closing(store.connect()) as db:
                # Store has already performed normal SQLite journal recovery.
                # An initialized but empty target from a failed first attempt
                # must retain the fresh-load path on retry.
                empty = db.execute("SELECT 1 FROM records LIMIT 1").fetchone() is None
                if empty:
                    if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise CheckpointError("Controller restore requires rollback journaling")
                    db.execute("PRAGMA synchronous=EXTRA")
                    if db.execute("PRAGMA synchronous").fetchone()[0] != 3:
                        raise CheckpointError("Controller restore requires EXTRA durability")
                else:
                    db.execute("PRAGMA synchronous=FULL")
                    if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                        raise CheckpointError("Existing controller restore requires WAL")
                    # Keep only record identities, with a bounded page cache.
                    # A build that forces temporary data into RAM cannot provide
                    # this restore's disk-backed key-set guarantee.
                    options = {row[0] for row in db.execute("PRAGMA compile_options")}
                    if "TEMP_STORE=3" in options:
                        raise CheckpointError("Disk-backed controller restore keys required")
                    db.execute("PRAGMA temp_store=FILE")
                    db.execute("PRAGMA temp.cache_size=-2048")
                    db.execute("CREATE TEMP TABLE controller_restore_seen("
                               "kind TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(kind,id)) WITHOUT ROWID")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if empty and db.execute("SELECT 1 FROM records LIMIT 1").fetchone() is not None:
                        raise CheckpointError("Controller restore target changed before transaction")
                    for kind,key,value in rows:
                        if len(key) > 512:
                            raise CheckpointError("Invalid canonical controller record ID")
                        raw_value = canonical(value).decode("utf-8")
                        if empty:
                            db.execute("INSERT INTO records VALUES(?,?,?)", (kind,key,raw_value))
                        else:
                            # A delta must not conceal a duplicate input identity.
                            db.execute("INSERT INTO temp.controller_restore_seen VALUES(?,?)", (kind,key))
                            # Ordinary statements preserve conflict policies in
                            # the dirty-index triggers; UPSERT's DO UPDATE does not.
                            same = db.execute("SELECT value IS ? FROM records WHERE kind=? AND id=?",
                                              (raw_value,kind,key)).fetchone()
                            if same is None:
                                db.execute("INSERT INTO records VALUES(?,?,?)", (kind,key,raw_value))
                            elif not same[0]:
                                db.execute("UPDATE records SET value=? WHERE kind=? AND id=?",
                                           (raw_value,kind,key))
                    if not empty:
                        # Missing records may be removed only after every signed
                        # block/shard/record has passed the input iterator.
                        db.execute("DELETE FROM records WHERE NOT EXISTS("
                                   "SELECT 1 FROM temp.controller_restore_seen AS seen "
                                   "WHERE seen.kind=records.kind AND seen.id=records.id)")
                        # Full replacement previously dirtied all lifecycle
                        # metadata. Preserve its fallback-expiry/default behavior
                        # without rewriting unchanged canonical record bodies.
                        db.execute("UPDATE record_lifecycle SET state='@dirty',lease='',expires_at=NULL,delivery=0 "
                                   "WHERE (state IS NOT '@dirty' OR lease IS NOT '' OR expires_at IS NOT NULL "
                                   "OR delivery IS NOT 0) AND EXISTS("
                                   "SELECT 1 FROM records WHERE records.kind=record_lifecycle.kind "
                                   "AND records.id=record_lifecycle.id)")
                    db.execute("UPDATE position SET seq=0,hash='' WHERE id=1")
                    db.commit()
                    if empty:
                        # Bulk data is now durable in the main database. Return
                        # the target to ordinary Store mode before its marker or
                        # any preparation receipt can be published.
                        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                            raise CheckpointError("Controller restore could not return to WAL")
                        db.execute("PRAGMA synchronous=FULL")
                    if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                        raise CheckpointError("Controller restore checkpoint is still busy")
                except BaseException:
                    if db.in_transaction:
                        db.rollback()
                    raise
            if not marker.exists():
                fd, temporary = tempfile.mkstemp(prefix=".controller-authority-", dir=directory)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(canonical(expected_marker))
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, marker)
                    fd = os.open(str(directory), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        else:
            def replace(tx):
                for kind,key in list(tx.db.execute("SELECT kind,id FROM records")):
                    tx.drop(kind,key)
                for kind,key,value in rows:
                    tx.put(kind,key,value)
            store.execute(replace)
    finally:
        store.close()


class CheckpointHelper:
    def __init__(self, config, session):
        self.config,self.session = config,session
        self.transport = Transport(session,config)
        self.token = secret(config["helper_key_file"])
        self.store = CheckpointStore(
            config["checkpoint_db"],config["service_id"],scope_digest(config),
            secret(config["checkpoint_key_file"]).encode())
        self.lock = asyncio.Lock()
        self.connection = None
        self.replication = "unobserved"
        self.replica_blocks = {}
        self.confirmed_controller_checkpoint = None
        self.controller_checkpoint_refresh_at = 0
        if self.store.head() is None and config.get("bootstrap_packet"):
            packet=json.loads(pathlib.Path(config["bootstrap_packet"]).read_text())
            self.store.install(packet)

    async def validate(self,b):
        if (b["worker_id"] != self.config["worker_id"] or
                b["service_id"] != self.config["service_id"]):
            raise Fault("Adapter request belongs to another host or service",403)
        return await self.transport.validate(b)

    async def workers(self):
        status, available = await self.transport.candidates()
        return status, {w:available[w] for w in self.config["replica_workers"] if w in available}

    async def status(self):
        status, workers = await self.workers()
        controller = self.config.get("authority_kind") == "controller"
        # An empty selection asks the coordinator to enumerate current links
        # for this service. Supervisor presence alone does not register a
        # checkpoint helper, even when a sibling service is connected.
        value = await self.transport.request("/api/service-checkpoints/status", {
            "service_id": self.config["service_id"],
            "workers": [] if controller else list(workers.values())})
        if value["boot_epoch"] != status["boot_epoch"]:
            raise Fault("Witness restarted during checkpoint inspection", 503)
        if controller:
            linked = {}
            for row in value["workers"]:
                worker = row["worker_id"]
                if worker not in self.config["replica_workers"]:
                    continue
                expected = workers.get(worker)
                if not expected or row["worker_incarnation"] != expected["worker_incarnation"]:
                    raise Fault("Checkpoint helper incarnation changed during inspection", 503)
                # A registered helper remains in the replication set even
                # when its latest status RPC fails.
                linked[worker] = expected
            workers = linked
        return value, workers

    async def chunk_missing(self, body):
        b = binding(body.get("binding"))
        await self.validate(b)
        keys = body.get("digests")
        if not isinstance(keys, list) or len(keys) > 256:
            raise CheckpointError("At most 256 exact block digests required")
        for key in keys:
            valid_digest(key)
        missing = await asyncio.to_thread(self.store.chunks.missing, keys)
        await self.validate(b)
        return {"missing": missing}

    async def chunk_put(self, body):
        b = binding(body.get("binding"))
        await self.validate(b)
        block = body.get("block")
        if not isinstance(block, dict) or not isinstance(block.get("data"), str):
            raise CheckpointError("Encoded controller block required")
        if len(block["data"]) > ((BLOCK_BYTES + 2) // 3) * 4:
            raise CheckpointError("Controller block exceeds transfer limit")
        try:
            raw = base64.b64decode(block["data"], validate=True)
        except (ValueError, TypeError):
            raise CheckpointError("Invalid controller block encoding")
        result = await asyncio.to_thread(self.store.chunks.put, raw, valid_digest(block.get("digest")))
        await self.validate(b)
        return result

    async def replicate_chunks(self, b, packet, targets):
        refs = state_blocks(packet["payload"]["state"])
        if refs is None:
            return
        for target in targets:
            cache_key = (target["worker_id"], target["worker_incarnation"],
                         b["boot_epoch"], b["grant_token"])
            known = self.replica_blocks.get(cache_key, set())
            keys = [key for key in refs if key not in known]
            for offset in range(0, len(keys), 256):
                asked = keys[offset:offset + 256]
                reply = await self.transport.request("/api/service-checkpoints/chunk-missing", {
                    "service_id": self.config["service_id"], "worker": target,
                    "binding": b, "digests": asked})
                missing = reply.get("result", {}).get("missing")
                if (reply.get("status") != 200 or not isinstance(missing, list) or
                        len(missing) != len(set(missing)) or not set(missing).issubset(asked)):
                    raise Fault("Invalid replica missing-block receipt", 503)
                for key in missing:
                    raw = await asyncio.to_thread(self.store.chunks.get, key)
                    reply = await self.transport.request("/api/service-checkpoints/chunk-put", {
                        "service_id": self.config["service_id"], "worker": target, "binding": b,
                        "chunk": {"digest": key, "data": base64.b64encode(raw).decode("ascii")}})
                    value = reply.get("result", {})
                    if (reply.get("status") != 200 or value.get("digest") != key or
                            value.get("bytes") != refs[key] or value.get("durable") is not True):
                        raise Fault("Replica did not durably acknowledge exact block", 503)

    async def fetch_chunks(self, b, packet, source):
        self.store.verify(packet)
        refs = state_blocks(packet["payload"]["state"])
        if refs is None:
            return
        missing = await asyncio.to_thread(self.store.chunks.missing, refs)
        if missing and source is None:
            raise Fault("Current controller blocks are unavailable", 503)
        for key in missing:
            reply = await self.transport.request("/api/service-checkpoints/chunk-get", {
                "service_id": self.config["service_id"], "worker": source,
                "binding": b, "chunk": {"digest": key}})
            value = reply.get("result", {})
            if (reply.get("status") != 200 or value.get("digest") != key or
                    value.get("bytes") != refs[key] or not isinstance(value.get("data"), str) or
                    len(value["data"]) > ((BLOCK_BYTES + 2) // 3) * 4):
                raise Fault("Source returned a mismatched controller block", 503)
            try:
                raw = base64.b64decode(value["data"], validate=True)
            except (ValueError, TypeError):
                raise CheckpointError("Source returned invalid block encoding")
            await asyncio.to_thread(self.store.chunks.put, raw, key)
        await self.validate(b)

    async def publish_packet(self,b,packet,workers=None,require_all=False):
        await self.validate(b)
        controller = self.config.get("authority_kind") == "controller"
        if workers is None:
            _, workers = await (self.status() if controller else self.workers())
        targets=[row for worker,row in workers.items() if worker!=self.config["worker_id"]]
        if require_all and set(workers)!=set(self.config["replica_workers"]):
            raise Fault("Witness restart requires every configured replica",503)
        body={"binding":b,"local_durable":True,
              "checkpoint":dict(checkpoint_ref(packet),package=packet),"targets":targets}
        acknowledged_targets = targets
        try:
            await self.replicate_chunks(b, packet, targets)
            reply=await self.transport.request("/api/service-checkpoints/replicate",body)
            self.replication="complete" if len(targets)+1==len(self.config["replica_workers"]) else "degraded"
        except (Fault,asyncio.TimeoutError,OSError):
            self.replica_blocks.clear()
            if require_all:
                raise
            if controller:
                if self.config.get("replication_policy", "available-peers") != "available-peers":
                    raise CheckpointError("Unsupported controller replication policy")
                # An advertised peer is still owed the checkpoint. A timeout is
                # not evidence that it left membership. Refresh exact identities,
                # then retry; only an actually absent peer permits degradation.
                _, current_workers = await self.status()
                acknowledged_targets = [row for worker,row in current_workers.items()
                                        if worker != self.config["worker_id"]]
                await self.replicate_chunks(b, packet, acknowledged_targets)
                reply = await self.transport.request("/api/service-checkpoints/replicate",
                    dict(body, targets=acknowledged_targets))
                self.replication = ("complete" if len(acknowledged_targets) + 1 ==
                    len(self.config["replica_workers"]) else "degraded")
            else:
                # Retrying this immutable physical snapshot cannot replay a command.
                reply=await self.transport.request("/api/service-checkpoints/replicate",
                                                   dict(body,targets=[]))
                self.replication="degraded"
                acknowledged_targets = []
        await self.validate(b)
        if (reply.get("binding")!=b or not same_ref(reply.get("head"),checkpoint_ref(packet))
                or reply["head"].get("boot_epoch")!=b["boot_epoch"]):
            raise Fault("Witness did not acknowledge this exact durable checkpoint",503)
        acknowledgements = reply.get("acknowledgements", [])
        expected = {(target["worker_id"], target["worker_incarnation"])
                    for target in acknowledged_targets}
        actual = set()
        for acknowledgement in acknowledgements:
            key = (acknowledgement.get("worker_id"), acknowledgement.get("worker_incarnation"))
            if (key in actual or acknowledgement.get("durable") is not True or
                    not same_ref(acknowledgement.get("checkpoint"), checkpoint_ref(packet))):
                raise Fault("Witness returned an invalid durable replica acknowledgement", 503)
            actual.add(key)
        if actual != expected:
            raise Fault("Witness acknowledgement set differs from requested replicas", 503)
        refs = state_blocks(packet["payload"]["state"])
        if refs is not None:
            self.replica_blocks = {(target["worker_id"], target["worker_incarnation"],
                b["boot_epoch"], b["grant_token"]): set(refs) for target in acknowledged_targets}
        await asyncio.to_thread(self.store.published,checkpoint_ref(packet))
        if controller:
            self.confirmed_controller_checkpoint = (b, reply["checkpoint"])
            # Recheck even an idle root so a returning helper can catch up.
            self.controller_checkpoint_refresh_at = time.monotonic() + 5
        return reply

    async def prepare(self,request):
        b=binding(request.get("binding"))
        await self.validate(b)
        async with self.lock:
            prior=await asyncio.to_thread(self.store.receipt,request)
            phase=await asyncio.to_thread(self.store.phase)
            if prior and phase and phase["binding"]==b:
                await self.validate(b)
                return prior
            observed,workers=await self.status()
            head=observed.get("head")
            heads={}
            for row in observed.get("workers",[]):
                if row.get("status")!=200 or row.get("result",{}).get("durable") is not True:
                    continue
                ref=row.get("checkpoint")
                retained=row.get("result",{}).get("retained",[])
                if head:
                    ref=next((v for v in [ref]+retained if same_ref(v,head)),ref)
                if ref:
                    heads[row["worker_id"]]=ref
            own=await asyncio.to_thread(self.store.head)
            if own:
                try:
                    local_packet = await asyncio.to_thread(self.store.packet)
                    await asyncio.to_thread(self.store.require_chunks, local_packet["payload"]["state"])
                except CheckpointError:
                    own = None
            if own:
                if head:
                    local_refs=await asyncio.to_thread(self.store.retained)
                    own=next((v for v in local_refs if same_ref(v,head)),own)
                heads[self.config["worker_id"]]=own
            selected=select_current(self.config["service_id"],head,heads,
                                    self.config["replica_workers"],b["boot_epoch"])
            ref=selected["head"]
            try:
                packet=await asyncio.to_thread(self.store.get,ref)
            except CheckpointError:
                source=workers.get(selected["source"])
                if source is None:
                    raise Fault("Current checkpoint source is unavailable",503)
                reply=await self.transport.request("/api/service-checkpoints/get",{
                    "service_id":self.config["service_id"],"worker":source,"checkpoint":ref})
                if not same_ref(reply.get("checkpoint"),ref) or reply.get("status")!=200:
                    raise Fault("Checkpoint reply does not match selected witness",503)
                packet=reply["result"]["package"]
            await self.fetch_chunks(b, packet, workers.get(selected["source"]))
            await asyncio.to_thread(self.store.retain,packet)
            await self.validate(b)
            await asyncio.to_thread(self.store.select_witnessed,ref)
            await self.publish_packet(b,packet,workers,require_all=selected["reestablish"])
            await asyncio.to_thread(restore_state,self.config["authority_home"],
                                    self.config["private_dir"],packet,self.config,self.store.chunks.get)
            await self.validate(b)
            return await asyncio.to_thread(self.store.prepare,request,ref)

    async def publish(self,body):
        b=binding(body.get("binding"))
        await self.validate(b)
        async with self.lock:
            phase=await asyncio.to_thread(self.store.phase)
            if not phase or phase["binding"]!=b:
                raise Fault("No exact prepared local authority",409)
            state=body.get("state")
            if (not isinstance(state,dict) or state.get("authority")!=self.config["authority_id"]
                    or state.get("site")!=self.config["site"]):
                raise Fault("Wrong logical service state",403)
            if self.config.get("authority_kind") == "controller" and state.get("format") != STATE_FORMAT:
                raise Fault("Controller authority requires a complete shard root", 409)
            packet=await asyncio.to_thread(self.store.advance,state,b)
            ref = checkpoint_ref(packet)
            if (self.confirmed_controller_checkpoint == (b, ref)
                    and time.monotonic() < self.controller_checkpoint_refresh_at):
                # This exact root already passed replication, the durable
                # witness and the current-binding check within this helper.
                return {"checkpoint": ref, "replication": self.replication}
            reply=await self.publish_packet(b,packet)
            return {"checkpoint":reply["checkpoint"],"replication":self.replication}

    async def fence(self,body):
        b=binding(body)
        await self.validate(b)
        phase=await asyncio.to_thread(self.store.phase)
        proof=body.get("prepared_state",{})
        if (not phase or phase["binding"]!=b or proof.get("checkpoint")!=phase["checkpoint"]
                or proof.get("binding")!=b):
            raise Fault("Physical fence lacks exact prepared state",409)
        if phase.get("fence"):
            return phase["fence"]
        name=container_name({"id":b["service_id"],"revision":b["manifest_revision"]},
                            b["worker_id"],b["worker_incarnation"],b)
        url=self.config["child_url_template"].format(
            container_name=name,service_id=b["service_id"],
            incarnation=b["worker_incarnation"][:8],grant=b["grant_token"][:8])
        reply=await local_rpc(self.session,url+"/fence",self.token,{"binding":b},
                              self.config.get("request_timeout_s",15))
        await self.validate(b)
        if reply.get("binding")!=b or reply.get("checkpoint")!=phase["checkpoint"]:
            raise Fault("Child fence response is stale",409)
        return await asyncio.to_thread(self.store.record_fence,b,phase["checkpoint"],reply)

    async def activate(self,body):
        b=binding(body.get("binding"))
        await self.validate(b)
        # Coordinator effect readiness must already acknowledge the same fence.
        await self.transport.validate(b,preparing=False)
        return await asyncio.to_thread(self.store.activate,body)

    async def health(self,request):
        head=await asyncio.to_thread(self.store.head)
        complete = bool(head)
        if head:
            try:
                packet = await asyncio.to_thread(self.store.packet)
                await asyncio.to_thread(self.store.require_chunks, packet["payload"]["state"])
            except CheckpointError:
                complete = False
        ready=bool(complete and self.connection)
        return web.json_response({"ready":ready,"checkpoint":head,
                                  "replication":self.replication},status=200 if ready else 503)

    async def handle(self,request):
        if request.headers.get("Origin") or not hmac.compare_digest(
                request.headers.get("Authorization",""),"Bearer "+self.token):
            raise web.HTTPUnauthorized()
        try:
            body=await request.json()
            if request.path=="/fence":
                value=await self.fence(body)
            elif body.get("operation")=="prepare":
                value=await self.prepare(body)
            elif body.get("operation")=="activate":
                value=await self.activate(body)
            elif body.get("operation")=="publish":
                value=await self.publish(body)
            elif body.get("operation")=="chunk_missing":
                value=await self.chunk_missing(body)
            elif body.get("operation")=="chunk_put":
                value=await self.chunk_put(body)
            elif body.get("operation")=="status":
                b=binding(body.get("binding"))
                await self.validate(b)
                phase=await asyncio.to_thread(self.store.phase)
                if not phase or phase["binding"]!=b:
                    raise Fault("Superseded local service phase",409)
                value=dict(phase,generation_floors=await asyncio.to_thread(
                    self.store.generation_floors,self.config.get("resources", [])),
                    replication=self.replication)
            else:
                raise Fault("Unknown service adapter operation",400)
            return web.json_response(value,headers={"Cache-Control":"no-store"})
        except (CheckpointError,ChunkError,Fault,KeyError,TypeError,ValueError) as error:
            return web.json_response({"error":str(error)},status=getattr(error,"status",409))
        except (asyncio.TimeoutError,OSError):
            return web.json_response({"error":"Service transport is unavailable"},status=503)

    async def worker_reply(self,message,worker):
        b=message.get("binding")
        ref=message.get("checkpoint") or {}
        reply=dict(type="checkpoint_result",id=message.get("id"),
                   worker_id=worker["worker_id"],worker_incarnation=worker["worker_incarnation"],
                   boot_epoch=message.get("boot_epoch"),binding=b,
                   service_id=self.config["service_id"],
                   checkpoint=None,status=200,result={"durable":True})
        try:
            if (message.get("service_id") != self.config["service_id"] or
                    message.get("worker_id") != worker["worker_id"] or
                    message.get("worker_incarnation") != worker["worker_incarnation"] or
                    (b is not None and binding(b)["service_id"] != self.config["service_id"])):
                raise Fault("Helper RPC belongs to another service or supervisor",403)
            status=await self.transport.request("/api/service-coordinator/status")
            if status["boot_epoch"]!=message.get("boot_epoch"):
                raise Fault("Stale helper RPC epoch",409)
            if message["type"] in ("checkpoint.chunk.put", "checkpoint.chunk.get",
                                     "checkpoint.chunk.missing"):
                await self.transport.validate(binding(b))
                if message["type"] == "checkpoint.chunk.put":
                    block = message.get("chunk")
                    if (not isinstance(block, dict) or not isinstance(block.get("data"), str) or
                            len(block["data"]) > ((BLOCK_BYTES + 2) // 3) * 4):
                        raise CheckpointError("Invalid bounded block upload")
                    raw = base64.b64decode(block["data"], validate=True)
                    reply["result"] = await asyncio.to_thread(
                        self.store.chunks.put, raw, valid_digest(block.get("digest")))
                elif message["type"] == "checkpoint.chunk.get":
                    key = valid_digest(message.get("chunk", {}).get("digest"))
                    raw = await asyncio.to_thread(self.store.chunks.get, key)
                    reply["result"] = {"digest": key, "bytes": len(raw),
                        "data": base64.b64encode(raw).decode("ascii")}
                else:
                    keys = message.get("digests")
                    if not isinstance(keys, list) or len(keys) > 256:
                        raise CheckpointError("Bounded missing-block query required")
                    for key in keys:
                        valid_digest(key)
                    reply["result"] = {"missing": await asyncio.to_thread(self.store.chunks.missing, keys)}
                await self.transport.validate(binding(b))
            elif message["type"]=="checkpoint.published":
                await self.transport.validate(binding(b))
                await asyncio.to_thread(self.store.published, ref)
                reply["checkpoint"] = ref
            elif message["type"]=="checkpoint.put":
                await self.transport.validate(binding(b))
                packet=ref["package"]
                if not same_ref(ref,checkpoint_ref(packet)):
                    raise Fault("Checkpoint payload reference mismatch",409)
                await asyncio.to_thread(self.store.retain,packet)
                await self.transport.validate(binding(b))
                reply["checkpoint"]=checkpoint_ref(packet)
            elif message["type"]=="checkpoint.get":
                packet=await asyncio.to_thread(self.store.get,ref)
                reply["checkpoint"]=checkpoint_ref(packet)
                reply["result"]["package"]=packet
            elif message["type"]=="checkpoint.status":
                packet = await asyncio.to_thread(self.store.packet)
                if packet:
                    await asyncio.to_thread(self.store.require_chunks, packet["payload"]["state"])
                reply["checkpoint"]=await asyncio.to_thread(self.store.head)
                reply["result"]["retained"]=await asyncio.to_thread(self.store.retained)
            else:
                raise Fault("Unknown helper operation",400)
        except (CheckpointError,ChunkError,Fault,KeyError,TypeError,ValueError) as error:
            reply.update(status=getattr(error,"status",409),error=str(error),result={})
        except (asyncio.TimeoutError,OSError):
            reply.update(status=503,error="Helper validation unavailable",result={})
        return reply

    async def watch_incarnation(self,ws,worker):
        # The supervisor and state helper have independent lifetimes. No RPC
        # arrives on an obsolete registration, so inspect ownership separately.
        try:
            while not ws.closed:
                await asyncio.sleep(self.config.get("incarnation_check_s",1))
                unused,workers=await self.workers()
                if workers.get(worker["worker_id"])!=worker:
                    await ws.close()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unavailable witness supplies no current identity assurance.
            await ws.close()

    async def worker_loop(self):
        while True:
            try:
                status,workers=await self.workers()
                worker=workers.get(self.config["worker_id"])
                if not worker:
                    await asyncio.sleep(1)
                    continue
                origin=self.transport.origin
                url="wss://"+urlsplit(origin).netloc+"/service-worker/"+worker["worker_id"]+"/connect"
                async with self.session.ws_connect(url,
                    headers={"Authorization":"Bearer "+self.transport.key},
                    heartbeat=15,max_msg_size=MAX_BODY) as ws:
                    await ws.send_json(dict(type="register",
                        service_id=self.config["service_id"],**worker))
                    registered=await ws.receive_json(timeout=self.config.get("request_timeout_s",15))
                    if (not isinstance(registered,dict) or registered.get("type")!="registered" or
                            registered.get("protocol")!=PROTOCOL or
                            registered.get("service_id")!=self.config["service_id"] or
                            registered.get("worker_id")!=worker["worker_id"] or
                            registered.get("worker_incarnation")!=worker["worker_incarnation"] or
                            registered.get("boot_epoch")!=status["boot_epoch"]):
                        raise Fault("Exact scoped helper registration required",503)
                    self.connection=worker
                    monitor=asyncio.create_task(self.watch_incarnation(ws,worker))
                    try:
                        async for event in ws:
                            if event.type!=WSMsgType.TEXT:
                                if event.type in (WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR):
                                    break
                                continue
                            message=json.loads(event.data)
                            if message.get("type")=="registered":
                                continue
                            await ws.send_json(await self.worker_reply(message,worker))
                    finally:
                        monitor.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await monitor
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("Checkpoint helper reconnect: %s",type(error).__name__)
            finally:
                self.connection=None
            await asyncio.sleep(1)


class AuthorityDaemon:
    def __init__(self,config,session):
        self.config,self.session=config,session
        self.binding=env_binding()
        self.transport=Transport(session,config)
        self.token=secret(config["helper_key_file"])
        self.url=os.environ.get("BIOME_SERVICE_ADAPTER_URL") or config["adapter_url"]
        controller=config.get("authority_kind")=="controller"
        runtime = dict(config, site_rollback_journal=
            os.environ.get("BIOME_SITE_ROLLBACK_JOURNAL") == "1") if controller else None
        self.app=Application(config["authority_home"],controller_runtime=runtime)
        authority_type=ControllerAuthority if controller else ScopedAuthority
        self.authority=authority_type(self.app,config,self.binding)
        self.lock=asyncio.Lock()
        self.connected=False
        self.events=None
        self.confirmed_controller_publication = None
        self.controller_publication_refresh_at = 0
        if controller and config.get("event_authorize_url"):
            from .controller_events import ManagedEvents
            self.events=ManagedEvents(self)
            self.authority.events=self.events

    async def helper(self,operation,**kw):
        return await local_rpc(self.session,self.url,self.token,
            dict(operation=operation,binding=self.binding,**kw),
            self.config.get("request_timeout_s",15),self.config.get("adapter_ca_file"))

    async def phase(self):
        return await self.helper("status")

    async def publish(self):
        if self.config.get("authority_kind")=="controller":
            from .controller_checkpoint import publish_controller
            if (self.confirmed_controller_publication is not None
                    and time.monotonic() < self.controller_publication_refresh_at
                    and not await asyncio.to_thread(self.authority.checkpoint_is_dirty)):
                return self.confirmed_controller_publication
            # A failed or uncertain publication must not reuse an earlier ACK,
            # even when snapshot construction already consumed the dirty list.
            self.confirmed_controller_publication = None
            result = await publish_controller(self)
            self.confirmed_controller_publication = result
            self.controller_publication_refresh_at = time.monotonic() + 5
            return result
        state=await asyncio.to_thread(self.authority.snapshot)
        return await self.helper("publish",state=state)

    async def start_fences(self):
        await self.transport.validate(self.binding)
        phase=await self.phase()
        checkpoint=os.environ["BIOME_SERVICE_CHECKPOINT"]
        if phase["checkpoint"]!=checkpoint:
            raise Fault("Child checkpoint differs from prepared baseline",409)
        async with self.lock:
            await asyncio.to_thread(self.authority.prepare_fences,checkpoint,
                                    phase.get("generation_floors",{}))
            await self.publish()

    async def dispatch(self,message):
        typ=message.get("type")
        reply=dict(type={"inspect":"inspect_result","pair":"pair_result"}.get(typ,"call_result"),
                   id=message.get("id"),binding=self.binding,status=200)
        try:
            if message.get("binding")!=self.binding:
                raise Fault("RPC belongs to another service grant",409)
            controller = self.config.get("authority_kind") == "controller"
            # The controller phase RPC already validates this exact binding
            # against the coordinator through the private helper.
            if not controller:
                await self.transport.validate(self.binding)
            phase=await self.phase()
            preparing=phase["phase"]!="active" or bool(message.get("preparation"))
            async with self.lock:
                # Reconcile any locally durable update whose previous response
                # was lost before processing another request.
                await self.publish()
                if typ=="call":
                    result=await asyncio.to_thread(self.authority.call,
                                      message["envelope"],preparing)
                    reply["result"]=result
                elif typ=="pair" and self.config.get("authority_kind")=="controller":
                    reply["result"]=await asyncio.to_thread(self.authority.pair,
                        message.get("operation"),message.get("payload"),preparing)
                elif typ=="inspect" and not preparing:
                    requested=message.get("authority")
                    if self.config.get("authority_kind")=="controller":
                        if requested not in [self.config["authority_id"]]+self.config.get("controller_aliases",[]):
                            raise Fault("Wrong managed stream authority",403)
                        reply["grant"]=await asyncio.to_thread(
                            self.authority.inspect,message["session"],requested)
                    else:
                        if requested!=self.config["authority_id"]:
                            raise Fault("Wrong logical stream authority",403)
                        reply["grant"]=await asyncio.to_thread(self.authority.inspect,message["session"])
                else:
                    raise Fault("Service effect admission is unavailable",503)
                await self.publish()
                if not controller:
                    await self.transport.validate(self.binding,preparing=preparing)
            if controller:
                # The result is already witnessed. The final check still
                # fences the reply, without holding the application lock.
                await self.transport.validate(self.binding,preparing=preparing)
        except (Fault,CheckpointError,KeyError,TypeError) as error:
            reply.update(status=getattr(error,"status",409),error=str(error))
            reply.pop("result",None)
            reply.pop("grant",None)
        except (OSError,asyncio.TimeoutError):
            reply.update(status=503,error="Service checkpoint transport unavailable")
            reply.pop("result",None)
            reply.pop("grant",None)
        return reply

    async def fence(self,request):
        if request.headers.get("Origin") or not hmac.compare_digest(
                request.headers.get("Authorization",""),"Bearer "+self.token):
            raise web.HTTPUnauthorized()
        try:
            body=await request.json()
            if body.get("binding")!=self.binding:
                raise Fault("Wrong child service binding",409)
            await self.transport.validate(self.binding)
            async with self.lock:
                evidence=await asyncio.to_thread(self.authority.finish_fences)
                await self.publish()
                await self.transport.validate(self.binding)
            return web.json_response(evidence)
        except (Fault,CheckpointError) as error:
            return web.json_response({"error":str(error)},status=getattr(error,"status",409))

    async def health(self,request):
        try:
            # In the managed controller, the private phase already validates
            # this exact binding. Its active phase and registered socket are
            # readiness evidence for the coordinator; requiring the final
            # coordinator effect grant here makes that report circular.
            if self.config.get("authority_kind") != "controller":
                await self.transport.validate(self.binding,preparing=False)
            phase=await self.phase()
            ready=self.connected and phase["phase"]=="active"
            return web.json_response({"ready":ready,"service_id":self.config["service_id"],
                "phase":phase["phase"],"replication":phase.get("replication")},
                status=200 if ready else 503)
        except Exception:
            return web.json_response({"ready":False},status=503)

    async def maintain_once(self):
        await self.transport.validate(self.binding,preparing=False)
        phase=await self.phase()
        if phase["phase"]!="active":
            return
        async with self.lock:
            await self.publish()
            await asyncio.to_thread(self.authority.maintain)
            await self.publish()
            await self.transport.validate(self.binding,preparing=False)
            if getattr(self,"events",None) is not None:
                await asyncio.to_thread(self.events.deliver)
                await self.publish()
                await self.transport.validate(self.binding,preparing=False)

    async def maintenance_loop(self):
        interval=self.config.get("maintenance_interval_s",1)
        if type(interval) not in (int,float) or interval<=0:
            raise CheckpointError("Positive service maintenance interval required")
        while True:
            try:
                await self.maintain_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("Service lifecycle retry: %s",type(error).__name__)
            await asyncio.sleep(interval)

    async def reply_to_authority_request(self, ws, message):
        started = time.monotonic()
        result = await self.dispatch(message)
        if not ws.closed:
            await ws.send_json(result)
        elapsed = time.monotonic() - started
        if self.config.get("authority_kind") == "controller" and elapsed >= 3:
            LOG.warning("Managed runtime slow RPC type=%s status=%s seconds=%.3f",
                result["type"], result["status"], elapsed)

    @staticmethod
    def authority_request_band(message):
        envelope = message.get("envelope") or {}
        operation = envelope.get("operation", "")
        if message.get("type") == "inspect" or operation == "stream.inspect":
            return "inspection"
        if operation in ("cap.discover", "cap.status", "provider.poll"):
            return "read"
        if operation in ("stream.open", "stream.attach"):
            return "attachment"
        if isinstance(operation, str) and (operation.startswith("stream.") or operation in (
                "cap.acquire", "cap.fenced", "cap.lease")):
            return "lifecycle"
        return "general"

    @staticmethod
    def authority_response(message, binding):
        typ = {"inspect": "inspect_result", "pair": "pair_result"}.get(
            message.get("type"), "call_result")
        return dict(type=typ, id=message.get("id"), binding=binding, status=200)

    async def dispatch_managed_reads(self, messages):
        # Stream grant inspection and provider inbox reads cannot expose
        # unacknowledged state. A batch shares the before/after publication
        # barrier, and each signed envelope still passes native authorization.
        replies = [self.authority_response(message, self.binding) for message in messages]

        def reject(reply, error):
            reply.pop("grant", None)
            reply.pop("result", None)
            reply.update(status=getattr(error, "status", 409), error=str(error))

        try:
            if (await self.phase())["phase"] != "active":
                raise Fault("Service effect admission is unavailable", 503)
            async with self.lock:
                await self.publish()
                for message, reply in zip(messages, replies):
                    try:
                        if message.get("binding") != self.binding:
                            raise Fault("RPC belongs to another service grant", 409)
                        if message.get("preparation") or self.authority_request_band(message) not in (
                                "read", "inspection"):
                            raise Fault("Service effect admission is unavailable", 503)
                        if message["type"] == "inspect":
                            requested = message.get("authority")
                            if requested not in [self.config["authority_id"]] + self.config.get(
                                    "controller_aliases", []):
                                raise Fault("Wrong managed stream authority", 403)
                            reply["grant"] = await asyncio.to_thread(self.authority.inspect,
                                message["session"], requested)
                        else:
                            reply["result"] = await asyncio.to_thread(self.authority.call,
                                message["envelope"], False)
                    except (Fault, CheckpointError, KeyError, TypeError) as error:
                        reject(reply, error)
                    except ValueError:
                        reject(reply, Fault("Invalid managed read request", 400))
                await self.publish()
            await self.transport.validate(self.binding, preparing=False)
        except (Fault, CheckpointError, KeyError, TypeError) as error:
            for reply in replies:
                reject(reply, error)
        except (OSError, asyncio.TimeoutError):
            for reply in replies:
                reject(reply, Fault("Service checkpoint transport unavailable", 503))
        return replies

    async def serve_controller_socket(self, ws):
        # Keep the wire reader responsive while the single dispatcher runs
        # fenced transactions. Lease updates, reads, and general work each
        # have bounded waiting space and take turns if all are busy.
        bands = ("read", "lifecycle", "general")
        stream_bands = ("attachment", "inspection")
        capacity = {"attachment": 1, "inspection": 4, "read": 8, "lifecycle": 1, "general": 1}
        pending = {band: [] for band in capacity}
        ready = asyncio.Event()
        stopping = False

        async def dispatch_queue():
            index = 0
            stream_index = 0
            previous = None
            try:
                while not stopping:
                    await ready.wait()
                    if stopping:
                        break
                    # Grant validation must not compete for admission with
                    # inbox reads. Alternate stream work and ordinary work.
                    if any(pending[band] for band in stream_bands) and (
                            previous not in stream_bands or not any(pending[band] for band in bands)):
                        while not pending[stream_bands[stream_index]]:
                            stream_index = (stream_index + 1) % len(stream_bands)
                        band = stream_bands[stream_index]
                        stream_index = (stream_index + 1) % len(stream_bands)
                    else:
                        while not pending[bands[index]]:
                            index = (index + 1) % len(bands)
                        band = bands[index]
                        index = (index + 1) % len(bands)
                    batch = pending[band]
                    pending[band] = []
                    previous = band
                    if not any(pending.values()):
                        ready.clear()
                    if len(batch) == 1:
                        await self.reply_to_authority_request(ws, batch[0])
                    else:
                        replies = await self.dispatch_managed_reads(batch)
                        for reply in replies:
                            if not ws.closed:
                                await ws.send_json(reply)
            finally:
                if not ws.closed:
                    await ws.close()

        dispatcher = asyncio.create_task(dispatch_queue())
        try:
            async for event in ws:
                if event.type != WSMsgType.TEXT:
                    continue
                message = json.loads(event.data)
                if message.get("type") == "registered":
                    self.connected = True
                    continue
                band = self.authority_request_band(message)
                queue = pending[band]
                if len(queue) >= capacity[band]:
                    reply = self.authority_response(message, self.binding)
                    reply.update(status=429, error="Managed controller is busy")
                    await ws.send_json(reply)
                    continue
                queue.append(message)
                ready.set()
        finally:
            stopping = True
            pending.clear()
            ready.set()
            # A running to_thread transaction must settle before reconnecting.
            await dispatcher

    async def authority_loop(self):
        while True:
            try:
                await self.start_fences()
                break
            except (Fault,CheckpointError,OSError,asyncio.TimeoutError) as error:
                LOG.warning("Service fence preparation retry: %s",type(error).__name__)
                await asyncio.sleep(1)
        while True:
            try:
                await self.transport.validate(self.binding)
                url="wss://"+urlsplit(self.transport.origin).netloc+"/service-authority/"+self.binding["service_id"]+"/connect"
                async with self.session.ws_connect(url,
                    headers={"Authorization":"Bearer "+self.transport.key},
                    heartbeat=15,max_msg_size=MAX_BODY) as ws:
                    await ws.send_json({"type":"register","binding":self.binding})
                    if self.config.get("authority_kind") == "controller":
                        await self.serve_controller_socket(ws)
                    else:
                        async for event in ws:
                            if event.type != WSMsgType.TEXT:
                                continue
                            message = json.loads(event.data)
                            if message.get("type") == "registered":
                                self.connected = True
                                continue
                            await self.reply_to_authority_request(ws, message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("Service authority reconnect: %s",type(error).__name__)
            finally:
                self.connected=False
            await asyncio.sleep(1)


async def run(config,role):
    async with ClientSession() as session:
        instance=CheckpointHelper(config,session) if role=="helper" else AuthorityDaemon(config,session)
        app=web.Application(client_max_size=MAX_BODY)
        if role=="helper":
            app.router.add_get("/health",instance.health)
            app.router.add_post("/adapter",instance.handle)
            app.router.add_post("/fence",instance.handle)
            loop=instance.worker_loop
        else:
            app.router.add_post("/fence",instance.fence)
            app.router.add_get("/health",instance.health)
            loop=instance.authority_loop
        runner=web.AppRunner(app,access_log=None)
        await runner.setup()
        port=config.get("helper_port",8870) if role=="helper" else config.get("authority_port",8871)
        tls=None
        cert,key=config.get("helper_tls_cert_file"),config.get("helper_tls_key_file")
        if role=="helper" and (cert or key):
            if not cert or not key:
                raise CheckpointError("Both private helper TLS certificate and key are required")
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert,key)
        await web.TCPSite(runner,"0.0.0.0",port,ssl_context=tls).start()
        tasks=[asyncio.create_task(loop())]
        if role=="authority":
            tasks.append(asyncio.create_task(instance.maintenance_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await runner.cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role",choices=("helper","authority"),required=True)
    parser.add_argument("--config",default="/config/service.json")
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config=json.loads(pathlib.Path(args.config).read_text())
    asyncio.run(run(config,args.role))


if __name__=="__main__":
    main()
