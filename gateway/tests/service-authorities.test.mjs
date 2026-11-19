import test from 'node:test';
import assert from 'node:assert/strict';
import {logicalServiceCall} from '../lib/service-authorities.mjs';
const actor='a'.repeat(64),authority='b'.repeat(64),native='c'.repeat(64);
const raw='{"site":"site","realm":"site","operation":"provider.poll","arguments":{"provider":"'+actor+'","number":1.0,"large":9007199254740993},"id":"op","auth":{"actor":"'+actor+'","signature":"YQ=="}}';
class DB{
 constructor(){this.rows=new Map()}
 prepare(sql){const db=this;let args=[];return {
  bind(...v){args=v;return this},
  async first(){if(sql.startsWith('SELECT site'))return {site:'site'};
   if(sql.startsWith('SELECT service_id'))return db.rows.get(args.join('|'))??null;
   throw Error('Unexpected query')},
  async run(){if(sql.startsWith('CREATE TABLE'))return {};
   if(sql.startsWith('INSERT INTO logical_service_scopes')){
    const key=args.slice(0,3).join('|');if(!db.rows.has(key))db.rows.set(key,{service_id:args[3],authority:args[4]});return {}}
   throw Error('Unexpected mutation')}
 }}}
const env=()=>({DB:new DB(),SERVICE_AUTHORITY_ORIGIN:'https://service.example',
 CONTROLLER_NODE_MAP:JSON.stringify({[native]:'native'}),
 SERVICE_AUTHORITY_DECLARATIONS:JSON.stringify([{service_id:'relay-authority',authority,providers:[actor]}])});
function route(){return Response.json({service_id:'relay-authority',authority,effect_ready:false})}
test('disabled feature and existing native authority preserve native path',async()=>{
 const before=globalThis.fetch;globalThis.fetch=()=>{throw Error('Unexpected service lookup')};
 try{
  assert.equal(await logicalServiceCall({}, {provider:actor},raw),null);
  assert.equal(await logicalServiceCall(env(), {authority:native},raw),null);
 }finally{globalThis.fetch=before}
});
test('unrelated native provider has no witness dependency',async()=>{
 const before=globalThis.fetch;globalThis.fetch=async()=>{throw Error('Witness must not be contacted')};
 try{assert.equal(await logicalServiceCall(env(),{provider:'d'.repeat(64)},raw),null)}
 finally{globalThis.fetch=before}
});
test('declared provider fails closed before any successful witness lookup',async()=>{
 const before=globalThis.fetch;globalThis.fetch=async()=>new Response('',{status:404});
 try{assert.equal((await logicalServiceCall(env(),{provider:actor},raw)).status,503)}
 finally{globalThis.fetch=before}
});
test('declared provisional authority forwards exact original signed bytes',async()=>{
 const before=globalThis.fetch,calls=[];globalThis.fetch=async(url,init)=>{
  calls.push({url,...init});return calls.length===1?route():Response.json({result:{fences:[]}})};
 try{
  const response=await logicalServiceCall(env(),{provider:actor},raw);
  assert.equal(response.status,200);assert.equal(calls[1].body,raw);
  assert.equal(calls[1].redirect,'error');
  assert.equal(calls[1].url,'https://service.example/api/service-authorities/call');
 }finally{globalThis.fetch=before}
});
test('known logical scope stays unavailable after witness404',async()=>{
 const before=globalThis.fetch,e=env();let count=0;
 globalThis.fetch=async()=>++count===1?route():Response.json({result:{}});
 try{
  assert.equal((await logicalServiceCall(e,{provider:actor},raw)).status,200);
  delete e.SERVICE_AUTHORITY_DECLARATIONS;
  globalThis.fetch=async()=>new Response('',{status:404});
  assert.equal((await logicalServiceCall(e,{provider:actor},raw)).status,503);
 }finally{globalThis.fetch=before}
});
test('transport and declared503 failures never fall back',async()=>{
 const before=globalThis.fetch;
 try{
  globalThis.fetch=async()=>new Response('',{status:503});
  assert.equal((await logicalServiceCall(env(),{provider:actor},raw)).status,503);
  globalThis.fetch=async()=>{throw Error('offline')};
  assert.equal((await logicalServiceCall(env(),{provider:actor},raw)).status,503);
 }finally{globalThis.fetch=before}
});
test('signed different controller is rejected without rewriting or call',async()=>{
 const before=globalThis.fetch;let calls=0;
 globalThis.fetch=async()=>{calls++;return route()};
 try{
  const body=JSON.stringify({...JSON.parse(raw),controller:native});
  assert.equal((await logicalServiceCall(env(),{provider:actor},body)).status,409);
  assert.equal(calls,1);
 }finally{globalThis.fetch=before}
});
test('logical consumer controller is resolved and forwarded',async()=>{
 const before=globalThis.fetch;let calls=0;
 const body=JSON.stringify({...JSON.parse(raw),controller:authority,operation:'cap.acquire'});
 globalThis.fetch=async(url,init)=>{calls++;if(calls===1){
  assert.deepEqual(JSON.parse(init.body),{authority});return route()}
  assert.equal(init.body,body);return Response.json({result:{id:'lease'}})};
 try{assert.equal((await logicalServiceCall(env(),{authority},body)).status,200)}
 finally{globalThis.fetch=before}
});
test('retained scope cannot silently change logical service identity',async()=>{
 const before=globalThis.fetch,e=env();let n=0;
 globalThis.fetch=async()=>++n===1?route():Response.json({result:{}});
 try{
  await logicalServiceCall(e,{provider:actor},raw);
  globalThis.fetch=async()=>Response.json({service_id:'other-service',authority});
  assert.equal((await logicalServiceCall(e,{provider:actor},raw)).status,409);
 }finally{globalThis.fetch=before}
});
test('wrong site and unsafe origin fail without forwarding',async()=>{
 const before=globalThis.fetch;globalThis.fetch=()=>{throw Error('Should not forward')};
 try{
  assert.equal((await logicalServiceCall(env(),{provider:actor},raw.replace('"site":"site"','"site":"other"'))).status,403);
  assert.equal((await logicalServiceCall({...env(),SERVICE_AUTHORITY_ORIGIN:'http://service.example'},{provider:actor},raw)).status,503);
 }finally{globalThis.fetch=before}
});
