"""Reviewed destination-only operator enrollment; no source or live host writes."""
import copy
import json
import pathlib
import tempfile
import unittest

from tests import test_controller_state_migration as base
from biomerte.app import Application, initialize
from biomerte.identity import fingerprint, generate, sign
from biomerte.store import Fault

migration = base.migration


class OperatorEnrollmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='operator-key-fixture-')
        cls.key = pathlib.Path(cls.keys.name) / 'operator.key'
        cls.public = generate(str(cls.key))
        cls.actor = fingerprint(cls.public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.sources = [
            base.snapshot(base.PI, [
                ['_meta','events',0],
                ['assistant_responsibilities','worker',{'desired':'pause'}],
                ['work','pi-task',{'state':'open'}]]),
            base.snapshot(base.MAC, [
                ['_meta','events',0],['work','mac-task',{'state':'open'}]])]
        self.entry = {'actor':self.actor,'public_key':self.public,
                      'label':'Site controller operator proxy','actor_alias':'admin'}
        self.policy = {'operator_enrollments':[copy.deepcopy(self.entry)]}

    def plan(self, policy=None, target=None):
        return migration.make_plan(self.sources,base.PI,target or base.TARGET,
            'operator-migration',self.policy if policy is None else policy)

    def export(self, plan=None, target=None):
        target = target or base.TARGET
        plan = plan or self.plan(target=target)
        markers = [migration.freeze_marker(s,target,'operator-migration')
                   for s in self.sources]
        return migration.export_state(self.sources,plan,markers)

    def put(self, source, kind, key, value):
        source['records'] = [r for r in source['records'] if r[:2] != [kind,key]]
        source['records'].append([kind,key,copy.deepcopy(value)])
        source['records'].sort(key=lambda row:tuple(row[:2]))

    def config(self, directory):
        directory = pathlib.Path(directory)
        key = directory / 'checkpoint.key'
        key.write_text('k'*48)
        return {'authority_kind':'controller','service_id':base.TARGET['service_id'],
            'authority_id':base.AUTH,'site':base.TARGET['site'],
            'controller_aliases':[base.PI,base.MAC],
            'operator_actors':[self.actor],
            'operator_actor_aliases':{self.actor:'admin'},
            'checkpoint_key_file':str(key),
            'checkpoint_db':str(directory/'checkpoint.sqlite')}

    def assert_no_checkpoint(self, config):
        self.assertFalse(pathlib.Path(config['checkpoint_db']).exists())
        self.assertFalse(pathlib.Path(config['checkpoint_db']+'.chunks').exists())

    def test_grant_is_reviewed_deterministic_and_destination_only(self):
        before = copy.deepcopy(self.sources)
        plan = self.plan()
        self.assertTrue(plan['ready'],plan['blocked'])
        self.assertEqual(plan,self.plan())
        self.assertEqual(plan['operator_enrollments'],[{
            'actor':self.actor,'actor_alias':'admin','realm':'site',
            'grant':'controller_operator','peer_digest':migration.digest(migration.operator_peer(self.entry))}])
        exported = self.export(plan)
        self.assertEqual(base.records(exported)[('peers',self.actor)],
                         migration.operator_peer(self.entry))
        self.assertEqual(exported['authorization']['source_receipt']['operator_enrollments'],
                         self.policy['operator_enrollments'])
        self.assertEqual(base.records(exported)[('_controller_migrations','operator-migration')]['policy'],
                         self.policy)
        self.assertEqual(base.records(exported)[('assistant_responsibilities','worker')]['desired'],'pause')
        self.assertEqual(self.sources,before)

    def test_invalid_or_private_declarations_are_rejected(self):
        variants = [
            ('fingerprint',dict(self.entry,actor='f'*64)),
            ('alias',dict(self.entry,actor_alias='other')),
            ('unknown-field',dict(self.entry,scopes=['peer.trust'])),
            ('private-only',dict(self.entry,public_key=self.key.read_text())),
            ('private-appended',dict(self.entry,public_key=self.public+self.key.read_text())),
            ('second-public',dict(self.entry,public_key=self.public+self.public)),
            ('trailing-text',dict(self.entry,public_key=self.public+'unreviewed')),
            ('empty-label',dict(self.entry,label=''))]
        for name,entry in variants:
            with self.subTest(case=name):
                with self.assertRaises(migration.MigrationError):
                    self.plan({'operator_enrollments':[entry]})
        with self.assertRaises(migration.MigrationError):
            self.plan({'operator_enrollments':[self.entry,self.entry]})

    def test_any_existing_or_revoked_source_peer_collision_is_rejected_before_resolution(self):
        original = copy.deepcopy(self.sources)
        for source_index in (0,1):
            for enabled in (True,False):
                self.sources = copy.deepcopy(original)
                peer = dict(migration.operator_peer(self.entry),enabled=enabled)
                self.put(self.sources[source_index],'peers',self.actor,peer)
                with self.subTest(source=source_index,enabled=enabled):
                    with self.assertRaises(migration.MigrationError):
                        self.plan()
        self.sources = copy.deepcopy(original)
        self.put(self.sources[0],'peers',self.actor,dict(migration.operator_peer(self.entry),enabled=False))
        self.put(self.sources[1],'peers',self.actor,migration.operator_peer(self.entry))
        policy = dict(self.policy,resolutions=[{'kind':'peers','id':self.actor,'source':base.MAC}])
        with self.assertRaises(migration.MigrationError):
            self.plan(policy)

    def test_policy_or_reviewed_plan_change_is_not_silently_accepted(self):
        plan = self.plan()
        changed = copy.deepcopy(plan)
        changed['policy']['operator_enrollments'][0]['label']='changed after review'
        with self.assertRaises(migration.MigrationError):
            self.export(changed)
        changed = copy.deepcopy(plan)
        changed['operator_enrollments'][0]['actor_alias']='changed'
        with self.assertRaises(migration.MigrationError):
            self.export(changed)

    def test_exact_operator_scope_bootstraps_a_new_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            exported = self.export()
            packet = migration.bootstrap_controller(exported,config)
            self.assertTrue(pathlib.Path(config['checkpoint_db']).exists())
            self.assertEqual(packet['payload']['state']['authority'],base.AUTH)
            self.assertEqual(packet['payload']['authorization']['source_receipt']['operator_enrollments'],
                             self.policy['operator_enrollments'])

    def test_changed_operator_actors_or_admin_aliases_fail_before_checkpoint_creation(self):
        exported = self.export()
        variants = [
            {'operator_actors':[]},
            {'operator_actors':[self.actor,self.actor]},
            {'operator_actors':[self.actor,'d'*64]},
            {'operator_actor_aliases':{}},
            {'operator_actor_aliases':{self.actor:self.actor}},
            {'operator_actor_aliases':{self.actor:'admin','d'*64:'admin'}}]
        for index,delta in enumerate(variants):
            with self.subTest(case=index),tempfile.TemporaryDirectory() as directory:
                config = dict(self.config(directory),**delta)
                with self.assertRaises(migration.MigrationError):
                    migration.bootstrap_controller(exported,config)
                self.assert_no_checkpoint(config)

    def test_inherited_peer_cannot_be_elevated_without_reviewed_declaration(self):
        inherited = 'd'*64
        self.put(self.sources[0],'peers',inherited,
                 {'id':inherited,'key':self.public,'label':'inherited','enabled':True,'scopes':[]})
        exported = self.export()
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            config['operator_actors'].append(inherited)
            config['operator_actor_aliases'][inherited]='admin'
            with self.assertRaises(migration.MigrationError):
                migration.bootstrap_controller(exported,config)
            self.assert_no_checkpoint(config)

    def test_changed_export_peer_or_receipt_cannot_create_checkpoint_artifacts(self):
        original = self.export()
        for mode in ('state-digest','peer-row','declaration','provenance','duplicate-row'):
            exported = copy.deepcopy(original)
            receipt = exported['authorization']['source_receipt']
            if mode == 'state-digest':
                receipt['state_digest']='f'*64
            elif mode == 'peer-row':
                next(row[2] for row in exported['state']['records']
                     if row[:2]==['peers',self.actor])['enabled']=False
                receipt['state_digest']=migration.digest(exported['state'])
            elif mode == 'declaration':
                receipt['operator_enrollments'][0]['label']='unreviewed'
            elif mode == 'provenance':
                next(row[2] for row in exported['state']['records']
                     if row[:2]==['_controller_migrations','operator-migration'])['policy']={}
                receipt['state_digest']=migration.digest(exported['state'])
            else:
                exported['state']['records'].append(copy.deepcopy(exported['state']['records'][-1]))
                receipt['state_digest']=migration.digest(exported['state'])
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                config = self.config(directory)
                with self.assertRaises(migration.MigrationError):
                    migration.bootstrap_controller(exported,config)
                self.assert_no_checkpoint(config)

    def test_real_signed_operator_call_uses_exported_peer_and_revocation_still_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            node = initialize(directory,site=base.TARGET['site'])
            target = dict(base.TARGET,authority_id=node['node'])
            exported = self.export(target=target)
            config = {'authority_kind':'controller','service_id':target['service_id'],
                'authority_id':node['node'],'site':node['site'],
                'controller_aliases':target['controller_aliases'],
                'operator_actors':[self.actor],'operator_actor_aliases':{self.actor:'admin'}}
            app = Application(directory,controller_runtime=config)
            try:
                def install(tx):
                    for kind,key,value in exported['state']['records']:
                        tx.put(kind,key,value)
                app.stores['site'].execute(install)
                def call(operation,arguments,request_id):
                    envelope = {'site':node['site'],'realm':'site','operation':operation,
                                'arguments':arguments,'id':request_id}
                    auth = {'actor':self.actor,'signature':sign(str(self.key),envelope)}
                    return app.call(operation,arguments,request_id,'site',auth=auth)
                result = call('knowledge.put',{'key':'operator-fixture','body':{'works':True}},'operator-write')
                self.assertIn('rev',result)
                self.assertFalse(call('peer.revoke',{'peer':self.actor},'operator-revoke')['enabled'])
                with self.assertRaises(Fault) as revoked:
                    call('knowledge.put',{'key':'blocked','body':{}},'operator-after-revoke')
                self.assertEqual(revoked.exception.status,403)
            finally:
                for store in app.stores.values():
                    store.close()


if __name__ == '__main__':
    unittest.main()
