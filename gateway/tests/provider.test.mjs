import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
const base='https://biome.test';
const sha=v=>createHash('sha256').update(v).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const DB={prepare(q){let args=[];const statement=sql.prepare(q);return {
  bind(...v){args=v;return this},async first(){return statement.get(...args)},
  async run(){return statement.run(...args)},async all(){return {results:statement.all(...args)}}
 }}};
 const token='f'.repeat(64),env={DB,BIOME_BRIDGE_PIN:sha(sha(token)+'\n')};
 const send=(path,body,headers={})=>gateway.fetch(new Request(base+path,{
  method:'POST',headers:{'Content-Type':'application/json',...headers},
  body:typeof body==='string'?body:JSON.stringify(body)}),env);
 const controller={Authorization:'Bearer '+token};
 const next=async()=>{for(let n=0;n<200;n++){const value=await(await send('/bridge/next',{},controller)).json();if(value.id)return value;await new Promise(r=>setTimeout(r,5))}throw Error('No queued request')};
 const complete=(job,status,body)=>send('/bridge/complete',{id:job.id,lease:job.lease,
  result:{content:[{type:'text',text:JSON.stringify({status,body})}]}},controller);
 return {sql,send,next,complete,env};
}
const actor='a'.repeat(64),signature=Buffer.alloc(256,17).toString('base64');
const packet=(operation='provider.poll',arguments_={provider:actor,limit:1})=>({
 site:'site-one',realm:'site',id:'stable-id',operation,arguments:arguments_,
 auth:{actor,signature}
});
test('native provider ingress rejects unsigned and owner operations before queueing',async()=>{
 const f=fixture();
 assert.equal((await f.send('/api/provider/call',{})).status,401);
 assert.equal((await f.send('/api/provider/call',packet('peer.trust',{}))).status,400);
 assert.equal((await f.send('/api/provider/call',packet('cap.define',{}))).status,400);
 assert.equal((await f.send('/api/provider/call',packet('provider.poll',{provider:'b'.repeat(64)}))).status,400);
 assert.equal((await f.send('/api/provider/call',packet(),{Origin:'https://wrong.test'})).status,403);
 assert.equal((await f.send('/api/provider/call',packet(),{'Content-Type':'text/plain'})).status,415);
 assert.equal(f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n,0);
 f.sql.close();
});
test('exact signed JSON survives transport and each retry rechecks live authorization',async()=>{
 const f=fixture();
 const p=packet('cap.offer',{provider:actor,contract:'relay.bank:1',resource:'board',configuration:{number:1}});
 const raw=JSON.stringify(p).replace('"number":1','"number":1.0,"big":9007199254740993');
 let previous;
 for(const [status,body] of [[200,{result:{id:'offer'}}],[403,{error:'Revoked credential'}]]){
  const waiting=f.send('/api/provider/call',raw),job=await f.next();
  assert.equal(job.request.name,'signed_provider_call');
  assert.deepEqual(job.request.arguments,{envelope:raw});
  assert.notEqual(job.id,previous);previous=job.id;
  await f.complete(job,status,body);
  const reply=await waiting;assert.equal(reply.status,status);assert.deepEqual(await reply.json(),body);
 }
 assert.equal(f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n,2);
 f.sql.close();
});
test('signed withdrawal forwards the exact revisions only for the named provider',async()=>{
 const f=fixture();
 try{
  const args={provider:actor,resource:'camera',service:'vision',registrations:{'vision.status:1':'1'.repeat(32)}};
  const rejected=await f.send('/api/provider/call',packet('cap.withdraw',{...args,provider:'b'.repeat(64)}));
  assert.equal(rejected.status,400);
  assert.equal(f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n,0);
  const raw=JSON.stringify(packet('cap.withdraw',args));
  const waiting=f.send('/api/provider/call',raw),job=await f.next();
  assert.equal(job.request.name,'signed_provider_call');
  assert.deepEqual(job.request.arguments,{envelope:raw});
  const body={result:{withdrawn:['vision.status:1'],superseded:[]}};
  await f.complete(job,200,body);
  const response=await waiting;assert.equal(response.status,200);assert.deepEqual(await response.json(),body);
 }finally{f.sql.close()}
});
test('signed registration lookup and restore preserve the exact enrolled batch',async()=>{
 const f=fixture();
 const contract='vision.status:1',registration='1'.repeat(32);
 const scope={provider:actor,resource:'camera',service:'vision'};
 const definition={name:'vision.status',version:1,inputs:{},outputs:{}};
 const offer={contract,state:'ready',capacity:1};
 const calls=[
  ['cap.registrations',{...scope,contracts:[contract]},
   {registrations:[{contract,status:'found',state:'offline',registration}]}],
  ['cap.publish',{...scope,offers:[{definition,offer,expected_registration:registration}]},
   {offers:[{contract,status:'published',state:'ready',registration:'2'.repeat(32)}]}],
 ];
 try{
  for(const [operation,args,result] of calls){
   const count=f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n;
   const denied=await f.send('/api/provider/call',packet(operation,{...args,provider:'b'.repeat(64)}));
   assert.equal(denied.status,400);
   assert.equal(f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n,count);
   const raw=JSON.stringify(packet(operation,args),null,2);
   const waiting=f.send('/api/provider/call',raw),job=await f.next();
   assert.equal(job.request.name,'signed_provider_call');
   assert.equal(job.request.arguments.envelope,raw);
   await f.complete(job,200,{result});
   const reply=await waiting;
   assert.equal(reply.status,200);
   assert.deepEqual(await reply.json(),{result});
  }
 }finally{f.sql.close()}
});
test('public pairing forwards candidate and proof only; approval never exposed',async()=>{
 const f=fixture();
 assert.equal((await f.send('/api/provider/pair/approve',{fingerprint:actor})).status,404);
 const beginning={public_key:'-----BEGIN PUBLIC KEY-----\nexample\n-----END PUBLIC KEY-----',label:'Biome Relay test'};
 const proof={challenge:'1'.repeat(64),site:'site-one',key:actor,signature};
 for(const [phase,body,status,result] of [
  ['begin',beginning,200,{result:{challenge:proof.challenge,site:proof.site,key:actor}}],
  ['finish',proof,403,{error:'Fingerprint not approved'}]
 ]){
  const waiting=f.send('/api/provider/pair/'+phase,body),job=await f.next();
  assert.equal(job.request.name,'signed_provider_pair');
  assert.equal(job.request.arguments.phase,phase);
  assert.deepEqual(JSON.parse(job.request.arguments.envelope),body);
  await f.complete(job,status,result);
  const response=await waiting;assert.equal(response.status,status);assert.deepEqual(await response.json(),result);
 }
 assert.equal((await f.send('/api/provider/pair/begin',{...beginning,scopes:['*']})).status,400);
 f.sql.close();
});

test('large signed provider result and pairing envelopes remain exact without an ingress cap',async()=>{
 const f=fixture();try{
  const payload=packet('provider.result',{provider:actor,result:{text:'😀'.repeat(270000)}});
  const raw=JSON.stringify(payload);assert.ok(Buffer.byteLength(raw)>1048576);
  const pending=f.send('/api/provider/call',raw),job=await f.next();
  assert.equal(job.request.arguments.envelope,raw);
  await f.complete(job,200,{result:{accepted:true}});
  assert.equal((await pending).status,200);
  const pair=JSON.stringify({public_key:'-----BEGIN PUBLIC KEY-----\nexample\n-----END PUBLIC KEY-----',label:'x'.repeat(140000)});
  const pairing=f.send('/api/provider/pair/begin',pair),candidate=await f.next();
  assert.equal(candidate.request.arguments.envelope,pair);
  await f.complete(candidate,403,{error:'Fingerprint not approved'});
  assert.equal((await pairing).status,403);
 }finally{f.sql.close()}
});

function captureFailures(t){
 const logs=[];t.mock.method(console,'error',(event,record)=>logs.push({event,...record}));return logs;
}
function assertSafeLog(logs,secrets=[]){
 const encoded=JSON.stringify(logs);
 for(const secret of [signature,actor,'stable-id','payload-canary','auth-canary',...secrets])assert.ok(!encoded.includes(secret),'No sensitive data: '+secret);
 for(const log of logs){assert.equal(log.event,'provider_gateway_failure');assert.equal(log.schema,1);assert.ok(log.elapsed_ms>=0);assert.ok(log.bridge_job.startsWith('provider-'));}
}
test('provider queue admission 503 is correlated and contains no payload or credentials',async t=>{
 const f=fixture(),logs=captureFailures(t),now=Math.floor(Date.now()/1000);
 try{
  for(let i=0;i<40;i++)f.sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,'{}','queued',?,?)").run('busy-'+i,now+60,now);
  const response=await f.send('/api/provider/call',packet(),{'cf-ray':'0123456789abcdef-SJC',Authorization:'Bearer auth-canary'});
  assert.equal(response.status,503);assert.deepEqual(await response.json(),{error:'Controller reply unavailable',retry_same_id:true});
  assert.equal(logs.length,1);assert.equal(logs[0].stage,'queue_admission');assert.equal(logs[0].reason,'queue_busy');assert.equal(logs[0].queue_capacity,40);assert.equal(logs[0].ray_id,'0123456789abcdef-SJC');
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,40);assertSafeLog(logs);
 }finally{f.sql.close()}
});
test('provider controller errors and malformed responses have distinct stages without body text',async t=>{
 const f=fixture(),logs=captureFailures(t);
 try{
  for(const [result,status,stage,reason] of [
   [{isError:true,content:[{type:'text',text:'Gateway journal HTTP 503'}]},503,'controller','controller_tool_error'],
   [{isError:true,content:[{type:'text',text:'payload-canary'}]},503,'controller','controller_tool_error'],
   [{content:[{type:'text',text:'payload-canary'}]},502,'response','controller_response_json_invalid'],
   [{content:[{type:'text',text:JSON.stringify({status:503,body:'payload-canary'})}]},502,'response','controller_response_shape_invalid'],
   [{content:[{type:'text',text:JSON.stringify({status:503,body:{error:'payload-canary'}})}]},503,'controller','controller_http_error'],
   [{content:[{type:'text',text:JSON.stringify({status:204,body:{error:'payload-canary'}})}]},503,'response','controller_response_serialization_failed']
  ]){
   const waiting=f.send('/api/provider/call',packet()),job=await f.next();
   f.sql.prepare("UPDATE bridge_jobs SET state='done',result=? WHERE id=?").run(JSON.stringify(result),job.id);
   const response=await waiting;assert.equal(response.status,status);
   const log=logs.at(-1);assert.equal(log.stage,stage);assert.equal(log.reason,reason);assert.equal(log.bridge_job,job.id);assert.equal(log.result_source,'live');
  }
  assert.equal(logs.length,6);assert.equal(logs[0].error_kind,'journal_http_503');assertSafeLog(logs);
 }finally{f.sql.close()}
});
test('provider queue database failures are safe and do not expose exception messages',async t=>{
 const f=fixture(),logs=captureFailures(t);
 try{
  f.env.DB={prepare(){throw Error('D1_ERROR: payload-canary auth-canary')}};
  const response=await f.send('/api/provider/call',packet());assert.equal(response.status,503);
  assert.deepEqual(await response.json(),{error:'Controller gateway unavailable'});
  assert.equal(logs.length,1);assert.equal(logs[0].reason,'runtime_configuration_failed');assert.equal(logs[0].error_kind,'database_error');assertSafeLog(logs);
 }finally{f.sql.close()}
});
test('provider stored result failure is separate from a controller-returned error',async t=>{
 const f=fixture(),logs=captureFailures(t);
 try{
  const waiting=f.send('/api/provider/call',packet()),job=await f.next();
  f.sql.prepare("UPDATE bridge_jobs SET state='done',result=? WHERE id=?").run('payload-canary',job.id);
  assert.equal((await waiting).status,503);assert.equal(logs.length,1);assert.equal(logs[0].stage,'response');assert.equal(logs[0].reason,'stored_result_read_failed');assert.equal(logs[0].error_kind,'invalid_json');assertSafeLog(logs);
 }finally{f.sql.close()}
});
test('provider waiter expiry records observed state without retrying or deleting the job',async t=>{
 const f=fixture(),logs=captureFailures(t),realTimeout=globalThis.setTimeout;
 t.mock.method(globalThis,'setTimeout',(fn,ms,...args)=>realTimeout(fn,0,...args));
 try{
  assert.equal((await f.send('/api/provider/call',packet())).status,503);
  assert.equal(logs.length,1);assert.equal(logs[0].stage,'controller_wait');assert.equal(logs[0].reason,'controller_reply_timeout');assert.equal(logs[0].poll_count,80);assert.equal(logs[0].job_state,'queued');
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);assertSafeLog(logs);
 }finally{f.sql.close()}
});
test('success produces no failure logs and logger failures do not change responses',async t=>{
 const f=fixture(),logs=captureFailures(t);
 try{
  const waiting=f.send('/api/provider/call',packet()),job=await f.next();await f.complete(job,200,{result:{accepted:true}});
  assert.equal((await waiting).status,200);assert.equal(logs.length,0);
  t.mock.method(console,'error',()=>{throw Error('logger failed')});
  const failure=f.send('/api/provider/call',packet()),failedJob=await f.next();await f.complete(failedJob,503,{error:'payload-canary'});
  assert.equal((await failure).status,503);
 }finally{f.sql.close()}
});
