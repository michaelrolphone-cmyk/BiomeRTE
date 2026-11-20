import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {sqliteD1} from './sqlite-d1.mjs';
import {device,hexHash,runTool} from '../lib/bridge.mjs';

async function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const mac='a'.repeat(64),pi='b'.repeat(64),tokens={mac:'1'.repeat(64),pi:'2'.repeat(64),legacy:'3'.repeat(64)};
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'site',1)");
 for(const id of ['mac','pi'])sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,1,2,?,?)')
  .run(id,await hexHash(tokens[id]),id==='mac'?Date.now()/1000:0,id==='mac'?1:0);
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'pi',
  CONTROLLER_NODE_MAP:JSON.stringify({[mac]:'mac',[pi]:'pi'})};
 const send=(path,body={},who='mac',method='POST')=>device(new Request('https://gateway'+path,{
  method,headers:who?{Authorization:'Bearer '+(tokens[who]??who)}:{},
  ...(method==='POST'?{body:JSON.stringify(body)}:{})}),env,path);
 return {sql,env,send,mac,pi,tokens};
}
async function queued(f){
 for(let i=0;i<100;i++){
  const job=f.sql.prepare("SELECT * FROM bridge_jobs WHERE state='queued' ORDER BY updated,id LIMIT 1").get();
  if(job)return job;
  await new Promise(resolve=>setTimeout(resolve,5));
 }
 assert.fail('Delivery check did not enqueue a controller request');
}
async function finish(f,pending,owner='mac',result=null){
 const job=await queued(f),request=JSON.parse(job.request);
 const value=result??{content:[{type:'text',text:JSON.stringify({node:f[owner],health:{runtime:'up'}})}]};
 f.sql.prepare("UPDATE bridge_jobs SET state='claimed',controller=?,lease='delivery',lease_until=? WHERE id=?")
  .run(owner,Date.now()/1000+60,job.id);
 const complete={id:job.id,lease:'delivery',result:value};
 assert.equal((await f.send('/bridge/complete',complete,owner==='mac'?'pi':'mac')).status,409);
 assert.equal((await f.send('/bridge/complete',complete,owner)).status,200);
 return {response:await pending,job,request,value};
}

test('registered device checks its own route while the default controller is offline',async()=>{
 const f=await fixture();try{
  const {response,request,value}=await finish(f,f.send('/bridge/check'));
  assert.equal(response.status,200);
  assert.deepEqual(await response.json(),value);
  assert.equal(request.name,'biome_status');
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
 }finally{f.sql.close()}
});
test('body cannot redirect the authenticated device or choose a different operation',async()=>{
 const f=await fixture();try{
  const {request}=await finish(f,f.send('/bridge/check',{controller:f.pi,controller_owner:'pi',operation:'cap.acquire',arguments:{}}));
  assert.equal(request.name,'biome_status');
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
 }finally{f.sql.close()}
});
test('controller tool failure is preserved instead of synthesizing a healthy check',async()=>{
 const f=await fixture();try{
  const result={isError:true,content:[{type:'text',text:'Controller unavailable'}]};
  const {response}=await finish(f,f.send('/bridge/check'),'mac',result);
  assert.deepEqual(await response.json(),result);
 }finally{f.sql.close()}
});
test('missing, incorrect, and disabled device credentials never enqueue a check',async()=>{
 const f=await fixture();try{
  assert.equal((await f.send('/bridge/check',{},null)).status,401);
  assert.equal((await f.send('/bridge/check',{},'9'.repeat(64))).status,401);
  f.sql.prepare("UPDATE controller_nodes SET enabled=0 WHERE id='mac'").run();
  assert.equal((await f.send('/bridge/check')).status,401);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
test('registered device rejects absent, ambiguous, and malformed mappings without default fallback',async()=>{
 const f=await fixture();try{
  const cases=[
   [undefined,409],
   [JSON.stringify({[f.pi]:'pi'}),409],
   [JSON.stringify({[f.mac]:'mac',[f.pi]:'mac'}),409],
   ['{',503],
   ['null',503],
   ['[]',503],
   ['1',503]
  ];
  for(const [mapping,status] of cases){
   f.env.CONTROLLER_NODE_MAP=mapping;
   assert.equal((await f.send('/bridge/check')).status,status,String(mapping));
  }
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
test('only POST is accepted for an authenticated device check',async()=>{
 const f=await fixture();try{
  assert.equal((await f.send('/bridge/check',{},'mac','GET')).status,405);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
test('unregistered legacy bridge credential retains its existing default check',async()=>{
 const f=await fixture();try{
  f.env.BIOME_BRIDGE_PIN=await hexHash(await hexHash(f.tokens.legacy)+'\n');
  const {request}=await finish(f,f.send('/bridge/check',{},'legacy'),'pi');
  assert.equal(request.name,'biome_status');
  assert.deepEqual(request.arguments,{controller_owner:'pi'});
 }finally{f.sql.close()}
});
test('registered identity takes precedence when the credential also matches legacy authorization',async()=>{
 const f=await fixture();try{
  f.env.BIOME_BRIDGE_PIN=await hexHash(await hexHash(f.tokens.mac)+'\n');
  const {request}=await finish(f,f.send('/bridge/check'));
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
 }finally{f.sql.close()}
});

const managedKeys=generateKeyPairSync('rsa',{modulusLength:2048});
function managed(f,{retained=false}={}){
 const declaration={service_id:'site-controller',authority:'c'.repeat(64),
  controller_aliases:[f.mac,f.pi],
  operator_actor:createHash('sha256').update(managedKeys.publicKey.export({type:'spki',format:'der'})).digest('hex')};
 f.env.SERVICE_AUTHORITY_ORIGIN='https://authority.example';
 f.env.CONTROLLER_RUNTIME_SIGNING_KEY=managedKeys.privateKey.export({type:'pkcs8',format:'pem'});
 if(retained){
  f.sql.exec('CREATE TABLE controller_runtime_scopes(site TEXT PRIMARY KEY,declaration TEXT NOT NULL)');
  f.sql.prepare('INSERT INTO controller_runtime_scopes VALUES(?,?)').run('site',JSON.stringify(declaration));
 }else{
  f.env.CONTROLLER_RUNTIME_AUTHORITY=JSON.stringify(declaration);
  f.env.CONTROLLER_RUNTIME_REQUIRED='1';
 }
 return declaration;
}

test('managed runtime keeps each authenticated device health check on its physical link',async t=>{
 const fetch=t.mock.method(globalThis,'fetch',async()=>{throw Error('Device health reached the managed authority')});
 for(const owner of ['mac','pi']){
  const f=await fixture();try{
   managed(f);
   const other=owner==='mac'?'pi':'mac';
   const pending=f.send('/bridge/check',{controller:f[other],operation:'cap.acquire',arguments:{}},owner);
   const {response,request,value}=await finish(f,pending,owner);
   assert.equal(response.status,200);
   assert.deepEqual(await response.json(),value);
   assert.equal(request.name,'biome_status');
   assert.deepEqual(request.arguments,{controller:f[owner],controller_owner:owner});
  }finally{f.sql.close()}
 }
 assert.equal(fetch.mock.callCount(),0);
});

test('retained logical custody cannot redirect the physical recovery check after flags disappear',async t=>{
 const fetch=t.mock.method(globalThis,'fetch',async()=>{throw Error('Retained runtime intercepted device health')});
 const f=await fixture();try{
  managed(f,{retained:true});
  const {response,request,value}=await finish(f,f.send('/bridge/check'));
  assert.deepEqual(await response.json(),value);
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
  assert.equal(fetch.mock.callCount(),0);
 }finally{f.sql.close()}
});

test('missing or malformed managed configuration does not block authenticated native recovery',async t=>{
 const fetch=t.mock.method(globalThis,'fetch',async()=>{throw Error('Recovery depended on managed configuration')});
 for(const declaration of [undefined,'{']){
  const f=await fixture();try{
   f.env.CONTROLLER_RUNTIME_REQUIRED='1';
   if(declaration!==undefined)f.env.CONTROLLER_RUNTIME_AUTHORITY=declaration;
   const {response,request,value}=await finish(f,f.send('/bridge/check'));
   assert.deepEqual(await response.json(),value);
   assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
  }finally{f.sql.close()}
 }
 assert.equal(fetch.mock.callCount(),0);
});

test('ordinary operator status still uses the logical authority when device recovery is separate',async t=>{
 const f=await fixture();try{
  const declaration=managed(f);
  const fetch=t.mock.method(globalThis,'fetch',async(url,request)=>{
   assert.equal(url,'https://authority.example/api/controller-runtimes/'+declaration.authority+'/call');
   const call=JSON.parse(request.body);
   assert.equal(call.auth.actor,declaration.operator_actor);
   assert.ok(call.auth.signature);
   assert.equal(request.redirect,'error');
   return Response.json({result:{node:declaration.authority,site:'site'}});
  });
  const reply=await runTool(f.env,'biome_status',{controller:f.mac});
  assert.equal(reply.isError,undefined);
  assert.equal(JSON.parse(reply.content[0].text).node,declaration.authority);
  assert.equal(fetch.mock.callCount(),3);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
