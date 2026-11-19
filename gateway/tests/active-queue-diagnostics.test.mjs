import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import {activeQueueDiagnostics} from '../lib/diagnostics.mjs';
import gateway from '../worker/index.js';

function fixture(now=1000){
 const sql=new DatabaseSync(':memory:');
 for(const name of ['0000_biome_gateway.sql','0001_controller_ha.sql','0003_bridge_live_claims.sql'])sql.exec(readFileSync('drizzle/'+name,'utf8'));
 const queries=[],baseDB=sqliteD1(sql);
 const DB={...baseDB,prepare(query){queries.push(query);return baseDB.prepare(query)}};
 let serial=0;
 const add=(request,{state='queued',controller=null,updated=now-1,deadline=now+22,leaseUntil=now+35,id='job-'+(++serial)}={})=>{
  sql.prepare('INSERT INTO bridge_jobs(id,request,state,deadline,updated,controller,lease,lease_until) VALUES(?,?,?,?,?,?,?,?)')
   .run(id,typeof request==='string'?request:JSON.stringify(request),state,deadline,updated,controller,'private-lease',leaseUntil);
 };
 return {sql,DB,queries,add,now};
}
const invoke=operation=>({name:'biome_invoke',arguments:{operation,request_id:'private-request',arguments:{}}});
const provider=operation=>({name:'signed_provider_call',arguments:{envelope:JSON.stringify({operation,auth:{signature:'PRIVATE-SIGNATURE',actor:'PRIVATE-ACTOR'},arguments:{body:'PRIVATE-PAYLOAD'}})}});
const native=operation=>({name:'biome_native_call',arguments:{body:JSON.stringify({operation,auth:{signature:'PRIVATE-SIGNATURE'},arguments:{token:'PRIVATE-TOKEN'}})}});

test('active queue groups nested operations and controllers with correct counts and update ages',async()=>{
 const f=fixture();try{
  f.add(invoke('provider.poll'),{updated:990});
  f.add(provider('provider.poll'),{updated:980});
  f.add(provider('provider.result'),{updated:999});
  f.add(native('cap.status'),{state:'claimed',controller:'mac',updated:995.125});
  f.add(invoke('cap.status'),{state:'claimed',controller:'mac',updated:998});
  f.add(invoke('cap.status'),{state:'claimed',controller:'pi',updated:999});
  f.add(invoke('work.list'),{deadline:1000,updated:1002});
  f.add(invoke('cap.invoke'),{state:'done'});
  f.add(provider('provider.poll'),{deadline:999});
  f.add(native('cap.invoke'),{state:'claimed',deadline:999,leaseUntil:999});
  const out=await activeQueueDiagnostics(f.DB,f.now);
  assert.equal(out.active_jobs,7);assert.equal(out.group_count,5);assert.equal(out.truncated,false);
  assert.equal(out.observed_at,1000);assert.equal(out.age_basis,'last_bridge_job_update');
  assert.deepEqual(out.groups,[
   {state:'queued',controller:null,operation:'provider.poll',count:2,oldest_state_age_seconds:20},
   {state:'claimed',controller:'mac',operation:'cap.status',count:2,oldest_state_age_seconds:4.875},
   {state:'claimed',controller:'pi',operation:'cap.status',count:1,oldest_state_age_seconds:1},
   {state:'queued',controller:null,operation:'provider.result',count:1,oldest_state_age_seconds:1},
   {state:'queued',controller:null,operation:'work.list',count:1,oldest_state_age_seconds:0}
  ]);
 }finally{f.sql.close()}
});

test('a live claimed lease remains visible beyond its dispatch deadline without double-counting',async()=>{
 const f=fixture();try{
  f.add(invoke('cap.status'),{state:'claimed',controller:'mac',deadline:999,leaseUntil:1001,updated:970});
  f.add(invoke('cap.status'),{state:'claimed',controller:'mac',deadline:1022,leaseUntil:1035,updated:995});
  f.add(provider('provider.poll'),{deadline:999,leaseUntil:1035});
  f.add(invoke('cap.invoke'),{state:'claimed',controller:'pi',deadline:999,leaseUntil:999});
  f.add(invoke('cap.invoke'),{state:'claimed',controller:'pi',deadline:999,leaseUntil:1000});
  const out=await activeQueueDiagnostics(f.DB,f.now);
  assert.equal(out.active_basis,'dispatch_deadline_or_live_claim');assert.equal(out.active_jobs,2);
  assert.deepEqual(out.groups,[{state:'claimed',controller:'mac',operation:'cap.status',count:2,oldest_state_age_seconds:30}]);
 }finally{f.sql.close()}
});

test('diagnostics never returns payloads, signatures, tokens or IDs, including malformed envelopes',async()=>{
 const f=fixture();try{
  f.add(provider('provider.poll'),{id:'PRIVATE-JOB-ID'});
  f.add(native('cap.status'));
  f.add({name:'biome_invoke',arguments:{operation:'cap.status',request_id:'PRIVATE-REQUEST-ID',arguments:{argv:['/bin/bash','PRIVATE-SCRIPT'],token:'PRIVATE-TOKEN'}}});
  for(const request of [
   'PRIVATE-MALFORMED-JSON',
   {name:'signed_provider_call',arguments:{envelope:'PRIVATE-MALFORMED-ENVELOPE'}},
   {name:'biome_native_call',arguments:{body:'null'}},
   invoke({token:'PRIVATE-TOKEN'}),
   invoke('Bearer PRIVATE-TOKEN'),
   invoke('a'.repeat(64)),
   {name:'PRIVATE-TOOL-NAME',arguments:{operation:'provider.result'}}
  ])f.add(request);
  const out=await activeQueueDiagnostics(f.DB,f.now),encoded=JSON.stringify(out);
  assert.equal(out.active_jobs,10);assert.equal(out.groups.find(g=>g.operation==='unknown').count,7);
  assert.doesNotMatch(encoded,/PRIVATE|signature|request_id|argv|private-lease/);
  for(const group of out.groups)assert.deepEqual(Object.keys(group).sort(),['controller','count','oldest_state_age_seconds','operation','state']);
  assert.equal(f.queries.length,1);
  assert.doesNotMatch(f.queries[0],/\bauth\b|\bsignature\b|\btoken\b/i);
 }finally{f.sql.close()}
});

test('group output is bounded while complete totals and truncation remain explicit',async()=>{
 const f=fixture();try{
  // Deliberately exceed normal 40-job admission to cover legacy/retry state.
  for(let i=0;i<100;i++)f.add(invoke('test.operation'+i),{updated:900+i});
  const out=await activeQueueDiagnostics(f.DB,f.now);
  assert.equal(out.limit,64);assert.equal(out.groups.length,64);assert.equal(out.group_count,100);assert.equal(out.active_jobs,100);assert.equal(out.truncated,true);
  assert.equal(out.groups[0].operation,'test.operation0');assert.equal(out.groups.at(-1).operation,'test.operation63');
  assert.equal(out.groups[0].oldest_state_age_seconds,100);
 }finally{f.sql.close()}
});

test('empty diagnostics and the real query plan use only indexed active ranges',async()=>{
 const f=fixture();try{
  for(let i=0;i<200;i++)f.add(provider('provider.poll'),{state:i%2?'done':'queued',deadline:i%2?1022:999});
  for(let i=0;i<100;i++)f.add(native('cap.status'),{state:'claimed',deadline:999,leaseUntil:999});
  const out=await activeQueueDiagnostics(f.DB,f.now);
  assert.equal(out.active_jobs,0);assert.equal(out.group_count,0);assert.deepEqual(out.groups,[]);assert.equal(out.truncated,false);
  const plan=f.sql.prepare('EXPLAIN QUERY PLAN '+f.queries[0]).all(f.now,f.now,f.now,64).map(row=>row.detail).join('\n');
  assert.match(plan,/SEARCH bridge_jobs USING INDEX bridge_dispatch \(state=\? AND deadline>\?\)/);
  assert.match(plan,/SEARCH bridge_jobs USING INDEX bridge_live_claims \(lease_until>\?\)/);
  assert.doesNotMatch(plan,/SCAN bridge_jobs/);
 }finally{f.sql.close()}
});

test('existing authenticated diagnostics adds aggregates without changing native queue or delivery state',async()=>{
 const f=fixture(Math.floor(Date.now()/1000));try{
  f.sql.prepare("INSERT INTO biome_owner VALUES(1,'owner')").run();
  f.sql.prepare("INSERT INTO controller_config(id,site,enabled) VALUES(1,'test-site',1)").run();
  f.sql.prepare("INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES('mac','PRIVATE-NODE-TOKEN',10,8,?,1)").run(f.now);
  f.add(provider('provider.poll'),{state:'claimed',controller:'mac'});
  f.add(invoke('cap.invoke'));
  f.add(invoke('host.terminal.run'),{state:'done'});
  const before=f.sql.prepare('SELECT * FROM bridge_jobs ORDER BY id').all();
  const base='https://biome.test',token='d'.repeat(43);
  f.sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)")
   .run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',f.now+60);
  const env={DB:f.DB,BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid'};
  const body={jsonrpc:'2.0',id:'diagnostic',method:'tools/call',params:{name:'biome_invoke',arguments:{operation:'controller.diagnostics',arguments:{},request_id:'diagnostic-only'}}};
  const send=(path,headers={})=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)}),env);
  assert.equal((await send('/mcp')).status,403);assert.equal((await send('/api/mcp')).status,401);
  for(const [path,headers] of [
   ['/mcp',{'oai-authenticated-user-id':'owner','oai-authenticated-user-email':'owner@test.invalid'}],
   ['/api/mcp',{Authorization:'Bearer '+token}]
  ]){
   const response=await send(path,headers);assert.equal(response.status,200);
   const rpc=await response.json(),out=JSON.parse(rpc.result.content[0].text);
   assert.equal(out.cluster.site,'test-site');assert.deepEqual(out.journal,[]);
   assert.deepEqual(out.queue.map(row=>({...row})),[{state:'claimed',count:1},{state:'done',count:1},{state:'queued',count:1}]);
   assert.equal(out.active_queue.active_jobs,2);assert.equal(out.active_queue.group_count,2);
   assert.ok(out.active_queue.groups.some(row=>row.operation==='provider.poll'&&row.controller==='mac'));
   assert.doesNotMatch(JSON.stringify(out),/PRIVATE|private-lease/);
  }
  assert.deepEqual(f.sql.prepare('SELECT * FROM bridge_jobs ORDER BY id').all(),before);
 }finally{f.sql.close()}
});
