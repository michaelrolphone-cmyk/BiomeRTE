import test from 'node:test';
import assert from 'node:assert/strict';
import {leasedStream} from '../lib/leased-stream.mjs';

const authorization='Stream grant.'+'a'.repeat(64);
const path='1'.repeat(32)+'/vision';
function request(headers={}) {
 return new Request('https://gateway.test/api/stream/'+path,{
  headers:{Upgrade:'websocket',Authorization:authorization,...headers}
 });
}

test('private router binding preserves upgrade, grant and exact lane',async()=>{
 const response={status:101,webSocket:{}};
 let forwarded;
 const actual=await leasedStream(request(),{
  CUSTOMER_HTTP_STREAM_ROUTER:{fetch:async req=>{forwarded=req;return response}},
  STREAM_ROUTER:{fetch(){throw Error('must prefer private Mac route')}}
 });
 assert.equal(actual,response);
 assert.equal(forwarded.url,'http://stream-router/'+path);
 assert.equal(forwarded.headers.get('Upgrade'),'websocket');
 assert.equal(forwarded.headers.get('Authorization'),authorization);
});

test('invalid public attachments never reach the private binding',async()=>{
 let calls=0;
 const env={CUSTOMER_HTTP_STREAM_ROUTER:{fetch(){calls++}}};
 assert.equal((await leasedStream(request({Authorization:'invalid'}),env)).status,401);
 assert.equal((await leasedStream(request({Origin:'https://other.test'}),env)).status,403);
 assert.equal((await leasedStream(request({Upgrade:'other'}),env)).status,400);
 assert.equal(calls,0);
});

test('missing persistent transport stays explicit',async()=>{
 assert.equal((await leasedStream(request(),{})).status,503);
});

test('public Heroku upgrade uses Workers-supported manual redirect mode',async()=>{
 const previous=globalThis.fetch;let status=101;
 globalThis.fetch=async(req,options)=>{
  assert.equal(req.url,'https://router.herokuapp.com/'+path);
  assert.equal(req.headers.get('Authorization'),authorization);
  assert.equal(req.headers.get('Upgrade'),'websocket');
  assert.equal(options.redirect,'manual');return {status};
 };
 try{
  assert.equal((await leasedStream(request(),{STREAM_ROUTER_ORIGIN:'https://router.herokuapp.com'})).status,101);
  status=302;
  assert.equal((await leasedStream(request(),{STREAM_ROUTER_ORIGIN:'https://router.herokuapp.com'})).status,502);
 }finally{globalThis.fetch=previous}
});
