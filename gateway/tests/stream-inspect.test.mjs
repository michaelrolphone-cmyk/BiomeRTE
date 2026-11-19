import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto,createHmac} from 'node:crypto';
import {inspectStream} from '../lib/leased-stream.mjs';
globalThis.crypto ||= webcrypto;
const authority='a'.repeat(64), session='b'.repeat(32), secret='test-only-'.repeat(8);
const env={STREAM_ROUTER_SECRET:secret,STREAM_ROUTER_AUTHORITIES:authority};
function request(body={},signing=secret){
 const raw=JSON.stringify({authority,session,timestamp:Date.now()/1000,...body});
 return new Request('https://gateway/api/stream/inspect',{method:'POST',body:raw,
  headers:{Authorization:'Router '+createHmac('sha256',signing).update(raw).digest('hex')}});
}
test('authenticated inspection calls only the owning controller stream.inspect',async()=>{
 let calls=0;
 const r=await inspectStream(request(),env,async(_env,name,args)=>{
  calls++;assert.equal(name,'biome_invoke');assert.equal(args.controller,authority);
  assert.equal(args.operation,'stream.inspect');assert.deepEqual(args.arguments,{session});
  return {structuredContent:{id:session,state:'open'}};
 });
 assert.equal(r.status,200);assert.equal(calls,1);assert.equal(r.headers.get('Cache-Control'),'no-store');
});
test('forged, stale, unknown authority and malformed inspect never dispatch',async()=>{
 const invoke=()=>{throw Error('must not dispatch')};
 for(const [req,status] of [[request({},'wrong'),401],[request({timestamp:0}),400],
  [request({authority:'c'.repeat(64)}),403],[request({session:'invalid'}),400],
  [request({timestamp:'now'}),400]])assert.equal((await inspectStream(req,env,invoke)).status,status);
});
test('inspection fails closed for controller errors and missing authentication config',async()=>{
 assert.equal((await inspectStream(request(),env,async()=>({isError:true}))).status,409);
 assert.equal((await inspectStream(request(),{})).status,503);
 assert.equal((await inspectStream(new Request('https://gateway/api/stream/inspect'),env)).status,405);
});
