"""Managed MCP/Grok event state, preserving native producer identities and cursors.

The existing delivery implementation uses per-producer SQL projections. Triggers
mirror each changed row into the canonical records table in the same transaction.
Only that canonical representation is checkpointed; projections are disposable.
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler

from . import mcp_events, grok_receivers, process
from .api import Context, event_page, op
from .store import Fault, digest, encode
from .identity import verify

EVENT_KINDS = {
    "subscriptions": "_gateway_event_subscriptions",
    "receiver_receipts": "_gateway_event_receipts",
    "failed_events": "_gateway_event_failures",
}
COLUMNS = {
    "subscriptions": ("id","owner","grant_id","url","args","key","old_key","rotate_until",
                      "verified","expires","cursor","failures","next_try","generation","last_error"),
    "receiver_receipts": ("id","signature","result"),
    "failed_events": ("sub","event","status"),
}
PRIMARY = {"subscriptions": ("id",), "receiver_receipts": ("id",),
           "failed_events": ("sub","event")}
OPERATIONS = frozenset(("gateway.events.change","gateway.events.grok"))


class Projection:
    """Rename only trusted SQL table identifiers; parameter values are untouched."""
    def __init__(self, path, controller):
        self.raw = sqlite3.connect(path, timeout=12, check_same_thread=False)
        self.raw.execute("PRAGMA synchronous=FULL")
        self.prefix = "controller_events_" + hashlib.sha256(controller.encode()).hexdigest()
        self.tables = {name: self.prefix+"_"+name for name in EVENT_KINDS}
        self.deferred = False

    @property
    def row_factory(self):
        return self.raw.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.raw.row_factory = value

    def sql(self, sql):
        return re.sub(r"\b(subscriptions|receiver_receipts|failed_events)\b",
                      lambda match: self.tables[match.group(1)], sql)

    def execute(self, sql, parameters=()):
        return self.raw.execute(self.sql(sql), parameters)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        if kind is not None:
            self.raw.rollback()
        elif not self.deferred:
            self.raw.commit()
        return False

    def close(self):
        self.raw.close()

    def rebuild(self, controller):
        self.execute("CREATE TABLE IF NOT EXISTS failed_events "
                     "(sub TEXT,event TEXT,status INTEGER,PRIMARY KEY(sub,event))")
        # No mirror trigger may fire while rebuilding this disposable projection.
        for table in EVENT_KINDS:
            for event in ("insert","update","delete"):
                self.raw.execute("DROP TRIGGER IF EXISTS "+self.tables[table]+"_"+event)
        for table, kind in EVENT_KINDS.items():
            self.execute("DELETE FROM "+table)
            columns = COLUMNS[table]
            for (raw,) in self.raw.execute("SELECT value FROM records WHERE kind=?", (kind,)):
                saved = json.loads(raw)
                if saved["source_controller"] != controller:
                    continue
                record = saved["record"]
                if table == "subscriptions" and json.loads(record["args"]).get("realm","site") != "site":
                    raise Fault("Managed event projection contains a local host subscription",409)
                self.execute("INSERT INTO "+table+"("+",".join(columns)+") VALUES("+
                             ",".join("?" for unused in columns)+")",
                             tuple(record.get(column) for column in columns))
            key_function = self.tables[table]+"_key"
            value_function = self.tables[table]+"_value"
            self.raw.create_function(key_function, -1,
                lambda *values, controller=controller: digest([controller]+list(values)))
            self.raw.create_function(value_function, -1,
                lambda *values, columns=columns, controller=controller: encode(
                    {"source_controller":controller,"record":dict(zip(columns,values))}))
            def key(prefix):
                return key_function+"("+",".join(prefix+"."+key for key in PRIMARY[table])+")"
            def value(prefix):
                return value_function+"("+",".join(prefix+"."+key for key in columns)+")"
            for event in ("insert","update","delete"):
                statements = []
                if event in ("update","delete"):
                    statements.append("DELETE FROM records WHERE kind='"+kind+"' AND id="+key("OLD")+";")
                if event in ("insert","update"):
                    statements.append("INSERT OR REPLACE INTO records VALUES('"+kind+"',"+
                                      key("NEW")+","+value("NEW")+");")
                self.raw.execute("CREATE TRIGGER "+self.tables[table]+"_"+event+
                    " AFTER "+event.upper()+" ON "+self.tables[table]+" BEGIN "+
                    "".join(statements)+" END")
        self.raw.commit()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


class ManagedEvents:
    def __init__(self, daemon, post=None, authorize=None, barrier=None):
        self.daemon, self.app, self.config = daemon, daemon.app, daemon.config
        self.controllers = set(self.app.controller_aliases)
        default = self.config.get("event_default_controller")
        if default not in self.controllers:
            raise Fault("Explicit event default controller alias required",409)
        self.default = default
        self.loop = None if barrier else asyncio.get_running_loop()
        self._barrier_override = barrier
        self._post = post or mcp_events.post_callback
        self._authorize_override = authorize
        self.stores = {}
        self.delivery_offset = 0
        for controller in sorted(self.controllers):
            connection = Projection(self.app.stores["site"].path, controller)
            store = mcp_events.EventStore(None,
                lambda realm,after,limit,c=controller: self.feed(c,realm,None,after,limit),
                self.authorize,
                post=lambda url,headers,body,c=connection: self.post(c,url,headers,body),
                topic_feed=lambda realm,topic,after,limit,c=controller:
                    self.feed(c,realm,topic,after,limit),
                controller=controller, connection=connection,
                realm_allowed=lambda realm: realm=="site")
            connection.rebuild(controller)
            self.stores[controller] = store

    def close(self):
        for store in self.stores.values():
            store.close()

    def barrier(self, publish=True):
        if self._barrier_override:
            return self._barrier_override(publish)
        async def check():
            if publish:
                await self.daemon.publish()
            await self.daemon.transport.validate(self.daemon.binding,preparing=False)
        return asyncio.run_coroutine_threadsafe(check(),self.loop).result(
            timeout=self.config.get("request_timeout_s",15)*4)

    def authorize(self, owner, grant):
        self.barrier(False)
        if self._authorize_override:
            return self._authorize_override(owner,grant)
        url = self.config.get("event_authorize_url","")
        if not url.startswith("https://"):
            raise Fault("HTTPS event authorization endpoint required",503)
        key = Path(self.config["event_authorize_key_file"]).read_text().strip()
        if len(key)<32:
            raise Fault("Missing private event authorization credential",503)
        headers = {"Authorization":"Bearer "+key,"Content-Type":"application/json"}
        access = self.config.get("event_site_access_file")
        if access:
            value = json.loads(Path(access).read_text())
            headers["OAI-Sites-Authorization"] = "Bearer "+value["token"]
        request = Request(url,data=encode({"owner":owner,"grant":grant}).encode(),headers=headers)
        with build_opener(NoRedirect).open(request,timeout=10) as response:
            result = json.loads(response.read(8192))
        return result.get("allowed") is True

    def post(self, connection, url, headers, body):
        # Verification can occur within a change operation. Publish only committed
        # state; the complete change and its request receipt commit together later.
        self.barrier(not connection.raw.in_transaction)
        return self._post(url,headers,body)

    def feed(self, controller, realm, topic, after, limit):
        if realm!="site" or after<0 or limit<0:
            raise Fault("Managed event feed requires a valid site cursor")
        kind = "events" if topic else "delegation_events"
        legacy_kind = "_controller_source_"+kind
        def read(tx):
            source = tx.get("_controller_source_feeds",controller)
            lineage = source[kind] if source else {"high":0,"logical_high":0,"floor":0}
            old_high, barrier = lineage["high"], lineage["logical_high"]
            current = tx.get("_meta","events" if topic else "delegation_sequence",0)
            high = old_high+max(0,current-barrier)
            page = {"events":[],"high":high,"scanned":after}
            if limit==0:
                return page
            if after<lineage.get("floor",0):
                raise Fault("Original producer event cursor was retired; explicit reconciliation required",410)
            remaining = min(limit,64)
            if after<old_high:
                rows = list(tx.db.execute("SELECT value FROM records WHERE kind=? AND id>? "
                    "AND id<=? ORDER BY id LIMIT ?",(legacy_kind,
                    controller+":"+"%020d"%after,controller+":"+"%020d"%old_high,remaining)))
                for (raw,) in rows:
                    event = json.loads(raw)
                    page["scanned"] = event["seq"]
                    if topic:
                        if event["topic"]!=topic:
                            continue
                        item={"seq":event["seq"],"id":event["id"],"topic":topic,
                              "data":event["payload"] if len(raw.encode())<=8192 else {"payload_omitted":True}}
                    else:
                        item=event
                    page["events"].append(item)
                remaining-=len(rows)
                if not remaining:
                    return page
                page["scanned"]=old_high
            native_after = barrier+max(0,after-old_high)
            native = (event_page(tx,Context(self.app),topic,native_after,remaining) if topic else
                      process.delegation_feed(tx,Context(self.app),native_after,remaining))
            for event in native["events"]:
                projected=dict(event,seq=old_high+event["seq"]-barrier)
                page["events"].append(projected)
            scanned=native.get("scanned",
                native["events"][-1]["seq"] if native["events"] else native_after)
            page["scanned"]=max(page["scanned"],old_high+scanned-barrier)
            return page
        return self.app.stores["site"].execute(read,read_only=True)

    def call(self, envelope):
        name,args,auth=envelope["operation"],envelope["arguments"],envelope["auth"]
        if name not in OPERATIONS:
            raise Fault("Unknown managed event operation",404)
        controller=args.get("controller") or self.default
        if controller not in self.controllers:
            raise Fault("Event producer is outside managed controller aliases",403)
        expected = ({"controller","method","params","owner","grant"} if name.endswith(".change")
                    else {"controller","action","args","owner","grant"})
        if set(args)!=expected:
            raise Fault("Exact managed event operation arguments required")
        if not isinstance(args["owner"],str) or not args["owner"] or args["grant"] is not None and not isinstance(args["grant"],str):
            raise Fault("Authenticated event owner and grant required",403)
        # The outer native signature is verified without modifying its controller.
        def authenticate(tx):
            peer=tx.need("peers",auth["actor"])
            if not peer.get("enabled") or peer["id"] not in self.app.controller_operator_actors:
                raise Fault("Managed event operation requires the configured signed operator",403)
            signed={key:envelope[key] for key in ("site","realm","operation","arguments","id")}
            if "controller" in envelope:
                signed["controller"]=envelope["controller"]
            verify(peer["key"],auth["signature"],signed)
            return self.app.controller_actor_aliases.get(peer["id"],peer["id"])
        actor=self.app.stores["site"].execute(authenticate,read_only=True)
        request_id=envelope["id"]
        if not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",request_id):
            raise Fault("Managed event request requires a stable native request ID")
        key,signature=digest([actor,request_id]),digest([name,args])
        store=self.stores[controller];db=store.db
        conflict=db.raw.execute("SELECT 1 FROM records WHERE kind=? AND id=?",
                                ("_controller_receipt_conflicts",key)).fetchone()
        if conflict:
            raise Fault("Conflicting controller receipt history; replay_allowed=false",409)
        saved=db.raw.execute("SELECT value FROM records WHERE kind='_receipt' AND id=?",(key,)).fetchone()
        if saved:
            receipt=json.loads(saved[0])
            if receipt["signature"]!=signature:
                raise Fault("Request ID reused with different arguments",409)
            if receipt.get("result_expired"):
                raise Fault("Receipt retired; request will not execute again",410)
            return receipt["result"]
        db.deferred=True
        try:
            if name.endswith(".change"):
                result=store.change(args["method"],args["params"],args["owner"],args["grant"])
            else:
                result=grok_receivers.mutate(store,args["action"],args["args"],args["owner"],args["grant"])
            receipt=dict(signature=signature,result=result,format=2,operation=name,
                         created_at=time.time(),authenticated_actor=auth["actor"])
            db.raw.execute("INSERT OR REPLACE INTO records VALUES('_receipt',?,?)",(key,encode(receipt)))
            self.barrier(False)
            db.raw.commit()
            return result
        except mcp_events.CallbackError as error:
            db.raw.rollback()
            raise Fault("Webhook callback verification failed",503) from error
        except (ValueError,KeyError,TypeError) as error:
            db.raw.rollback()
            raise Fault("Invalid managed event registration arguments",400) from error
        except BaseException:
            db.raw.rollback()
            raise
        finally:
            db.deferred=False

    def deliver(self):
        controllers=sorted(self.stores)
        controllers=controllers[self.delivery_offset:]+controllers[:self.delivery_offset]
        for controller in controllers:
            store=self.stores[controller]
            if store.active() and store.deliver(max_callbacks=1):
                self.delivery_offset=(sorted(self.stores).index(controller)+1)%len(self.stores)
                return 1
        return 0


@op('gateway.events.change','controller:str method:str params:obj owner:str grant:any')
def event_operation(tx,ctx,controller,method,params,owner,grant):
    """Manage a signed operator's site webhook through the active controller authority."""
    raise Fault('Event changes require the managed controller publication boundary',409)


@op('gateway.events.grok','controller:str action:str args:obj owner:str grant:any')
def grok_operation(tx,ctx,controller,action,args,owner,grant):
    """Manage a signed operator's site Grok receiver through the active controller authority."""
    raise Fault('Receiver changes require the managed controller publication boundary',409)
