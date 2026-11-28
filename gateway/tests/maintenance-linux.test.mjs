import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {maintenanceCall,maintenanceDevice} from '../lib/maintenance.mjs';
import {sqliteD1} from './sqlite-d1.mjs';

const sha=value=>createHash('sha256').update(value).digest('hex');
async function fixture(){
 const sql=new DatabaseSync(':memory:'),token='e'.repeat(64),host='pi-maintenance';
 const env={DB:sqliteD1(sql),maintenanceActor:'owner@example.invalid'};
 const send=(body,credential=token)=>maintenanceDevice(new Request('https://test.invalid/maintenance/device',{
  method:'POST',headers:{Authorization:'Bearer '+credential},body:JSON.stringify(body)}),env);
 // Initialize schema through an unauthorized device read, without a controller.
 await send({operation:'poll'});
 const enroll=(args={host,key_hash:sha(token),agent:'linux-v1'},id='enroll-pi')=>maintenanceCall(env,'maintenance.enroll',args,id);
 const snapshot={version:1,agent:'linux-v1',boot_id:'boot-one',executor:'waiting_for_durable_storage',
  interfaces:[{name:'wlan0',ipv4:'192.168.1.23'}],block_devices:[{name:'mmcblk0',counters:[1,2,3]}]};
 return {sql,token,host,env,send,enroll,snapshot};
}

test('owner-authenticated Linux enrollment works without any controller or phone tables',async()=>{
 const f=await fixture();try{
  assert.deepEqual(await f.enroll(),{id:'enroll-pi',accepted:true});
  const row=f.sql.prepare('SELECT * FROM maintenance_hosts WHERE id=?').get(f.host);
  assert.equal(row.key_hash,sha(f.token));assert.equal(row.agent,'linux-v1');
  assert.equal(row.phone_key,'');assert.equal(row.phone_device,'');
  assert.equal(f.sql.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE name='bridge_jobs'").get().n,0);
  assert.equal(JSON.stringify(f.sql.prepare('SELECT * FROM maintenance_audit').all()).includes(f.token),false);
  assert.deepEqual(await f.enroll(),{id:'enroll-pi',accepted:true});
 }finally{f.sql.close()}
});

test('Linux enrollment requires the authenticated owner independently of device credentials',async()=>{
 const f=await fixture();try{
  delete f.env.maintenanceActor;
  await assert.rejects(f.enroll(),/Authenticated owner required/);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_hosts').get().n,0);
 }finally{f.sql.close()}
});

test('read-only diagnostic heartbeat remains available without claiming any queued work',async()=>{
 const f=await fixture();try{
  await f.enroll();
  await maintenanceCall(f.env,'maintenance.submit',{host:f.host,argv:['/bin/echo','one']},'one');
  const response=await f.send({operation:'heartbeat',diagnostics:f.snapshot});
  assert.equal(response.status,200);assert.deepEqual(await response.json(),{accepted:true,host:f.host});
  const state=await maintenanceCall(f.env,'maintenance.status',{},'status');
  assert.deepEqual(state.hosts[0].diagnostics,f.snapshot);
  assert.equal(state.hosts[0].agent,'linux-v1');assert.equal(typeof state.hosts[0].seen,'number');
  assert.equal(state.jobs[0].state,'queued');
  assert.equal((await (await f.send({operation:'poll'})).json()).jobs[0].id,'one');
  assert.equal(f.sql.prepare('SELECT state FROM maintenance_jobs WHERE id=?').get('one').state,'claimed');
 }finally{f.sql.close()}
});

test('revocation disables heartbeat and cannot be bypassed by reenrolling the same identity',async()=>{
 const f=await fixture();try{
  await f.enroll();await maintenanceCall(f.env,'maintenance.revoke',{host:f.host},'revoke');
  assert.equal((await f.send({operation:'heartbeat',diagnostics:f.snapshot})).status,401);
  await assert.rejects(f.enroll(undefined,'retry'),/cannot be re-enabled/);
 }finally{f.sql.close()}
});

test('new enrollment cannot reuse another hosts credential or replace an existing identity',async()=>{
 const f=await fixture();try{
  await f.enroll();
  await assert.rejects(f.enroll({host:'second',key_hash:sha(f.token),agent:'linux-v1'},'other'),/another host/);
  await assert.rejects(f.enroll({host:f.host,key_hash:sha('f'.repeat(64)),agent:'linux-v1'},'replace'),/new host identity/);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_hosts').get().n,1);
 }finally{f.sql.close()}
});

test('concurrent credential enrollment has exactly one durable host binding',async()=>{
 const f=await fixture();try{
  const results=await Promise.allSettled([
   f.enroll({host:'first',key_hash:sha(f.token),agent:'linux-v1'},'first'),
   f.enroll({host:'second',key_hash:sha(f.token),agent:'linux-v1'},'second')]);
  assert.equal(results.filter(r=>r.status==='fulfilled').length,1);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_hosts WHERE key_hash=?').get(sha(f.token)).n,1);
 }finally{f.sql.close()}
});

test('invalid diagnostics and unauthenticated device requests do not expose or overwrite a snapshot',async()=>{
 const f=await fixture();try{
  await f.enroll();
  assert.equal((await f.send({operation:'heartbeat',diagnostics:f.snapshot})).status,200);
  assert.equal((await f.send({operation:'heartbeat',diagnostics:{...f.snapshot,argv:['/bin/false']}})).status,400);
  assert.equal((await f.send({operation:'heartbeat',diagnostics:f.snapshot},'f'.repeat(64))).status,401);
  assert.deepEqual(JSON.parse(f.sql.prepare('SELECT diagnostics FROM maintenance_hosts WHERE id=?').get(f.host).diagnostics),f.snapshot);
 }finally{f.sql.close()}
});

test('existing Mac hosts retain their status shape and command delivery protocol',async()=>{
 const f=await fixture();try{
  f.sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run('mac',sha(f.token),'key','phone','site');
  const state=await maintenanceCall(f.env,'maintenance.status',{},'status');
  assert.deepEqual(Object.keys(state.hosts[0]).sort(),['enabled','id','seen']);
  await maintenanceCall(f.env,'maintenance.submit',{host:'mac',argv:['/bin/echo','mac']},'mac-command');
  assert.equal((await (await f.send({operation:'poll'})).json()).jobs[0].id,'mac-command');
  assert.equal((await (await f.send({operation:'poll'})).json()).jobs[0].id,'mac-command');
  assert.equal((await f.send({operation:'heartbeat',diagnostics:f.snapshot})).status,403);
 }finally{f.sql.close()}
});
