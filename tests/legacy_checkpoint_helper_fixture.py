"""Frozen deployed relay protocol used only by the cross-repository TLS test.

Captured read only from biomerte-relay-state on 2026-11-20.
Daemon source SHA256: 52238ffa86b85451d32c564d0b0d8dfd8f6f06aab434f3a1ec72ea6f1e83c13b
Checkpoint source SHA256: b6634eff4b2ccfb4736d42f6dacce51a678aa48f4de16b73deb5aec363292ccd
The methods below are unchanged captured source, not a simulated protocol.
Storage/session/config construction is inherited from the test's current helper.
"""
import asyncio
import contextlib
import json
import logging
from urllib.parse import urlsplit

from aiohttp import WSMsgType

from biomerte.service_checkpoint import CheckpointError, binding, checkpoint_ref, same_ref
from biomerte.service_daemon import CheckpointHelper, MAX_BODY, restore_state
from biomerte.store import Fault

LOG = logging.getLogger("biomerte.legacy-checkpoint-fixture")

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


class LegacyCheckpointHelper(CheckpointHelper):
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

