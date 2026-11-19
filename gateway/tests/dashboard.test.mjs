import {test} from 'node:test';
import assert from 'node:assert/strict';
import {dashboard} from '../lib/dashboard.mjs';
const base='https://biomerte-controller.cantikash.chatgpt.site';
const owner=async()=> 'owner';
const headers={Origin:base,'Content-Type':'application/json'};
const body={id:'same-request',operation:'system.snapshot',arguments:{},realm:'site',cached:true};
const req=(data=body,extra={})=>new Request(base+'/dashboard/api',{method:'POST',headers:{...headers,...extra},body:JSON.stringify(data)});
test('anonymous dashboard offers top-level owner sign-in; API and assets reject anonymous users',async()=>{
 const deps={ownerPrincipal:async()=>null,enqueue:()=>{throw Error('unauthorized enqueue')}};
 const page=await dashboard(new Request(base+'/dashboard/'),{},deps);assert.equal(page.status,200);assert.match(await page.text(),/signin-with-chatgpt/);
 assert.equal((await dashboard(req(),{},deps)).status,401);
 assert.equal((await dashboard(new Request(base+'/dashboard/ui.js'),{},deps)).status,401);
 assert.equal((await dashboard(new Request(base+'/dashboard/',{headers:{'oai-authenticated-user-id':'other'}}),{},deps)).status,403);
});
test('original assets load under dashboard prefix without controller token entry',async()=>{
 const deps={ownerPrincipal:owner};
 const html=await dashboard(new Request(base+'/dashboard/'),{},deps);
 assert.equal(html.status,200);assert.match(html.headers.get('Cache-Control'),/no-store/);
 const text=await html.text();assert.match(text,/\/dashboard\/ui.js/);assert.doesNotMatch(text,/type="password"/);
 const js=await (await dashboard(new Request(base+'/dashboard/ui.js'),{},deps)).text();
 assert.match(dashboardModel,/\/dashboard\/api/);assert.doesNotMatch(js,/Bearer /);
});
test('origin, media type, request schema, method and unknown routes are checked before dispatch',async()=>{
 const deps={ownerPrincipal:owner,enqueue:()=>{throw Error('invalid enqueue')}};
 for(const data of [{...body,id:''},{...body,realm:'other'},{...body,arguments:[]},{...body,cached:'true'},{...body,path:'http://evil'},{...body,operation:'../../x'}])assert.equal((await dashboard(req(data),{},deps)).status,400);
 assert.equal((await dashboard(req(body,{Origin:'https://evil.example'}),{},deps)).status,403);
 assert.equal((await dashboard(req(body,{'Sec-Fetch-Site':'cross-site'}),{},deps)).status,403);
 assert.equal((await dashboard(req(body,{'Content-Type':'text/plain'}),{},deps)).status,415);
 assert.equal((await dashboard(new Request(base+'/dashboard/api'),{},deps)).status,405);
 assert.equal((await dashboard(new Request(base+'/dashboard/else'),{},deps)).status,404);
 assert.equal((await dashboard(new Request(base+'/dashboard'),{},deps)).status,308);
});
test('forwards exact call including cached and local realm; stable owner-bound retry key',async()=>{
 const calls=[];const deps={ownerPrincipal:owner,enqueue:async(...args)=>{calls.push(args);return {content:[{type:'text',text:JSON.stringify({status:200,body:{result:{work:[]}}})}]}}};
 for(let i=0;i<2;i++)assert.deepEqual(await (await dashboard(req({...body,realm:'local'}),{},deps)).json(),{result:{work:[]}});
 assert.equal(calls[0][1],'biome_dashboard_call');assert.deepEqual(calls[0][2],{...body,realm:'local'});assert.equal(calls[0][3],calls[1][3]);
});
test('preserves controller errors and reports pending calls as recoverable',async()=>{
 let deps={ownerPrincipal:owner,enqueue:async()=>({content:[{type:'text',text:JSON.stringify({status:409,body:{error:'revision conflict'}})}]})};
 assert.equal((await dashboard(req(),{},deps)).status,409);
 deps={...deps,enqueue:async()=>({isError:true,content:[{type:'text',text:'The controller has not replied yet. Retry with the same request_id to recover the result.'}]})};
 assert.equal((await dashboard(req(),{},deps)).status,504);
});

import vm from 'node:vm';
import {dashboardJS,dashboardModel} from '../lib/dashboard-assets.mjs';
import {createClient} from '../dashboard/model.mjs';
test('pending gateway response retries the identical controller request ID',async()=>{
 const bodies=[];
 const client=createClient(async(path,options)=>{bodies.push(options.body);return {status:bodies.length===1?504:200,ok:bodies.length>1,json:async()=>bodies.length===1?{error:'pending'}:{result:'ready'}}});
 assert.equal(await client('work.list'),'ready');assert.equal(bodies.length,2);assert.equal(bodies[0],bodies[1]);
});

test('large controller responses stream every verified chunk without dropping data',async()=>{
 const bytes=new TextEncoder().encode(JSON.stringify({result:'x'.repeat(200000)}));
 const parts=[bytes.slice(0,131072),bytes.slice(131072)];
 const digest=async b=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b)),x=>x.toString(16).padStart(2,'0')).join('');
 const descriptors=await Promise.all(parts.map(async b=>({sha256:await digest(b),size:b.length})));
 const all=await digest(bytes);const calls=[];
 const deps={ownerPrincipal:owner,enqueue:async(env,name,args)=>{
  calls.push(name);
  const value=name==='biome_dashboard_call'?{status:200,transport:{sha256:all,size:bytes.length,chunks:descriptors}}:
    {base64:Buffer.from(parts[args.index]).toString('base64'),...descriptors[args.index]};
  return {content:[{type:'text',text:JSON.stringify(value)}]};
 }};
 const res=await dashboard(req(),{},deps);assert.equal(res.status,200);assert.equal((await res.json()).result.length,200000);
 assert.deepEqual(calls,['biome_dashboard_call','biome_dashboard_chunk','biome_dashboard_chunk']);
});

test('large dashboard operation arguments are forwarded intact with existing owner checks',async()=>{
 const payload={...body,arguments:{text:'😀'.repeat(270000)}};
 const deps={ownerPrincipal:owner,enqueue:async(env,name,args)=>{
  assert.deepEqual(args,payload);
  return {content:[{type:'text',text:JSON.stringify({status:200,body:{result:true}})}]};
 }};
 assert.equal((await dashboard(req(payload),{},deps)).status,200);
});
