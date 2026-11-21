"""Scoped physical-service authority using the existing signed Biome call core.

The database belongs only to one logical service. Host identity is never cloned.
During preparation only provider fence/recovery traffic is admitted. A new
physical generation invalidates old streams; uncertain operations retain their
identity and outcome instead of being replayed.
"""
import threading
import time

from .app import Application
from .api import Context
from .store import Fault, digest
from . import providers
from .service_checkpoint import binding

PREPARATION = frozenset(("cap.offer", "provider.poll", "cap.fenced",
                         "provider.result", "cap.status"))
CALLS = PREPARATION | frozenset((
    "provider.claim", "cap.acquire", "cap.lease", "cap.leases", "cap.release",
    "cap.invoke", "cap.submit", "stream.open", "stream.attach", "stream.list",
    "stream.inspect", "stream.close"))


class ScopedAuthority:
    def __init__(self, app, config, current_binding):
        self.app, self.config, self.binding = app, config, binding(current_binding)
        self.resources = frozenset(config["resources"])
        self.providers = frozenset(config["providers"])
        self.contracts = frozenset(config["contracts"])
        self.lock = threading.RLock()
        if not self.resources or not self.providers or not self.contracts:
            raise Fault("Explicit service resource, provider and contract scope required")
        if app.config.get("etcd") or app.config.get("gateway_journal"):
            raise Fault("Logical service state requires local durable SQLite")
        if app.node != config["authority_id"] or app.site != config["site"]:
            raise Fault("Service private identity differs from configured logical authority")
        if self.binding["service_id"] != config["service_id"]:
            raise Fault("Wrong service grant binding")
        self.marker = digest(["service-authority", self.binding])

    def snapshot(self):
        def read(tx):
            return {"records": [[kind, key, __import__("json").loads(value)]
                for kind, key, value in tx.db.execute(
                    "SELECT kind,id,value FROM records ORDER BY kind,id")],
                "authority": self.app.node, "site": self.app.site}
        with self.lock:
            return self.app.stores["site"].execute(read, read_only=True)

    def maintain(self):
        # This store contains only this service's scoped records. Run the native
        # capability lifecycle, never controller workflows or assistant pumps.
        def advance(tx):
            for kind in ("leases","actions","_stream_grants"):
                for key,value in tx.db.execute("SELECT id,value FROM records WHERE kind=?",(kind,)):
                    row=__import__("json").loads(value)
                    if row.get("resource") not in self.resources:
                        raise Fault("Lifecycle record is outside service scope",403)
            ctx=Context(self.app,scope="site")
            result=providers.settle_submissions(tx,ctx)
            self.renew_control_streams(tx,ctx)
            return result
        with self.lock:
            return self.app.stores["site"].execute(advance)

    @staticmethod
    def renew_control_streams(tx, ctx):
        """Renew the existing epoch while its persistent control lease is current."""
        from . import lifecycle, shared_streams, streams
        now=time.time()
        for grant in tx.rows("_stream_grants"):
            if (grant.get("renew_with_lease") is not True or
                    grant.get("contract")!="device.outputs.stream:1" or
                    grant.get("lanes")!=["control"] or grant.get("state")!="open" or
                    not now<grant["expires_at"]<=now+120):
                continue
            try:
                streams.valid(tx,ctx,grant)
                lease=tx.need("leases",grant["lease"])
                if (lifecycle.policy(tx,lease)["lease_class"]!="persistent" or
                        not shared_streams.consumers(tx,grant)):
                    continue
            except Fault:
                continue
            grant["expires_at"]=now+3600
            tx.put("_stream_grants",grant["id"],grant)

    def _resource_row(self, tx, kind, key):
        row = tx.need(kind, key)
        if row.get("resource") not in self.resources:
            raise Fault("Record is outside logical service scope", 403)
        return row

    def _check(self, envelope, preparing):
        if not isinstance(envelope, dict):
            raise Fault("Signed envelope required")
        name, args = envelope.get("operation"), envelope.get("arguments")
        auth = envelope.get("auth")
        if (envelope.get("site") != self.app.site or envelope.get("realm") != "site"
                or not isinstance(args, dict) or not isinstance(auth, dict)
                or not isinstance(auth.get("actor"), str)
                or not isinstance(auth.get("signature"), str)
                or not isinstance(envelope.get("id"), str) or not envelope["id"]):
            raise Fault("Exact signed site envelope required", 403)
        if set(envelope) - {"site","realm","operation","arguments","id","auth","controller"}:
            raise Fault("Unexpected signed envelope fields", 403)
        if envelope.get("controller") not in (None, self.app.node):
            raise Fault("Request addressed to another authority", 403)
        if name not in CALLS:
            raise Fault("Operation is outside logical service scope", 403)
        if preparing and (name not in PREPARATION or auth["actor"] not in self.providers):
            raise Fault("Service is preparing its physical fence", 503)
        if args.get("resource") is not None and args["resource"] not in self.resources:
            raise Fault("Resource is outside logical service scope", 403)
        if args.get("provider") is not None and args["provider"] not in self.providers:
            raise Fault("Provider is outside logical service scope", 403)
        if args.get("contract") is not None and args["contract"] not in self.contracts:
            raise Fault("Contract is outside logical service scope", 403)
        if name=="cap.offer" and args.get("capacity") not in (None,1):
            raise Fault("Scoped physical authority requires exclusive capacity one",403)
        if name in ("cap.acquire","cap.submit") and args.get("resource") not in self.resources:
            raise Fault("Exact logical resource selection is required", 403)
        def refs(tx):
            if name in ("cap.submit","cap.invoke") and args.get("operation"):
                source=tx.get("_service_source","initial") or {}
                history=source.get("former_native_records",[])
                if any(isinstance(row,(list,tuple)) and len(row)==3 and
                       row[0] in ("_submissions","actions") and row[1]==args["operation"]
                       for row in history):
                    raise Fault("Original native operation "+args["operation"]+
                        " remains with source "+str(source.get("source_controller","unknown"))+
                        "; outcome=original_source_history; replay_allowed=false",409)
            if args.get("lease"):
                self._resource_row(tx, "leases", args["lease"])
            if args.get("session"):
                self._resource_row(tx, "_stream_grants", args["session"])
            if args.get("operation") and name in (
                    "provider.claim","provider.result","cap.status"):
                self._resource_row(tx, "actions", args["operation"])
            if name == "cap.offer":
                peer = tx.need("peers", auth["actor"])
                if args.get("provider") != auth["actor"] or not peer.get("enabled"):
                    raise Fault("Provider identity mismatch", 403)
        self.app.stores["site"].execute(refs, read_only=True)
        return name, args, auth

    def call(self, envelope, preparing):
        with self.lock:
            name, args, auth = self._check(envelope, preparing)
            result = self.app.call(name, args, envelope["id"], "site", auth=auth,
                                   controller=envelope.get("controller"))
            if preparing and name == "provider.poll":
                marker = self.app.stores["site"].execute(
                    lambda tx: tx.need("_service_fences", self.marker), read_only=True)
                wanted = set(marker["leases"].values())
                result = dict(result, actions=[], activations=[], streams=[],
                              fences=[lease for lease in result.get("fences", [])
                                      if lease["id"] in wanted])
            return result

    def inspect(self, session):
        with self.lock:
            self.app.stores["site"].execute(
                lambda tx: self._resource_row(tx, "_stream_grants", session), read_only=True)
            return self.app.call("stream.inspect", {"session": session},
                                 "service-inspect-" + session, "site")

    def prepare_fences(self, prepared_checkpoint, retained_floors=None):
        """Persist intent before exposing a fence through signed provider polling."""
        def run(tx):
            prior = tx.get("_service_fences", self.marker)
            if prior:
                if prior["checkpoint"] != prepared_checkpoint:
                    raise Fault("Service fence preparation identity changed", 409)
                return prior
            leases, now = {}, time.time()
            for resource in sorted(self.resources):
                offers = [row for row in tx.rows("offers")
                          if row.get("resource") == resource
                          and row.get("provider") in self.providers
                          and row.get("contract") in self.contracts]
                contract = self.config.get("fence_contracts", {}).get(resource)
                if contract:
                    offers = [row for row in offers if row["contract"] == contract]
                if not offers:
                    raise Fault("No exact scoped provider offer for physical fence", 503)
                if any(providers.capacity_of(tx,row)!=1 for row in offers):
                    raise Fault("Scoped physical authority requires exclusive capacity one",409)
                if len({row["provider"] for row in offers})!=1:
                    raise Fault("Physical resource has multiple enforcement providers",409)
                offer = sorted(offers, key=lambda row: row["id"])[0]
                floor = max(tx.get("_fence_generations", resource, 0),
                            (retained_floors or {}).get(resource, 0))
                for kind in ("guards", "leases"):
                    for row in tx.rows(kind):
                        if row.get("resource") == resource:
                            floor = max(floor, row.get("generation", 0))
                if type(floor) is not int or not 0 <= floor < 9007199254740990:
                    raise Fault("Invalid retained physical generation", 409)
                lease_id = digest(["service-fence", self.binding, resource])
                policy = __import__("biomerte.lifecycle", fromlist=["policy"]).policy(tx, {
                    "offer": offer["id"], "provider": offer["provider"]})
                lease = dict(id=lease_id, task="service-fence-"+self.marker,
                    owner=self.app.node, contract=offer["contract"],
                    requested_resource=resource, resource=resource, placement={},
                    provider=offer["provider"], offer=offer["id"],
                    guard=resource, generation=floor+1, state="fencing",
                    lease_class=policy["lease_class"], lease_policy=policy,
                    created_at=now, fencing_since=now, last_progress_at=now,
                    missed_heartbeats=0, service_binding=self.binding)
                tx.put("guards", resource, {"generation":floor+1,"holder":lease_id,
                                            "resource":resource})
                tx.put("leases", lease_id, lease)
                leases[resource] = lease_id
            marker = dict(binding=self.binding, checkpoint=prepared_checkpoint,
                          leases=leases, phase="fencing")
            return tx.put("_service_fences", self.marker, marker)
        with self.lock:
            return self.app.stores["site"].execute(run)

    def finish_fences(self):
        """A signed cap.fenced observation proves quiescence, never prior success."""
        def run(tx):
            marker = tx.need("_service_fences", self.marker)
            if marker["binding"] != self.binding:
                raise Fault("Physical fence belongs to another service grant", 409)
            if marker["phase"] == "fenced":
                return marker["evidence"]
            fence_ids = set(marker["leases"].values())
            evidence = {}
            for resource, lease_id in marker["leases"].items():
                lease = tx.need("leases", lease_id)
                guard = tx.need("guards", resource)
                if (lease["state"] != "bound" or guard["holder"] != lease_id
                        or guard["generation"] != lease["generation"]):
                    raise Fault("Awaiting signed physical fence acknowledgement", 503)
                evidence[resource] = {"provider":lease["provider"],"lease":lease_id,
                                      "generation":lease["generation"]}
            now = time.time()
            for stream in tx.rows("_stream_grants"):
                if stream.get("resource") in self.resources and stream.get("state") == "open":
                    stream.update(state="revoked", expires_at=now,
                                  service_fence=self.marker)
                    tx.put("_stream_grants", stream["id"], stream)
            for action in tx.rows("actions"):
                if (action.get("resource") in self.resources
                        and action.get("state") in ("queued","claimed","uncertain")):
                    action.update(state="uncertain", recovery={
                        "reason":"service_owner_changed",
                        "physical_fence":evidence[action["resource"]],
                        "outcome":"unknown", "replay_allowed":False,
                        "observed_at":now})
                    tx.put("actions", action["id"], action)
            for lease in tx.rows("leases"):
                if (lease.get("resource") in self.resources and lease["id"] not in fence_ids
                        and lease.get("state") in ("bound","fencing","queued")):
                    if lease.get("queue_key"):
                        tx.drop("_lease_queue",lease.pop("queue_key"))
                    lease.update(state="released", released_at=now,
                                 physical_fence=evidence[lease["resource"]],
                                 outcome_preserved=True)
                    tx.put("leases",lease["id"],lease)
            for lease_id in fence_ids:
                if not providers.release(tx,tx.need("leases",lease_id)):
                    raise Fault("Service fence unexpectedly owns an operation",409)
            result = {"fenced":True,"resources":evidence,
                      "binding":self.binding,"checkpoint":marker["checkpoint"],
                      "boot_epoch":self.binding["boot_epoch"],
                      "grant_token":self.binding["grant_token"]}
            marker.update(phase="fenced",evidence=result)
            tx.put("_service_fences",self.marker,marker)
            return result
        with self.lock:
            return self.app.stores["site"].execute(run)
