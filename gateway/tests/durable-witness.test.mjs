import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {durableWitness} from '../lib/durable-witness.mjs';

const protocol='biomerte-durable-witness/1',secret='witness-private-credential-'.repeat(3);
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64),scope='d'.repeat(64);
function fixture(){
 const sql=new DatabaseSync(':memory:'),env={DB:sqliteD1(sql),DURABLE_WITNESS_SECRET:secret,DURABLE_WITNESS_ID:'services'};
 const send=async(body,headers={})=>{
  const reply=await durableWitness(new Request('https://gateway.test/internal/service-witness/v1',{
   method:'POST',headers:{Authorization:'Bearer '+secret,'Content-Type':'application/json',...headers},
   body:JSON.stringify({protocol,witness:'services',...body})}),env);
  return {status:reply.status,value:await reply.json()};
 };
 const acquire=async(boot='boot-a-0000000000000000000000000000')=>{
  const reply=await send({operation:'acquire',boot_id:boot,lease_s:120,max_grant_s:30});
  assert.equal(reply.status,200);return {boot_id:boot,epoch:reply.value.lease.epoch};
 };
 const ready=()=>sql.prepare("UPDATE service_witness_leases SET ready_at=0 WHERE witness='services'").run();
 const commit=(owner,sequence,digest,expected,previous_digest='')=>send({operation:'commit',owner,
  service_id:'controller',scope_digest:scope,checkpoint:{sequence,digest},expected,previous_digest});
 return {sql,send,acquire,ready,commit};
}

test('durable witness retains only latest metadata and exact retries survive lost replies',async()=>{
 const f=fixture();try{
  const owner=await f.acquire();f.ready();
  assert.equal((await f.commit(owner,0,a,null)).status,200);
  // Treat the first acknowledgement as lost, then retry the identical CAS.
  assert.equal((await f.commit(owner,1,b,{sequence:0,digest:a},a)).status,200);
  assert.equal((await f.commit(owner,1,b,{sequence:0,digest:a},a)).status,200);
  const heads=await f.send({operation:'heads',owner});
  assert.equal(heads.value.heads.length,1);
  assert.equal(heads.value.heads[0].digest,b);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM service_witness_heads').get().n,1);
  const columns=f.sql.prepare('PRAGMA table_info(service_witness_heads)').all().map(x=>x.name);
  assert.deepEqual(columns,['witness','service_id','scope_digest','sequence','digest','committed_epoch']);
 }finally{f.sql.close()}
});

test('rollback, same-sequence conflict, missing CAS parent and changed scope never overwrite',async()=>{
 const f=fixture();try{
  const owner=await f.acquire();f.ready();
  await f.commit(owner,0,a,null);
  await f.commit(owner,1,b,{sequence:0,digest:a},a);
  for(const request of [
   {sequence:0,digest:a,expected:null,parent:''},
   {sequence:1,digest:c,expected:{sequence:1,digest:b},parent:b},
   {sequence:2,digest:c,expected:{sequence:1,digest:b},parent:a},
   {sequence:2,digest:c,expected:null,parent:b}
  ])assert.equal((await f.commit(owner,request.sequence,request.digest,request.expected,request.parent)).status,409);
  const changed=await f.send({operation:'commit',owner,service_id:'controller',scope_digest:c,
   checkpoint:{sequence:2,digest:c},expected:{sequence:1,digest:b},previous_digest:b});
  assert.equal(changed.status,409);
  assert.equal((await f.send({operation:'heads',owner,service_id:'controller'})).value.head.digest,b);
 }finally{f.sql.close()}
});

test('new witness preserves durable head and fences every delayed old-epoch commit',async()=>{
 const f=fixture();try{
  const old=await f.acquire();f.ready();await f.commit(old,0,a,null);
  const denied=await f.send({operation:'acquire',boot_id:'boot-b-0000000000000000000000000000',lease_s:120,max_grant_s:30});
  assert.equal(denied.status,409);
  f.sql.prepare('UPDATE service_witness_leases SET expires_at=0').run();
  const current=await f.acquire('boot-b-0000000000000000000000000000');
  assert.equal(current.epoch,old.epoch+1);
  const lease=f.sql.prepare('SELECT ready_at,expires_at,max_grant_s FROM service_witness_leases').get();
  assert.equal(lease.max_grant_s,30);
  assert.ok(lease.expires_at-lease.ready_at>89.9&&lease.expires_at-lease.ready_at<90.1);
  assert.equal((await f.commit(current,0,a,{sequence:0,digest:a})).status,409,'quarantine must finish before publishing');
  f.ready();
  assert.equal((await f.commit(old,1,b,{sequence:0,digest:a},a)).status,409);
  assert.equal((await f.send({operation:'heads',owner:current})).value.heads[0].digest,a);
  assert.equal((await f.commit(current,0,a,{sequence:0,digest:a})).status,200);
 }finally{f.sql.close()}
});

test('acquire is idempotent for lost replies and concurrent boots never share a lease',async()=>{
 const f=fixture();try{
  const [one,two]=await Promise.all([
   f.send({operation:'acquire',boot_id:'boot-a-0000000000000000000000000000',lease_s:120,max_grant_s:30}),
   f.send({operation:'acquire',boot_id:'boot-b-0000000000000000000000000000',lease_s:120,max_grant_s:30})]);
  assert.deepEqual([one.status,two.status].sort(),[200,409]);
  const first=one.status===200?one:two,boot=first.value.lease.boot_id;
  const retry=await f.send({operation:'acquire',boot_id:boot,lease_s:120,max_grant_s:30});
  assert.equal(retry.value.lease.epoch,first.value.lease.epoch);
  assert.equal(retry.value.lease.expires_at,first.value.lease.expires_at);
 }finally{f.sql.close()}
});

test('expired lease cannot be renewed or commit and transport accepts no payload journal',async()=>{
 const f=fixture();try{
  const owner=await f.acquire();f.ready();
  const wrong=await f.send({operation:'heads',owner},{Authorization:'Bearer wrong'});
  assert.equal(wrong.status,401);
  assert.equal((await f.send({operation:'heads',owner},{Origin:'https://other.test'})).status,403);
  assert.equal((await f.send({operation:'heads',owner,package:{records:[1]}})).status,400);
  f.sql.prepare('UPDATE service_witness_leases SET expires_at=0').run();
  assert.equal((await f.send({operation:'renew',owner,lease_s:120})).status,409);
  assert.equal((await f.commit(owner,0,a,null)).status,409);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM service_witness_heads').get().n,0);
 }finally{f.sql.close()}
});

test('a retiring owner releases only its epoch and the successor still waits out child grants',async()=>{
 const f=fixture();try{
  const old=await f.acquire();f.ready();await f.commit(old,0,a,null);
  const release=owner=>f.send({operation:'release',owner});
  assert.equal((await release({...old,boot_id:'unrelated-boot-0000000000'})).status,409);
  assert.equal((await release(old)).status,200);
  assert.equal((await release(old)).status,200);
  assert.equal((await f.send({operation:'acquire',boot_id:old.boot_id,lease_s:120,max_grant_s:30})).status,409);
  assert.equal((await f.send({operation:'renew',owner:old,lease_s:120})).status,409);
  const next=await f.acquire('next-boot-000000000000');
  assert.equal(next.epoch,old.epoch+1);
  assert.equal((await release(old)).status,409);
  const observed=await f.send({operation:'renew',owner:next,lease_s:120});
  assert.equal(observed.status,200);
  assert.ok(observed.value.lease.ready_at-observed.value.lease.server_now>29);
  assert.equal((await f.commit(next,1,b,{sequence:0,digest:a},a)).status,409);
  assert.equal((await f.send({operation:'heads',owner:next})).value.heads[0].digest,a);
  f.ready();
  assert.equal((await f.commit(old,1,b,{sequence:0,digest:a},a)).status,409);
  assert.equal((await f.commit(next,1,b,{sequence:0,digest:a},a)).status,200);
 }finally{f.sql.close()}
});
