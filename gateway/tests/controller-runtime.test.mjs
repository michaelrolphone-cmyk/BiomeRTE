import test from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync,createHash,verify,webcrypto} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {controllerRuntimeConfig,controllerRuntimeTool,controllerRuntimeNative,controllerRuntimePair,pythonCanonical} from '../lib/controller-runtime.mjs';
globalThis.crypto??=webcrypto;
const authority='a'.repeat(64),mac='b'.repeat(64),pi='c'.repeat(64),other='d'.repeat(64);
const keys=generateKeyPairSync('rsa',{modulusLength:2048});
const actor=createHash('sha256').update(keys.publicKey.export({type:'spki',format:'der'})).digest('hex');
const declaration={service_id:'site-controller',authority,controller_aliases:[mac,pi],operator_actor:actor};
class DB{
 constructor(){this.retained=new Map();this.created=false}
 prepare(sql){const db=this;let args=[];return{
  bind(...values){args=values;return this},
  async first(){
   if(sql.startsWith('SELECT name FROM sqlite_master'))return db.created?{name:'controller_runtime_scopes'}:null;
   if(sql.startsWith('SELECT site'))return {site:'site'};
   if(sql.startsWith('SELECT declaration'))return db.retained.has(args[0])?{declaration:db.retained.get(args[0])}:null;
   throw Error('Unexpected query: '+sql);
  },
  async run(){
   if(sql.startsWith('CREATE TABLE')){db.created=true;return {}}
   if(sql.startsWith('INSERT INTO controller_runtime_scopes')){if(!db.retained.has(args[0]))db.retained.set(...args);return {}}
   throw Error('Unexpected mutation: '+sql);
  }
 }}
}
const env=()=>({DB:new DB(),CONTROLLER_RUNTIME_AUTHORITY:JSON.stringify(declaration),
 SERVICE_AUTHORITY_ORIGIN:'https://authority.example',CONTROLLER_RUNTIME_SIGNING_KEY:keys.privateKey.export({type:'pkcs8',format:'pem'})});
const envelope=controller=>({site:'site',realm:'site',operation:'provider.poll',
 arguments:{provider:other},id:'stable-request-id',...(controller?{controller}:{}),auth:{actor:other,signature:'YQ=='}});
async function fetched(fn,response=()=>Response.json({result:{ok:true}})){
 const original=globalThis.fetch,calls=[];
 globalThis.fetch=async(url,init)=>{calls.push({url,...init});return response(url,init)};
 try{return await fn(calls)}finally{globalThis.fetch=original}
}
test('disabled runtime preserves native routing without network calls',async()=>{
 await fetched(async calls=>{
  assert.equal(await controllerRuntimeConfig({}),null);
  assert.equal(await controllerRuntimeNative({DB:new DB()},JSON.stringify(envelope(pi))),null);
  assert.equal(calls.length,0);
 });
});
test('both aliases and an absent provider selector route to the same logical runtime without byte changes',async()=>{
 await fetched(async calls=>{
  const e=env();
  for(const selected of [mac,pi,authority,undefined]){
   const raw=JSON.stringify(envelope(selected)).replace('"arguments":{','"arguments":{"number":1.0,"large":9007199254740993,');
   assert.equal((await controllerRuntimeNative(e,raw)).status,200);
   assert.equal(calls.at(-1).body,raw);
   assert.equal(calls.at(-1).url,'https://authority.example/api/controller-runtimes/'+authority+'/call');
   assert.equal(calls.at(-1).redirect,'error');
  }
 });
});
test('recorded native dispatch origin remains valid across another alias',async()=>{
 await fetched(async calls=>{
  const raw=JSON.stringify(envelope(mac));
  assert.equal((await controllerRuntimeNative(env(),raw,pi)).status,200);
  assert.equal(calls[0].body,raw);
 });
});
test('unrelated logical services remain outside controller scope',async()=>{
 await fetched(async calls=>{
  assert.equal(await controllerRuntimeNative(env(),JSON.stringify(envelope(other))),null);
  assert.equal(calls.length,0);
 });
});
test('wrong site and realm are rejected before network forwarding',async()=>{
 await fetched(async calls=>{
  assert.equal((await controllerRuntimeNative(env(),JSON.stringify({...envelope(pi),site:'foreign'}))).status,403);
  assert.equal((await controllerRuntimeNative(env(),JSON.stringify({...envelope(pi),realm:'local'}))).status,400);
  assert.equal(calls.length,0);
 });
});
test('failed authority response never attempts native fallback',async()=>{
 await fetched(async calls=>{
  const r=await controllerRuntimeNative(env(),JSON.stringify(envelope(pi)));
  assert.equal(r.status,503);assert.equal(calls.length,1);
 },()=>new Response('',{status:503}));
});
test('network failure stays explicit and tells caller to reuse original request',async()=>{
 await fetched(async calls=>{
  const r=await controllerRuntimeNative(env(),JSON.stringify(envelope(pi)));
  assert.equal(r.status,503);assert.equal((await r.json()).retry_same_id,true);assert.equal(calls.length,1);
 },()=>{throw Error('offline')});
});
test('retained custody survives removal of both deployment flags',async()=>{
 const e=env();await controllerRuntimeConfig(e);
 const replacement={DB:e.DB,SERVICE_AUTHORITY_ORIGIN:e.SERVICE_AUTHORITY_ORIGIN};
 assert.equal((await controllerRuntimeConfig(replacement)).authority,authority);
 delete e.CONTROLLER_RUNTIME_AUTHORITY;
 assert.equal((await controllerRuntimeConfig(e)).authority,authority);
});
test('retained logical identity cannot change silently',async()=>{
 const e=env();await controllerRuntimeConfig(e);
 await assert.rejects(controllerRuntimeConfig({...e,CONTROLLER_RUNTIME_AUTHORITY:JSON.stringify({...declaration,authority:other})}),
  error=>error.status===409);
});
test('required runtime with missing config fails closed',async()=>{
 await assert.rejects(controllerRuntimeConfig({DB:new DB(),CONTROLLER_RUNTIME_REQUIRED:'1',SERVICE_AUTHORITY_ORIGIN:'https://authority.example'}));
});
test('unsafe origin and duplicate aliases fail before network',async()=>{
 await fetched(async calls=>{
  assert.equal((await controllerRuntimeNative({...env(),SERVICE_AUTHORITY_ORIGIN:'http://authority.example'},JSON.stringify(envelope(pi)))).status,503);
  assert.equal((await controllerRuntimeNative({...env(),CONTROLLER_RUNTIME_AUTHORITY:JSON.stringify({...declaration,controller_aliases:[pi,pi]})},JSON.stringify(envelope(pi)))).status,503);
  assert.equal(calls.length,0);
 });
});
test('only owner tool path signs; original request ID and selected alias are preserved',async()=>{
 const args={operation:'work.create',arguments:{title:'Example'},request_id:'request-original',controller:pi};
 await fetched(async calls=>{
  const r=await controllerRuntimeTool(env(),'biome_invoke',args);
  assert.equal(r.isError,undefined);
  const signed=JSON.parse(calls[0].body),auth=signed.auth;delete signed.auth;
  assert.equal(signed.id,args.request_id);assert.equal(signed.controller,pi);
  assert.equal(auth.actor,actor);
  assert.equal(verify('RSA-SHA256',Buffer.from(pythonCanonical(signed)),keys.publicKey,Buffer.from(auth.signature,'base64')),true);
  assert.deepEqual(signed.arguments,args.arguments);
 });
});
test('native actor is never elevated with the operator signer',async()=>{
 await fetched(async calls=>{
  const raw=JSON.stringify(envelope(pi));
  await controllerRuntimeNative(env(),raw);
  assert.equal(JSON.parse(calls[0].body).auth.actor,other);
  assert.equal(JSON.parse(calls[0].body).auth.signature,'YQ==');
 });
});
test('local maintenance and physical gateway operations retain their separate route',async()=>{
 await fetched(async calls=>{
  for(const a of [{realm:'local',operation:'work.list'},{operation:'maintenance.status'},{operation:'controller.status'}])
   assert.equal(await controllerRuntimeTool(env(),'biome_invoke',a),null);
  assert.equal(calls.length,0);
 });
});
test('pairing is URL-scoped and never an approval route',async()=>{
 await fetched(async calls=>{
  const raw='{"label":"untrusted","public_key":"example"}';
  assert.equal((await controllerRuntimePair(env(),'begin',raw,pi)).status,200);
  assert.equal(calls[0].url,'https://authority.example/api/controller-runtimes/'+authority+'/pair/begin');
  assert.equal(calls[0].body,raw);
  assert.equal((await controllerRuntimePair(env(),'approve',raw,pi)).status,400);
  assert.equal(calls.length,1);
 });
});
test('signing canonical form matches Python for real wire JSON, including numeric keys and Unicode',()=>{
 const cases=[
  {z:'é😀\u007f\n',a:'text',n:0.000001,s:1e21,b:9007199254740992},
  {'10':10,'2':2,'\uffff':1,'😀':2,'a':null},
  [1e-7,1e-6,0.0001,1.2345678901234567e-6,1.2e21,-0,-1e-8,1.2345678901234568e21],
  envelope(pi)
 ];
 for(const value of cases){
  const p=spawnSync('python3',['-c','import sys,json;print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(",",":"),allow_nan=False))'],{input:JSON.stringify(value),encoding:'utf8'});
  assert.equal(p.status,0,p.stderr);assert.equal(pythonCanonical(value),p.stdout.trim());
 }
});
