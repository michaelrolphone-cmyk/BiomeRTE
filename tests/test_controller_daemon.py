"""Publication failure must not expose commands, results or pairing state."""
import asyncio
import json
import os
import tempfile
import types
import unittest

try:
    from biomerte.service_daemon import AuthorityDaemon, scope_digest
except ModuleNotFoundError as error:
    raise unittest.SkipTest("Controller daemon boundary tests require aiohttp") from error

from biomerte.app import Application, initialize
from biomerte.controller_authority import ControllerAuthority
from biomerte.identity import generate, fingerprint, sign
from biomerte.service_checkpoint import digest as checkpoint_digest
from biomerte.store import Fault

B = dict(service_id="site-controller",manifest_revision="revision",worker_id="mac",
         worker_incarnation="mac-1",boot_epoch="boot",grant_token="grant")


class ControllerDaemonTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory()
        cls.key=os.path.join(cls.keys.name,"proxy.key")
        cls.public=generate(cls.key)
        cls.actor=fingerprint(cls.public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conf=initialize(self.tmp.name,"controller-boundary")
        self.config=dict(authority_kind="controller",service_id=B["service_id"],
            authority_id=conf["node"],site=conf["site"],
            controller_aliases=["physical-pi","physical-mac"],
            operator_actors=[self.actor],operator_actor_aliases={self.actor:"admin"})
        self.app=Application(self.tmp.name,controller_runtime=self.config)
        self.addCleanup(self.close)
        self.authority=ControllerAuthority(self.app,self.config,B)
        self.app.stores["site"].execute(lambda tx: tx.put("peers",self.actor,
            dict(id=self.actor,key=self.public,enabled=True,scopes=[])))
        self.daemon=AuthorityDaemon.__new__(AuthorityDaemon)
        self.daemon.config=self.config
        self.daemon.binding=B
        self.daemon.app=self.app
        self.daemon.authority=self.authority
        self.daemon.lock=asyncio.Lock()
        self.published=0
        self.validated=0
        self.fail_publish=0
        self.fail_validate=0
        self.current_phase="active"
        async def validate(binding,preparing=True):
            self.validated+=1
            if self.validated==self.fail_validate:
                raise Fault("Grant revoked",503)
            return {"valid":True}
        async def publish():
            self.published+=1
            if self.published==self.fail_publish:
                raise Fault("Missing replica acknowledgement",503)
            return {"checkpoint":"fixture"}
        async def phase():
            # The real private adapter validates the binding before returning
            # its phase. Revocation on the final fence must still hide a result.
            await validate(B)
            return {"phase":self.current_phase}
        self.daemon.transport=types.SimpleNamespace(validate=validate)
        self.daemon.publish=publish
        self.daemon.phase=phase

    def close(self):
        for store in self.app.stores.values():
            store.close()

    def message(self,key="boundary",request="request-1"):
        body=dict(site=self.config["site"],realm="site",controller="physical-pi",
            operation="knowledge.put",arguments={"key":key,"body":{"value":1}},id=request)
        envelope=dict(body,auth=dict(actor=self.actor,signature=sign(self.key,body)))
        return dict(type="call",id="rpc-1",binding=B,envelope=envelope)

    def document(self,key="boundary"):
        return self.app.stores["site"].execute(lambda tx: tx.get("documents",key),read_only=True)

    async def test_no_new_transaction_before_previous_state_is_published(self):
        self.fail_publish=1
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertNotIn("result",result)
        self.assertIsNone(self.document())

    async def test_lost_post_commit_ack_hides_result_and_same_id_replays_once(self):
        self.fail_publish=2
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertNotIn("result",result)
        committed=self.document()
        self.assertIsNotNone(committed)
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],200)
        self.assertEqual(self.document(),committed)
        self.assertEqual(len(self.document()["heads"]),1)

    async def test_revoked_grant_cannot_expose_committed_response(self):
        self.fail_validate=2
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertNotIn("result",result)
        self.assertIsNotNone(self.document())
        self.assertEqual(self.published,2)

    async def test_preparation_never_admits_site_commands(self):
        self.current_phase="prepared"
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertIsNone(self.document())

    async def test_pairing_is_public_pending_only_and_published_before_reply(self):
        message=dict(type="pair",id="pair-1",binding=B,operation="begin",
                     payload={"public_key":self.public,"label":"pending device"})
        response=await self.daemon.dispatch(message)
        self.assertEqual(response["type"],"pair_result")
        self.assertEqual(response["status"],200)
        row=self.app.stores["site"].execute(
            lambda tx: tx.need("candidates",self.actor),read_only=True)
        self.assertEqual(row["state"],"pending")
        rejected=await self.daemon.dispatch(dict(message,operation="approve"))
        self.assertEqual(rejected["status"],403)
        self.fail_publish=self.published+2
        failed=await self.daemon.dispatch(message)
        self.assertEqual(failed["status"],503)
        self.assertNotIn("result",failed)

    async def test_maintenance_does_not_tick_after_failed_publication(self):
        self.fail_publish=1
        before=self.authority.snapshot()
        with self.assertRaises(Fault):
            await self.daemon.maintain_once()
        self.assertEqual(self.authority.snapshot(),before)

    def test_scope_digest_preserves_physical_scope_and_binds_operator_mapping(self):
        physical=dict(service_id="physical",authority_id="id",site="site",
                      resources=["relay"],providers=["provider"],contracts=["relay:1"])
        self.assertEqual(scope_digest(physical),checkpoint_digest(physical))
        self.assertNotEqual(scope_digest(self.config),
            scope_digest(dict(self.config,controller_aliases=["different"])))
        self.assertNotEqual(scope_digest(self.config),
            scope_digest(dict(self.config,operator_actor_aliases={})))
        with self.assertRaises(ValueError):
            scope_digest(dict(self.config,authority_kind="unexpected"))


if __name__=="__main__":
    unittest.main()
