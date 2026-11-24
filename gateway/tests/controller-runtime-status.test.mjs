import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash,createHmac,webcrypto} from 'node:crypto';
import gateway from '../worker/index.js';
import {runTool} from '../lib/bridge.mjs';
import {pythonCanonical} from '../lib/controller-runtime.mjs';
import {sqliteD1} from './sqlite-d1.mjs';
globalThis.crypto??=webcrypto;

const base='https://gateway.test',origin='https://coordinator.test';
const authority='a'.repeat(64),mac='b'.repeat(64),pi='c'.repeat(64),actor='d'.repeat(64);
const secret='coordinator-test-only-'.repeat(4),privateValue='never-return-private-value';
const declaration={service_id:'site-controller',authority,controller_aliases:[mac,pi],operator_actor:actor};
const specification={id:declaration.service_id,images:{'linux/arm64':'sha256:'+'e'.repeat(64)},
 effect_policy:'externally_fenced',state_policy:'externally_managed',lease_ttl_s:10,
 startup_grace_s:120,preferred_workers:['mac'],activation:{protocol:'biomerte-service-adapter/1',binding:'controller'},
 authority:{id:authority,kind:'controller',resources:[],providers:[],controller_aliases:[mac,pi]},
 command:['python3','-m','biomerte.service_daemon','authority'],
 annotations:{deployment:'current',future_field:{preserved:true}}};
const manifest={...specification,revision:createHash('sha256').update(pythonCanonical(specification)).digest('hex')};
const args=()=>({operation:'controller.runtime_status',arguments:{},request_id:'status-read'});
const owner={'oai-authenticated-user-id':'owner','oai-authenticated-user-email':'owner@test.invalid'};
function fixture(){
 const sql=new DatabaseSync(':memory:');
 sql.exec("CREATE TABLE controller_config(id INTEGER PRIMARY KEY,site TEXT);INSERT INTO controller_config VALUES(1,'site');"+
  "CREATE TABLE biome_owner(singleton INTEGER PRIMARY KEY,user_id TEXT);INSERT INTO biome_owner VALUES(1,'owner');"+
  "CREATE TABLE biome_oauth_tokens(hash TEXT,kind TEXT,principal TEXT,resource TEXT,expires REAL)");
 const env={DB:sqliteD1(sql),BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',
  CONTROLLER_RUNTIME_AUTHORITY:JSON.stringify(declaration),CONTROLLER_RUNTIME_REQUIRED:'1',
  SERVICE_AUTHORITY_ORIGIN:origin,STREAM_ROUTER_SECRET:secret,UNRELATED_SECRET:privateValue};
 // No runtime operator signing key or physical controller queue is installed.
 const send=(input=args(),headers=owner,path='/mcp')=>gateway.fetch(new Request(base+path,{
  method:'POST',headers:{'Content-Type':'application/json',...headers},
  body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'biome_invoke',arguments:input}})
 }),env);
 return {sql,env,send};
}
function observation(){
 return {protocol:'biomerte-service-coordinator/1',authority_mode:'single_process',
  boot_epoch:'f'.repeat(32),max_lease_ttl_s:30,boot_grace_remaining_s:0,
  supported_features:['verified_external_fence_wait','independent_authority_rpc_timeout'],
  durable_witness_ready:true,authority_transport:{registration_timeout_s:5,rpc_timeout_s:20,secret:privateValue},
  environment:{STREAM_ROUTER_SECRET:secret},services:{
   [declaration.service_id]:{service_id:declaration.service_id,manifest_revision:manifest.revision,
    state:'service_unhealthy',conflicting_revisions:[],desired_manifest:structuredClone(manifest),
    owner:{worker_id:'mac',worker_incarnation:'incarnation-1',lease_ttl_s:8,draining:false,grant_token:privateValue},
    candidates:[{worker_id:'mac',worker_incarnation:'incarnation-1',reason:'service_unhealthy',
     checkpoint:{service_id:declaration.service_id,sequence:42,digest:'1'.repeat(64),
      boot_epoch:'f'.repeat(32),package:privateValue},state_proof:privateValue}],private_field:privateValue},
   other:{private_field:privateValue}
  }};
}
async function fetched(fn,reply=()=>Response.json(observation())){
 const original=globalThis.fetch,calls=[];
 globalThis.fetch=async(url,init)=>{calls.push({url,...init});return reply(url,init)};
 try{return await fn(calls)}finally{globalThis.fetch=original}
}
const value=async response=>{
 assert.equal(response.status,200);
 const result=(await response.json()).result;
 assert.notEqual(result.isError,true,result.content?.[0]?.text);
 return JSON.parse(result.content[0].text);
};
test('owner can diagnose an unavailable child through the fixed coordinator route',async()=>{
 const {sql,send}=fixture();
 try{await fetched(async calls=>{
  const result=await value(await send());
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,origin+'/api/service-coordinator/status');
  assert.equal(calls[0].method,'GET');assert.equal(calls[0].body,undefined);
  assert.equal(calls[0].redirect,'manual');
  assert.equal(calls[0].headers.Authorization,'Bearer '+createHmac('sha256',secret).update('biomerte/service-coordinator/v1').digest('hex'));
  assert.equal(new Headers(calls[0].headers).has('Origin'),false);
  assert.equal(result.service.state,'service_unhealthy');
  assert.equal(result.service.candidates[0].reason,'service_unhealthy');
  assert.equal(result.service_id,declaration.service_id);assert.equal(result.authority,authority);
  assert.deepEqual(result.service.desired_manifest,manifest);
  assert.deepEqual(result.coordinator.authority_transport,{registration_timeout_s:5,rpc_timeout_s:20});
  assert.equal(result.service.candidates[0].checkpoint.sequence,42);
  const encoded=JSON.stringify(result);
  assert.equal(encoded.includes(secret),false);assert.equal(encoded.includes(privateValue),false);
  assert.equal(encoded.includes(calls[0].headers.Authorization),false);
  assert.equal(Object.hasOwn(result,'environment'),false);
  assert.equal(Object.hasOwn(result.service.owner,'grant_token'),false);
 });}finally{sql.close()}
});
test('normal OAuth owner authentication remains required and sufficient',async()=>{
 const {sql,env,send}=fixture();
 try{await fetched(async calls=>{
  assert.equal((await send(args(),{},'/api/mcp')).status,401);
  assert.equal((await send(args(),{})).status,403);
  assert.equal((await send(args(),{...owner,'oai-authenticated-user-id':'other'})).status,403);
  assert.equal((await send(args(),{...owner,Origin:'https://untrusted.test'})).status,403);
  const direct=await runTool(env,'biome_invoke',args());
  assert.equal(direct.isError,true);assert.equal(direct.structuredContent.status,403);
  assert.equal(calls.length,0);
  const token='t'.repeat(43),hash=createHash('sha256').update(token).digest('base64url');
  sql.prepare("INSERT INTO biome_oauth_tokens VALUES(?,'access','owner',?,?)").run(hash,base+'/api/mcp',Date.now()/1000+60);
  const result=await value(await send(args(),{Authorization:'Bearer '+token},'/api/mcp'));
  assert.equal(result.service_id,declaration.service_id);assert.equal(calls.length,1);
 });}finally{sql.close()}
});
test('runtime status cannot choose a controller, service, URL, local realm or provider context',async()=>{
 const {sql,send}=fixture();
 try{await fetched(async calls=>{
  for(const input of [
   {...args(),controller:mac},{...args(),realm:'local'},{...args(),provider_context:{}},
   {...args(),arguments:{service_id:'other'}},{...args(),arguments:{url:'https://elsewhere.test'}},
   {...args(),arguments:{operation:'configure'}},
   {operation:'controller.invoke',arguments:{controller:mac,operation:'controller.runtime_status',arguments:{}},request_id:'nested'}
  ]){
   const response=await send(input);assert.equal(response.status,200);
   assert.equal((await response.json()).result.isError,true);
  }
  assert.equal(calls.length,0);
 });}finally{sql.close()}
});
test('missing secret and invalid configured origins never cause an upstream request',async()=>{
 const {sql,env,send}=fixture();
 try{await fetched(async calls=>{
  delete env.STREAM_ROUTER_SECRET;
  assert.equal((await (await send()).json()).result.isError,true);
  env.STREAM_ROUTER_SECRET=secret;
  for(const invalid of ['http://coordinator.test','https://user:password@coordinator.test',
   'https://coordinator.test/extra','https://coordinator.test/?query=1','https://coordinator.test/#fragment']){
   env.SERVICE_AUTHORITY_ORIGIN=invalid;
   assert.equal((await (await send()).json()).result.isError,true);
  }
  assert.equal(calls.length,0);
 });}finally{sql.close()}
});
test('retained runtime declaration is used without changing custody or falling back to a host',async()=>{
 const {sql,env,send}=fixture();
 try{await fetched(async calls=>{
  await value(await send());
  delete env.CONTROLLER_RUNTIME_AUTHORITY;delete env.CONTROLLER_RUNTIME_REQUIRED;
  assert.equal((await value(await send())).authority,authority);
  env.CONTROLLER_RUNTIME_AUTHORITY=JSON.stringify({...declaration,authority:'9'.repeat(64)});
  const result=(await (await send()).json()).result;
  assert.equal(result.isError,true);assert.equal(result.structuredContent.status,409);
  assert.equal(calls.length,2);
 });}finally{sql.close()}
});
test('a missing service and an explicit manifest conflict remain visible diagnostics',async()=>{
 const {sql,send}=fixture();
 try{
  await fetched(async()=>{
   const result=await value(await send());assert.equal(result.service,null);
   assert.equal(result.coordinator.protocol,'biomerte-service-coordinator/1');
  },()=>Response.json({...observation(),services:{}}));
  await fetched(async()=>{
   const result=await value(await send());
   assert.equal(result.service.desired_manifest,null);
   assert.deepEqual(result.service.conflicting_revisions,[manifest.revision,'2'.repeat(64)]);
  },()=>{
   const result=observation();const row=result.services[declaration.service_id];
   row.desired_manifest=null;row.conflicting_revisions=[manifest.revision,'2'.repeat(64)];
   row.state='manifest_conflict';return Response.json(result);
  });
 }finally{sql.close()}
});
test('redirects, upstream errors and network exceptions never disclose credentials or response bodies',async()=>{
 const {sql,send}=fixture();
 try{
  const replies=[
   ()=>new Response(privateValue,{status:307,headers:{Location:'https://untrusted.test/'+privateValue}}),
   ()=>new Response(secret,{status:401}),
   ()=>new Response(privateValue,{status:503}),
   ()=>{throw Error(secret+' '+privateValue)},
   ()=>new Response(privateValue,{status:200})
  ];
  for(const reply of replies)await fetched(async calls=>{
   const response=await send();assert.equal(response.status,200);
   const result=(await response.json()).result;
   assert.equal(result.isError,true);assert.equal(calls.length,1);
   assert.equal(calls[0].redirect,'manual');
   assert.equal(JSON.stringify(result).includes(secret),false);
   assert.equal(JSON.stringify(result).includes(privateValue),false);
  },reply);
 }finally{sql.close()}
});
test('malformed or differently scoped coordinator results cannot masquerade as runtime status',async()=>{
 const {sql,send}=fixture();
 try{
  const wrongService=observation();wrongService.services[declaration.service_id].service_id='other';
  const wrongAuthority=observation();wrongAuthority.services[declaration.service_id].desired_manifest.authority.id='9'.repeat(64);
  const wrongRevision=observation();wrongRevision.services[declaration.service_id].desired_manifest.revision='9'.repeat(64);
  for(const bad of [null,{protocol:'other',services:{}},{protocol:'biomerte-service-coordinator/1',services:[]},
   wrongService,wrongAuthority,wrongRevision]){
   await fetched(async calls=>{
    const response=await send();const result=(await response.json()).result;
    assert.equal(result.isError,true);assert.equal(calls.length,1);
   },()=>Response.json(bad));
  }
 }finally{sql.close()}
});
