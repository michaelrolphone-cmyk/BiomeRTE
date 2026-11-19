import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {sqliteD1} from './sqlite-d1.mjs';
import {maintenanceCall,maintenanceDevice,maintenanceHistoryPlan,maintenanceHistoryApply,maintenanceResultRetention} from '../lib/maintenance.mjs';
const sha=value=>createHash('sha256').update(value).digest('hex');
async function fixture(){
 const sql=new DatabaseSync(':memory:'),token='d'.repeat(64),host='mac',env={DB:sqliteD1(sql),maintenanceActor:'owner',MAINTENANCE_RESULT_MAX_COUNT:0,MAINTENANCE_REQUEST_MAX_COUNT:0};
 const send=body=>maintenanceDevice(new Request('https://test.invalid/maintenance/device',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(body)}),env);
 await send({operation:'poll'});
 sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run(host,sha(token),'key','phone','site');
 const input=id=>({host,argv:['/bin/echo',id]});
 const legacy=async(id='old',completed=2)=>{
  await maintenanceCall(env,'maintenance.submit',input(id),id);
  const result={state:'succeeded',stdout:'historical-'+id};
  sql.prepare("UPDATE maintenance_jobs SET state='succeeded',result=?,created=1,updated=?,completed=NULL,retention_version=NULL,result_digest=NULL,result_bytes=NULL,request_digest=NULL,request_bytes=NULL,approval_digest=NULL,execution_digest=NULL WHERE id=?").run(JSON.stringify(result),completed,id);
  sql.prepare('UPDATE maintenance_audit SET retention_version=NULL,body_digest=NULL,body_bytes=NULL WHERE id=?').run(id);
  return result;
 };
 return {sql,env,host,send,legacy,input};
}

test('history plan is payload-read-only and enrollment preserves original age and replay fences',async()=>{
 const f=await fixture();try{
  const result=await f.legacy();
  const before=f.sql.prepare('SELECT * FROM maintenance_jobs').all(),audit=f.sql.prepare('SELECT * FROM maintenance_audit').all();
  const saved=await maintenanceHistoryPlan(f.env,{host:f.host},100);
  assert.deepEqual(f.sql.prepare('SELECT * FROM maintenance_jobs').all(),before);assert.deepEqual(f.sql.prepare('SELECT * FROM maintenance_audit').all(),audit);
  assert.equal(saved.plan.records[0].completed,2);
  assert.deepEqual((await maintenanceHistoryApply(f.env,saved.plan,saved.plan_sha256,100)).enrolled,['old']);
  assert.equal(f.sql.prepare("SELECT completed FROM maintenance_jobs WHERE id='old'").get().completed,2);
  assert.equal(f.sql.prepare("SELECT request_retired FROM maintenance_jobs WHERE id='old'").get().request_retired,0);
  await maintenanceResultRetention(f.env,f.host,100,true);
  const row=f.sql.prepare("SELECT * FROM maintenance_jobs WHERE id='old'").get();assert.equal(row.request_retired,1);assert.equal(row.result_retired,1);
  const repeat=await maintenanceHistoryApply(f.env,saved.plan,saved.plan_sha256,100);assert.deepEqual(repeat.already_enrolled,['old']);
  const receipt=await (await f.send({operation:'complete',id:'old',result})).json();assert.equal(receipt.accepted,true);assert.equal(receipt.result_sha256,row.result_digest);
  assert.equal((await maintenanceCall(f.env,'maintenance.submit',f.input('old'),'old')).state,'succeeded');
  await assert.rejects(maintenanceCall(f.env,'maintenance.submit',{...f.input('old'),argv:['/bin/false']},'old'),/different command/);
  const history=await (await f.send({operation:'retention_history',ids:['old']})).json();assert.equal(history.records[0].execution_sha256,row.execution_digest);
 }finally{f.sql.close()}
});

test('historical migration protects active uncertain authority-dependent and corrupt records',async()=>{
 const f=await fixture();try{
  for(const id of ['active','uncertain','signed','bad-age','bad-result','bad-audit'])await f.legacy(id);
  f.sql.exec("UPDATE maintenance_jobs SET state='claimed' WHERE id='active';UPDATE maintenance_jobs SET state='uncertain' WHERE id='uncertain';UPDATE maintenance_jobs SET confirmation='{}' WHERE id='signed';UPDATE maintenance_jobs SET updated=0 WHERE id='bad-age';UPDATE maintenance_jobs SET result_digest='wrong' WHERE id='bad-result';UPDATE maintenance_audit SET body='{}' WHERE id='bad-audit'");
  const saved=await maintenanceHistoryPlan(f.env,{host:f.host},100);assert.equal(saved.plan.records.length,0);assert.equal(saved.plan.blocked.length,6);
  assert.equal((await maintenanceHistoryApply(f.env,saved.plan,saved.plan_sha256,100)).enrolled.length,0);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs WHERE retention_version IS NOT NULL').get().n,0);
 }finally{f.sql.close()}
});

test('stale audited preimages and action-time owner authorization are enforced',async()=>{
 const f=await fixture();try{
  await f.legacy();const saved=await maintenanceHistoryPlan(f.env,{host:f.host},100);
  f.sql.prepare("UPDATE maintenance_audit SET body=? WHERE id='old'").run(JSON.stringify({...f.input('old'),extra:'changed'}));
  const result=await maintenanceHistoryApply(f.env,saved.plan,saved.plan_sha256,100);assert.deepEqual(result.enrolled,[]);assert.equal(result.blocked.length,1);
  await assert.rejects(maintenanceHistoryApply(f.env,saved.plan,'wrong',100),/digest/);
  delete f.env.maintenanceActor;
  await assert.rejects(maintenanceCall(f.env,'maintenance.retention.apply',{plan:saved.plan,expected_sha256:saved.plan_sha256},'apply'),/Authenticated owner/);
 }finally{f.sql.close()}
});

test('migration batch rolls job and audit enrollment back together on interruption',async()=>{
 const f=await fixture();try{
  await f.legacy();const saved=await maintenanceHistoryPlan(f.env,{host:f.host},100);
  f.sql.exec("CREATE TRIGGER fail_history BEFORE UPDATE OF retention_version ON maintenance_audit BEGIN SELECT RAISE(ABORT,'crash'); END");
  await assert.rejects(maintenanceHistoryApply(f.env,saved.plan,saved.plan_sha256,100),/crash/);
  assert.equal(f.sql.prepare("SELECT retention_version FROM maintenance_jobs WHERE id='old'").get().retention_version,null);
  f.sql.exec('DROP TRIGGER fail_history');assert.deepEqual((await maintenanceHistoryApply(f.env,saved.plan,saved.plan_sha256,100)).enrolled,['old']);
 }finally{f.sql.close()}
});

test('history enumeration is bounded keyset indexed and device evidence is host-scoped',async()=>{
 const f=await fixture();try{
  for(let i=0;i<5;i++)await f.legacy('old-'+i,i+2);
  await f.legacy('foreign');f.sql.exec("UPDATE maintenance_jobs SET host='other' WHERE id='foreign'");
  const first=await maintenanceHistoryPlan(f.env,{host:f.host,limit:2},100),second=await maintenanceHistoryPlan(f.env,{host:f.host,after:first.plan.next_cursor,limit:2},100);
  assert.deepEqual(first.plan.records.map(r=>r.id),['old-0','old-1']);assert.deepEqual(second.plan.records.map(r=>r.id),['old-2','old-3']);assert.equal(first.plan.more,true);
  await assert.rejects(maintenanceHistoryPlan(f.env,{host:f.host,limit:129},100),/range/);
  const evidence=await (await f.send({operation:'retention_history',ids:['foreign','old-0']})).json();assert.equal(evidence.records.length,1);assert.equal(evidence.blocked[0].id,'foreign');
  assert.equal((await f.send({operation:'retention_history',ids:Array.from({length:129},(_,i)=>String(i))})).status,400);
 }finally{f.sql.close()}
});

test('oversized historical row cannot wedge pagination or bypass device/apply byte guards',async()=>{
 const f=await fixture();try{
  await f.legacy('a-huge');await f.legacy('b-small');
  f.sql.prepare("UPDATE maintenance_jobs SET result=? WHERE id='a-huge'").run(JSON.stringify({state:'succeeded',stdout:'x'.repeat(9*1024*1024)}));
  const saved=await maintenanceHistoryPlan(f.env,{host:f.host,limit:1},100);
  assert.equal(saved.plan.next_cursor,'a-huge');assert.equal(saved.plan.blocked.length,1);
  const next=await maintenanceHistoryPlan(f.env,{host:f.host,after:saved.plan.next_cursor},100);assert.deepEqual(next.plan.records.map(r=>r.id),['b-small']);
  const device=await (await f.send({operation:'retention_history',ids:['a-huge','b-small']})).json();
  assert.deepEqual(device.records.map(r=>r.id),['b-small']);assert.match(device.blocked[0].reason,/byte budget/);
  // A formerly reviewed small row that grew cannot cause apply to read an
  // unbounded body while discovering its stale preimage.
  const small=await maintenanceHistoryPlan(f.env,{host:f.host,after:'a-huge'},100);
  f.sql.prepare("UPDATE maintenance_jobs SET result=? WHERE id='b-small'").run(JSON.stringify({state:'succeeded',stdout:'y'.repeat(9*1024*1024)}));
  const applied=await maintenanceHistoryApply(f.env,small.plan,small.plan_sha256,100);
  assert.deepEqual(applied.enrolled,[]);assert.match(applied.blocked[0].reason,/byte budget/);
 }finally{f.sql.close()}
});
