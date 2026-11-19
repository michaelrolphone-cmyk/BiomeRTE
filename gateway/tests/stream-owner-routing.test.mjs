import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {createHash,createHmac} from 'node:crypto';
import {sqliteD1} from './sqlite-d1.mjs';
import gateway from '../worker/index.js';
import {providerSocket} from '../lib/transport.mjs';

const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const sha=value=>createHash('sha256').update(value).digest('hex');
const base='https://biome.test';
const controllers={mac:'a'.repeat(64),pi:'b'.repeat(64)};
const tokens={mac:'1'.repeat(64),pi:'2'.repeat(64)};
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'test',1);INSERT INTO biome_owner VALUES(1,'owner')");
 for(const [id,token] of Object.entries(tokens))sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,?,2,?,1)').run(id,sha(token),id==='mac'?0:1,Date.now()/1000);
 const token='s'.repeat(43),auth={Authorization:'Bearer '+token};
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)")
  .run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+300);
 const env={DB:sqliteD1(sql),BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',BIOME_RECEIVER_WRAP_KEY:Buffer.alloc(32,11).toString('base64'),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',CONTROLLER_NODE_MAP:JSON.stringify(Object.fromEntries(Object.entries(controllers).map(([id,identity])=>[identity,id])))};
 const send=(path,body={},headers=auth)=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'content-type':'application/json',...headers},body:JSON.stringify(body)}),env);
 const device=(path,body={},node='pi')=>send('/bridge/'+path,body,{Authorization:'Bearer '+tokens[node]});
 const event=(method,params,headers=auth)=>send('/api/mcp',{jsonrpc:'2.0',id:1,method,params},headers);
 return {sql,env,send,device,event,auth};
}
async function next(f,node='pi'){
 for(let attempt=0;attempt<300;attempt++){
  const job=await (await f.device('next',{},node)).json();
  if(job.id)return job;
  await pause(5);
 }
 assert.fail('Expected an owner-routed job');
}
async function finish(f,job,value,node='pi'){
 const result={content:[{type:'text',text:JSON.stringify(value)}]};
 assert.equal((await f.device('complete',{id:job.id,lease:job.lease,result},node==='pi'?'mac':'pi')).status,409,'Other controller cannot complete the selected owner job');
 assert.equal((await f.device('complete',{id:job.id,lease:job.lease,result},node)).status,200);
}
function exchange(controller=controllers.pi){return {controller,contract:'chat.private.exchange:2',resource:'privatechat.mac',envelope:{id:'stable-exchange',kind:'hello'}}}

test('explicit nondefault exchange submit, live status, lost reply retry and rejection stay on the owner',async()=>{
 const f=fixture();
 try{
  // The selected Pi remains usable when the default Mac is unavailable.
  f.sql.prepare("UPDATE controller_nodes SET ready=0 WHERE id='mac'").run();
  let submitId;
  for(let retry=0;retry<2;retry++){
   const response=await f.send('/api/exchange',exchange()),text=response.text();
   if(!retry){
    const submit=await next(f);submitId=submit.id;
    assert.equal(submit.request.arguments.controller,controllers.pi);
    assert.equal(submit.request.arguments.controller_owner,'pi');
    assert.equal(submit.request.arguments.operation,'cap.submit');
    await finish(f,submit,{id:'stable-exchange',state:'queued'});
   }
   // A repeated request_id still reads current status rather than the old receipt.
   const status=await next(f);
   assert.equal(status.request.arguments.operation,'cap.submission');
   assert.equal(status.request.arguments.controller,controllers.pi);
   assert.equal(status.request.arguments.controller_owner,'pi');
   await finish(f,status,{id:'stable-exchange',state:'succeeded'});
   const frames=(await text).trim().split('\n').map(JSON.parse);
   assert.equal(frames.at(-1).status.state,'succeeded');
  }
  assert.equal(f.sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE json_extract(request,'$.arguments.operation')='cap.submit'").get().n,1);
  assert.equal(f.sql.prepare('SELECT state FROM bridge_jobs WHERE id=?').get(submitId).state,'done');
  const conflict=await f.send('/api/exchange',{...exchange(),resource:'different-resource'});
  assert.equal((await conflict.text()).trim().split('\n').map(JSON.parse).at(-1).type,'error');
  assert.equal(f.sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE json_extract(request,'$.arguments.controller_owner')='mac'").get().n,0);
 }finally{f.sql.close()}
});

test('invalid or unknown stream selectors and revoked OAuth never enqueue a default-owner request',async()=>{
 const f=fixture();
 try{
  for(const controller of ['pi','c'.repeat(64),null,{}])assert.ok((await f.send('/api/exchange',exchange(controller))).status>=400);
  f.sql.exec("DELETE FROM biome_oauth_tokens");
  assert.equal((await f.send('/api/exchange',exchange())).status,401);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});

test('unavailable explicit owner cannot be claimed by default and resumes only on its own link',async()=>{
 const f=fixture();
 try{
  f.sql.prepare("UPDATE controller_nodes SET ready=0 WHERE id='pi'").run();
  const response=await f.send('/api/exchange',exchange()),text=response.text();
  for(let i=0;i<100&&!f.sql.prepare('SELECT id FROM bridge_jobs').get();i++)await pause(5);
  assert.equal((await (await f.device('next',{},'mac')).json()).idle,true);
  assert.equal((await (await f.device('next',{},'pi')).json()).idle,true);
  f.sql.prepare("UPDATE controller_nodes SET ready=1 WHERE id='pi'").run();
  const submit=await next(f);await finish(f,submit,{state:'queued'});
  const status=await next(f);await finish(f,status,{state:'succeeded'});
  assert.equal((await text).trim().split('\n').map(JSON.parse).at(-1).status.state,'succeeded');
 }finally{f.sql.close()}
});

const subscription=controller=>({controller,name:'workflow.delegation.requested',arguments:{target:'lmstudio',realm:'site'},delivery:{mode:'webhook',url:'https://callback.test/events',secret:'whsec_'+Buffer.alloc(32).toString('base64')},ttlMs:60000,cursor:'0'});
test('subscription registration, renewal, retry, unsubscribe and lease events preserve nondefault owner',async()=>{
 const f=fixture();
 try{
  f.sql.prepare("UPDATE controller_nodes SET ready=0 WHERE id='mac'").run();
  for(const [method,params] of [
   ['events/subscribe',subscription(controllers.pi)],
   ['events/subscribe',{...subscription(controllers.pi),ttlMs:120000}],
   ['events/subscribe',subscription(controllers.pi)],
   ['events/unsubscribe',subscription(controllers.pi)],
   ['events/subscribe',{...subscription(controllers.pi),name:'lease.available',arguments:{reply_to:'inbox',realm:'local'}}]
  ]){
   const pending=f.event(method,params),job=await next(f);
   assert.equal(job.request.name,'mcp_event_change');
   assert.equal(job.request.arguments.controller,controllers.pi);
   assert.equal(job.request.arguments.controller_owner,'pi');
   assert.equal(job.request.arguments.owner,'owner');assert.equal(job.request.arguments.grant,'grant');
   assert.equal(job.request.arguments.method,method);assert.deepEqual(job.request.arguments.params,params);
   await finish(f,job,{result:{id:'same-owner-subscription'}});
   assert.deepEqual((await (await pending).json()).result,{id:'same-owner-subscription'});
   assert.equal(f.sql.prepare('SELECT request FROM bridge_jobs WHERE id=?').get(job.id).request,'{}','Completed subscription secrets are scrubbed');
  }
 }finally{f.sql.close()}
});

test('subscription omission retains default routing, malformed selectors reject and OAuth stays enforced',async()=>{
 const f=fixture();
 try{
  const params=subscription(undefined);delete params.controller;
  const pending=f.event('events/subscribe',params),job=await next(f,'mac');
  assert.equal(job.request.arguments.controller,undefined);assert.equal(job.request.arguments.params.controller,undefined);
  assert.equal(job.request.arguments.controller_owner,'mac');await finish(f,job,{result:{}},'mac');await pending;
  for(const controller of [null,'pi','c'.repeat(64),{}]){
   const value=await (await f.event('events/subscribe',subscription(controller))).json();assert.equal(value.error.code,-32602);
  }
  assert.equal((await f.event('events/subscribe',subscription(controllers.pi),{})).status,401);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);
 }finally{f.sql.close()}
});

async function callback(f,connection,id,body){
 const ts=String(Math.floor(Date.now()/1000)),key=Buffer.from(connection.secret.slice(6),'base64');
 const signature=createHmac('sha256',key).update(id+'.'+ts+'.'+JSON.stringify(body)).digest('base64');
 return f.send(new URL(connection.url).pathname,body,{'webhook-id':id,'webhook-timestamp':ts,'webhook-signature':'v1,'+signature});
}
test('callback mailbox ownership separates same event IDs, retries and acknowledgments across controllers',async()=>{
 const f=fixture(),root='/agent-events/lmstudio/';
 try{
  const boxes={};
  for(const [node,controller] of Object.entries(controllers))boxes[node]=await (await f.send(root+'connect',{controller})).json();
  const old=await (await f.send(root+'connect',{})).json();
  assert.equal((await (await f.send(root+'connect',{controller:controllers.pi})).json()).url,boxes.pi.url);
  assert.equal(new Set([boxes.mac.url,boxes.pi.url,old.url]).size,3);
  const ev={eventId:'delegate_same',name:'workflow.delegation.requested',data:{delegation_id:'same'}};
  assert.equal((await callback(f,boxes.pi,'verify_1',{type:'verification',challenge:'challenge'})).status,200);
  assert.equal((await callback(f,boxes.pi,ev.eventId,ev)).status,409);
  assert.equal((await callback(f,boxes.pi,ev.eventId,{...ev,controller:controllers.mac})).status,409);
  for(const [node,controller] of Object.entries(controllers)){
   const event={...ev,controller};
   assert.equal((await callback(f,boxes[node],ev.eventId,event)).status,200);
   assert.equal((await callback(f,boxes[node],ev.eventId,event)).status,200);
   assert.deepEqual((await (await f.send(root+'next',{controller})).json()).events,[event]);
  }
  assert.equal((await callback(f,old,ev.eventId,ev)).status,200,'Legacy envelopes remain valid');
  await f.send(root+'ack',{controller:controllers.mac},{...f.auth,'x-event-id':ev.eventId});
  assert.deepEqual((await (await f.send(root+'next',{controller:controllers.mac})).json()).events,[]);
  assert.deepEqual((await (await f.send(root+'next',{controller:controllers.pi})).json()).events,[{...ev,controller:controllers.pi}]);
  assert.deepEqual((await (await f.send(root+'next',{})).json()).events,[ev]);
  for(const action of ['connect','next','ack']){
   assert.equal((await f.send(root+action,{controller:'c'.repeat(64)})).status,409);
   assert.equal((await f.send(root+action,{controller:controllers.pi},{})).status,401);
  }
  f.sql.exec('DELETE FROM biome_oauth_tokens');
  assert.equal((await f.send(root+'next',{controller:controllers.pi})).status,401);
 }finally{f.sql.close()}
});

test('provider held polls preserve explicit controller on every live retry and reject unknown selectors',async()=>{
 const f=fixture(),previousResponse=globalThis.Response,previousPair=globalThis.WebSocketPair;
 class Socket{
  constructor(){this.listeners={};this.frames=[]}
  accept(){}
  addEventListener(name,listener){(this.listeners[name]??=[]).push(listener)}
  send(frame){this.frames.push(JSON.parse(frame))}
  receive(frame){for(const listener of this.listeners.message??[])listener({data:JSON.stringify(frame)})}
  close(){for(const listener of this.listeners.close??[])listener({})}
 }
 const client=new Socket(),server=new Socket();
 globalThis.WebSocketPair=class{constructor(){this[0]=client;this[1]=server}};
 globalThis.Response=class extends previousResponse{constructor(body,options){if(options?.status===101){super(null);this.webSocket=options.webSocket}else super(body,options)}};
 try{
  await providerSocket(new Request(base+'/api/provider/connect',{headers:{...f.auth,Upgrade:'websocket'}}),f.env,f.env);
  server.receive({type:'invoke',id:'provider-poll',arguments:{controller:controllers.pi,operation:'provider.poll',arguments:{provider:'test'},request_id:'same-poll'}});
  for(let turn=0;turn<2;turn++){
   const job=await next(f);
   assert.equal(job.request.arguments.controller,controllers.pi);assert.equal(job.request.arguments.controller_owner,'pi');
   assert.equal(job.request.arguments.operation,'provider.poll');
   await finish(f,job,{actions:turn?[{id:'work'}]:[]});
  }
  for(let i=0;i<100&&!server.frames.some(frame=>frame.id==='provider-poll');i++)await pause(5);
  assert.equal(JSON.parse(server.frames.find(frame=>frame.id==='provider-poll').result.content[0].text).actions.length,1);
  server.receive({type:'invoke',id:'invalid',arguments:{controller:'c'.repeat(64),operation:'provider.poll',arguments:{},request_id:'invalid'}});
  for(let i=0;i<100&&!server.frames.some(frame=>frame.id==='invalid');i++)await pause(5);
  assert.equal(server.frames.find(frame=>frame.id==='invalid').result.isError,true);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,2);
 }finally{
  server.close();await pause(300);globalThis.Response=previousResponse;
  if(previousPair===undefined)delete globalThis.WebSocketPair;else globalThis.WebSocketPair=previousPair;
  f.sql.close();
 }
});
