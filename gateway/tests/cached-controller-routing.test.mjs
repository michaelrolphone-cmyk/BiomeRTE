import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import gateway from '../worker/index.js';
import {sqliteD1} from './sqlite-d1.mjs';

const base='https://biome.test',pi='b'.repeat(64),mac='a'.repeat(64);
const tokens={pi:'2'.repeat(64),mac:'1'.repeat(64)};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'test',1);INSERT INTO biome_owner VALUES(1,'owner')");
 for(const [node,token] of Object.entries(tokens))sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,?,2,?,1)').run(node,createHash('sha256').update(token).digest('hex'),node==='mac'?0:1,Date.now()/1000);
 const token='o'.repeat(43),auth={Authorization:'Bearer '+token};
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)")
  .run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+300);
 const env={DB:sqliteD1(sql),BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',CONTROLLER_NODE_MAP:JSON.stringify({[mac]:'mac',[pi]:'pi'})};
 const send=(path,body,headers=auth)=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'content-type':'application/json',...headers},body:JSON.stringify(body)}),env);
 const invoke=(args,headers=auth)=>send('/api/mcp',{jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'biome_invoke',arguments:args}},headers);
 const device=(path,body={},node='pi')=>send('/bridge/'+path,body,{Authorization:'Bearer '+tokens[node]});
 return {sql,env,invoke,device};
}
const route=(operation,arguments_={},id='original-id',controller=pi,realm)=>({operation:'controller.invoke',arguments:{controller,operation,arguments:arguments_,...(realm===undefined?{}:{realm})},request_id:id});
function cachedSchema(call){
 assert.deepEqual(Object.keys(call).filter(key=>!['operation','arguments','request_id','realm'].includes(key)),[]);
 assert.equal(typeof call.operation,'string');assert.equal(typeof call.request_id,'string');assert.ok(call.arguments&&typeof call.arguments==='object'&&!Array.isArray(call.arguments));
}
async function next(f,node='pi'){
 for(let n=0;n<200;n++){
  const job=await (await f.device('next',{},node)).json();
  if(job.id)return job;
  await sleep(5);
 }
 assert.fail('Expected owner-routed job');
}
async function complete(f,job,result,node='pi'){
 assert.equal((await f.device('complete',{id:job.id,lease:job.lease,result},node==='pi'?'mac':'pi')).status,409);
 assert.equal((await f.device('complete',{id:job.id,lease:job.lease,result},node)).status,200);
}
function value(result){return JSON.parse(result.content[0].text)}
const answer=body=>({content:[{type:'text',text:JSON.stringify(body)}],structuredContent:body});

test('cached schema catalog to selected discovery, acquire and live status keeps original identities and Pi owner',async()=>{
 const f=fixture();
 try{
  f.sql.prepare("UPDATE controller_nodes SET ready=0 WHERE id='mac'").run();
  const catalogCall={operation:'system.discovery',arguments:{section:'controllers'},request_id:'catalog'};cachedSchema(catalogCall);
  const catalog=value((await(await f.invoke(catalogCall)).json()).result);
  let call=catalog.controllers.find(row=>row.controller===pi).next_call;
  const nextCalls=[
   {operation:'cap.acquire',arguments:{contract:'test:1'},request_id:'acquire-original',realm:'site'},
   {operation:'cap.status',arguments:{command:'command-original'},request_id:'status-original',realm:'site'},
   {operation:'cap.status',arguments:{command:'command-original'},request_id:'status-original',realm:'site'}
  ];
  const jobs=[];
  for(let step=0;step<4;step++){
   cachedSchema(call);assert.equal(call.operation,'controller.invoke');assert.equal(call.arguments.controller,pi);
   const pending=f.invoke(call),job=await next(f);jobs.push(job);
   const args=job.request.arguments;
   assert.equal(args.operation,step===0?'system.discovery':step===1?'cap.acquire':'cap.status');
   assert.equal(args.controller,pi);assert.equal(args.controller_owner,'pi');
   assert.equal(args.request_id,call.request_id,'Envelope request_id passes through unchanged');
   assert.deepEqual(args.arguments,call.arguments.arguments);
   const body=step===0?{section:'offers',items:[],complete:true,next_call:nextCalls[step]}:
    step===3?{state:'succeeded'}:{state:'queued',next_call:nextCalls[step]};
   await complete(f,job,answer(body));
   const result=(await(await pending).json()).result,payload=value(result);
   assert.deepEqual(result.structuredContent,payload,'Both MCP representations expose the same pinned continuation');
   if(step===0)assert.equal(payload.controller,pi);
   if(step<3){call=payload.next_call;assert.equal(call.request_id,nextCalls[step].request_id);}
  }
  assert.notEqual(jobs[2].id,jobs[3].id,'Repeated status IDs remain live reads');
  assert.equal(f.sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE json_extract(request,'$.arguments.controller_owner')='mac'").get().n,0);
 }finally{f.sql.close()}
});

test('wrapper mutation retries share exact native receipt with modern selectors and reject changed input',async()=>{
 const f=fixture();
 try{
  const call=route('cap.acquire',{contract:'test:1'},'same-mutation',pi,'local'),pending=f.invoke(call),job=await next(f);
  assert.equal(job.request.arguments.realm,'local');assert.equal(job.request.arguments.request_id,'same-mutation');
  await complete(f,job,answer({id:'lease',controller:{ordinary:'business data'}}));
  const first=(await(await pending).json()).result;
  assert.deepEqual((await(await f.invoke(call)).json()).result,first);
  const modern={controller:pi,operation:'cap.acquire',arguments:{contract:'test:1'},request_id:'same-mutation',realm:'local'};
  assert.deepEqual((await(await f.invoke(modern)).json()).result,first,'Wire spelling does not change native request identity');
  const conflict=route('cap.acquire',{contract:'different:1'},'same-mutation',pi,'local');
  assert.equal((await(await f.invoke(conflict)).json()).result.isError,true);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);
 }finally{f.sql.close()}
});

test('cached selected discovery aliases preserve request ID on repeated snapshots',async()=>{
 const f=fixture();
 try{
  const call=route('discovery.page',{section:'offers'},'exact-page'),pending=f.invoke(call),job=await next(f);
  assert.equal(job.request.arguments.operation,'system.discovery');assert.equal(job.request.arguments.request_id,'exact-page');
  await complete(f,job,answer({section:'offers',items:[],complete:true}));await pending;
  assert.equal(value((await(await f.invoke(call)).json()).result).controller,pi);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);
 }finally{f.sql.close()}
});

test('unknown selection, recursive routing, ambiguous realms and redirected management reject without queueing',async()=>{
 const f=fixture();
 try{
  const calls=[
   route('cap.acquire',{},'unknown','c'.repeat(64)),route('cap.acquire',{},'label','pi'),
   route('controller.invoke',route('cap.acquire').arguments),route('controller.diagnostics'),
   route('maintenance.submit'),route('grok.receiver.register'),route('system.discovery',{section:'controllers'}),
   {...route('cap.acquire',{},'conflict',pi,'local'),realm:'site'},
   {...route('cap.acquire'),controller:pi},
   {...route('cap.acquire'),arguments:{...route('cap.acquire').arguments,request_id:'inner-id'}},
   {...route('cap.acquire'),arguments:{...route('cap.acquire').arguments,controller_owner:'mac'}},
   route('cap.acquire',null),route('cap.acquire',[]),route('cap.acquire',{},'realm',pi,'other')
  ];
  for(const call of calls){const result=(await(await f.invoke(call)).json()).result;assert.equal(result.isError,true,JSON.stringify(call));}
  assert.equal((await f.invoke(route('cap.acquire'),{})).status,401);
  f.sql.exec('DELETE FROM biome_oauth_tokens');assert.equal((await f.invoke(route('cap.acquire'))).status,401);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});

test('selected unavailable Pi never falls back and wrong-owner continuations fail closed',async()=>{
 const f=fixture();
 try{
  f.sql.prepare("UPDATE controller_nodes SET ready=0 WHERE id='pi'").run();
  const pending=f.invoke(route('cap.acquire'));
  for(let n=0;n<100&&!f.sql.prepare('SELECT id FROM bridge_jobs').get();n++)await sleep(5);
  assert.equal((await(await f.device('next',{},'mac')).json()).idle,true);
  f.sql.prepare("UPDATE controller_nodes SET ready=1 WHERE id='pi'").run();
  const job=await next(f);
  await complete(f,job,answer({state:'queued',next_call:{controller:mac,operation:'cap.status',arguments:{},request_id:'read'}}));
  assert.equal((await(await pending).json()).result.isError,true);
  const deniedPending=f.invoke(route('cap.acquire',{},'rejected')),deniedJob=await next(f);
  const denial={isError:true,content:[{type:'text',text:'Wrong application controller'}]};
  await complete(f,deniedJob,denial);
  assert.deepEqual((await(await deniedPending).json()).result,denial,'Controller rejection remains a rejection');
 }finally{f.sql.close()}
});

test('legacy omitted selector and modern explicit selector retain their existing response shapes',async()=>{
 const f=fixture();
 try{
  for(const [controller,node] of [[undefined,'mac'],[pi,'pi']]){
   const call={operation:'cap.acquire',arguments:{},request_id:'same-call',...(controller===undefined?{}:{controller})};
   const pending=f.invoke(call),job=await next(f,node);
   assert.equal(job.request.arguments.controller,controller);assert.equal(job.request.arguments.controller_owner,node);
   const body={next_call:{operation:'cap.status',arguments:{command:'same-call'},request_id:'live-status'}};
   await complete(f,job,answer(body),node);
   assert.deepEqual(value((await(await pending).json()).result),body,'Only compatibility-wrapper responses are wrapped');
  }
 }finally{f.sql.close()}
});
