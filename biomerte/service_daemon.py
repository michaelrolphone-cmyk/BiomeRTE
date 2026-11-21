"""Docker entry points for replicated service state and scoped authorities."""
import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import pathlib
import ssl
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from .app import Application
from .store import Store, Fault
from .service_checkpoint import (
    ADAPTER_PROTOCOL, PROTOCOL, BINDING_FIELDS, CheckpointError,
    CheckpointStore, binding, canonical, checkpoint_ref, digest, same_ref,
    select_current)
from .service_authority import ScopedAuthority
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


def restore_state(home, private_dir, packet, config):
    state = packet["payload"]["state"]
    if state.get("authority") != config["authority_id"] or state.get("site") != config["site"]:
        raise CheckpointError("Checkpoint private identity mismatch")
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
    # The runner calls prepare before child creation. The helper owns this replace.
    store = Store(str(directory),"site")
    try:
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
        if self.store.head() is None and config.get("bootstrap_packet"):
            packet=json.loads(pathlib.Path(config["bootstrap_packet"]).read_text())
            self.store.install(packet)

    async def validate(self,b):
        if b["worker_id"] != self.config["worker_id"]:
            raise Fault("Adapter request belongs to another host",403)
        return await self.transport.validate(b)

    async def workers(self):
        status, available = await self.transport.candidates()
        return status, {w:available[w] for w in self.config["replica_workers"] if w in available}

    async def status(self):
        status,workers = await self.workers()
        value = await self.transport.request("/api/service-checkpoints/status",{
            "service_id":self.config["service_id"],"workers":list(workers.values())})
        if value["boot_epoch"] != status["boot_epoch"]:
            raise Fault("Witness restarted during checkpoint inspection",503)
        return value,workers

    async def publish_packet(self,b,packet,workers=None,require_all=False):
        await self.validate(b)
        if workers is None:
            unused,workers = await self.workers()
        targets=[row for worker,row in workers.items() if worker!=self.config["worker_id"]]
        if require_all and set(workers)!=set(self.config["replica_workers"]):
            raise Fault("Witness restart requires every configured replica",503)
        body={"binding":b,"local_durable":True,
              "checkpoint":dict(checkpoint_ref(packet),package=packet),"targets":targets}
        try:
            reply=await self.transport.request("/api/service-checkpoints/replicate",body)
            self.replication="complete" if len(targets)+1==len(self.config["replica_workers"]) else "degraded"
        except (Fault,asyncio.TimeoutError,OSError):
            if require_all:
                raise
            # Retrying the same immutable snapshot cannot replay an application
            # command. Record the current head before any effect-bearing reply.
            reply=await self.transport.request("/api/service-checkpoints/replicate",
                                               dict(body,targets=[]))
            self.replication="degraded"
        await self.validate(b)
        if (reply.get("binding")!=b or not same_ref(reply.get("head"),checkpoint_ref(packet))
                or reply["head"].get("boot_epoch")!=b["boot_epoch"]):
            raise Fault("Witness did not acknowledge this exact durable checkpoint",503)
        await asyncio.to_thread(self.store.published,checkpoint_ref(packet))
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
                await asyncio.to_thread(self.store.retain,packet)
            await self.validate(b)
            await asyncio.to_thread(self.store.select_witnessed,ref)
            await self.publish_packet(b,packet,workers,require_all=selected["reestablish"])
            await asyncio.to_thread(restore_state,self.config["authority_home"],
                                    self.config["private_dir"],packet,self.config)
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
            packet=await asyncio.to_thread(self.store.advance,state,b)
            reference=checkpoint_ref(packet)
            previous=getattr(self,"last_publication",None)
            # The grant is checked before entering the lock. Recheck it here
            # before reusing an exact, recently witnessed checkpoint, including
            # when the local lock has delayed this request.
            if (previous is not None and previous["binding"]==b
                    and same_ref(previous["reference"],reference)
                    and time.monotonic()<previous["expires"]):
                await self.validate(b)
                return dict(previous["result"])
            reply=await self.publish_packet(b,packet)
            result={"checkpoint":reply["checkpoint"],"replication":self.replication}
            self.last_publication={"binding":b,"reference":reference,"result":result,
                                   "expires":time.monotonic()+5}
            return dict(result)

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
        ready=bool(head and self.connection)
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
            elif body.get("operation")=="status":
                b=binding(body.get("binding"))
                await self.validate(b)
                phase=await asyncio.to_thread(self.store.phase)
                if not phase or phase["binding"]!=b:
                    raise Fault("Superseded local service phase",409)
                value=dict(phase,generation_floors=await asyncio.to_thread(
                    self.store.generation_floors,self.config["resources"]),
                    replication=self.replication)
            else:
                raise Fault("Unknown service adapter operation",400)
            return web.json_response(value,headers={"Cache-Control":"no-store"})
        except (CheckpointError,Fault,KeyError,TypeError) as error:
            return web.json_response({"error":str(error)},status=getattr(error,"status",409))
        except (asyncio.TimeoutError,OSError):
            return web.json_response({"error":"Service transport is unavailable"},status=503)

    async def worker_reply(self,message,worker):
        b=message.get("binding")
        ref=message.get("checkpoint") or {}
        reply=dict(type="checkpoint_result",id=message.get("id"),
                   worker_id=worker["worker_id"],worker_incarnation=worker["worker_incarnation"],
                   boot_epoch=message.get("boot_epoch"),binding=b,
                   checkpoint=None,status=200,result={"durable":True})
        try:
            status=await self.transport.request("/api/service-coordinator/status")
            if status["boot_epoch"]!=message.get("boot_epoch"):
                raise Fault("Stale helper RPC epoch",409)
            if message["type"]=="checkpoint.put":
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
                reply["checkpoint"]=await asyncio.to_thread(self.store.head)
                reply["result"]["retained"]=await asyncio.to_thread(self.store.retained)
            else:
                raise Fault("Unknown helper operation",400)
        except (CheckpointError,Fault,KeyError,TypeError) as error:
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
                unused,workers=await self.workers()
                worker=workers.get(self.config["worker_id"])
                if not worker:
                    await asyncio.sleep(1)
                    continue
                origin=self.transport.origin
                url="wss://"+urlsplit(origin).netloc+"/service-worker/"+worker["worker_id"]+"/connect"
                async with self.session.ws_connect(url,
                    headers={"Authorization":"Bearer "+self.transport.key},
                    heartbeat=15,max_msg_size=MAX_BODY) as ws:
                    self.connection=worker
                    await ws.send_json(dict(type="register",**worker))
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
        self.app=Application(config["authority_home"])
        self.authority=ScopedAuthority(self.app,config,self.binding)
        self.lock=asyncio.Lock()
        self.connected=False

    async def helper(self,operation,**kw):
        return await local_rpc(self.session,self.url,self.token,
            dict(operation=operation,binding=self.binding,**kw),
            self.config.get("request_timeout_s",15),self.config.get("adapter_ca_file"))

    async def phase(self):
        return await self.helper("status")

    async def publish(self):
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
        reply=dict(type="inspect_result" if typ=="inspect" else "call_result",
                   id=message.get("id"),binding=self.binding,status=200)
        try:
            if message.get("binding")!=self.binding:
                raise Fault("RPC belongs to another service grant",409)
            await self.transport.validate(self.binding,preparing=typ!="inspect")
            phase=await self.phase()
            preparing=phase["phase"]!="active" or bool(message.get("preparation"))
            if typ=="inspect":
                if preparing:
                    raise Fault("Service effect admission is unavailable",503)
                if message.get("authority")!=self.config["authority_id"]:
                    raise Fault("Wrong logical stream authority",403)
                # Inspection is read-only. The router already requires an
                # attachment credential, which is issued after publication.
                # Check the exact active binding before and after this read.
                reply["grant"]=await asyncio.to_thread(self.authority.inspect,message["session"])
                await self.transport.validate(self.binding,preparing=False)
            elif typ=="call":
                async with self.lock:
                    # Reconcile durable updates before processing another call.
                    await self.publish()
                    reply["result"]=await asyncio.to_thread(self.authority.call,
                                                           message["envelope"],preparing)
                    await self.publish()
                    await self.transport.validate(self.binding,preparing=preparing)
            else:
                raise Fault("Service effect admission is unavailable",503)
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

    async def serve_requests(self, ws):
        """Keep reading WebSocket control frames while one RPC publishes state."""
        calls=asyncio.Queue()
        inspections=asyncio.Queue()

        async def dispatch_requests(inbox):
            try:
                while True:
                    message=await inbox.get()
                    if message is None:
                        return
                    reply=await self.dispatch(message)
                    if not ws.closed:
                        await ws.send_json(reply)
            except Exception:
                await ws.close()
                raise

        workers=[asyncio.create_task(dispatch_requests(inbox))
                 for inbox in (calls,inspections)]
        try:
            async for event in ws:
                if event.type!=WSMsgType.TEXT:
                    if event.type in (WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR):
                        break
                    continue
                message=json.loads(event.data)
                if message.get("type")=="registered":
                    self.connected=True
                else:
                    inbox=inspections if message.get("type")=="inspect" else calls
                    inbox.put_nowait(message)
        finally:
            self.connected=False
            # Nothing queued has started. Finish the one claimed RPC so a
            # disconnect cannot interrupt its durable receipt publication.
            for inbox in (calls,inspections):
                while not inbox.empty():
                    inbox.get_nowait()
                inbox.put_nowait(None)
            await asyncio.gather(*workers)

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
                    await self.serve_requests(ws)
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
