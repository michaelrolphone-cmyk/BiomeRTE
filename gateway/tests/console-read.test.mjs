import {test} from 'node:test';
import assert from 'node:assert/strict';
import {consoleRead} from '../lib/console-read.mjs';
const origin='https://controller.example';
const req=(body,headers={})=>new Request(origin+'/api/console/read',{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)});
const deps={ownerPrincipal:async()=>null,bearerPrincipal:async()=>null};
test('console reads reject anonymous and cross-origin browser access before dispatch',async()=>{
 assert.equal((await consoleRead(req({view:'info'}),{},deps)).status,401);
 assert.equal((await consoleRead(req({view:'info'},{Origin:'https://other.example'}),{},deps)).status,403);
 assert.equal((await consoleRead(req({view:'info'},{'Sec-Fetch-Site':'cross-site'}),{},deps)).status,403);
});
test('owner-authorized discovery avoids the reserved MCP browser endpoint',async()=>{
 let args;const response=await consoleRead(req({view:'offers',cursor:'opaque'}),{},{...deps,ownerPrincipal:async()=> 'owner',discoverPage:async(env,a)=>{args=a;return {content:[{type:'text',text:JSON.stringify({items:[{id:'real-offer'}],section_total:71})}]};}});
 assert.equal(response.status,200);const result=await response.json();assert.equal(result.data.section_total,71);assert.deepEqual(args,{section:'offers',limit:25,cursor:'opaque'});
});
test('existing OAuth owner can read native records through the same chunk-capable dashboard path',async()=>{
 let captured;const response=await consoleRead(req({view:'processes',realm:'local',request_id:'same-read'}),{},{...deps,bearerPrincipal:async()=> 'owner',dashboard:async(r,env,d)=>{captured={url:r.url,body:await r.json(),owner:await d.ownerPrincipal()};return Response.json({result:{definitions:[],runs:[{id:'run'}]}});}});
 assert.equal((await response.json()).data.runs[0].id,'run');assert.equal(captured.url,origin+'/dashboard/api');assert.equal(captured.owner,'owner');assert.equal(captured.body.id,'same-read');assert.equal(captured.body.realm,'local');assert.equal(captured.body.operation,'process.list');
});
test('definition reads retain exact revision and authorize through the existing definition handler',async()=>{
 let captured;const response=await consoleRead(req({view:'definition',name:'release',revision:'a'.repeat(64),realm:'site'}),{},{...deps,bearerPrincipal:async()=> 'owner',dashboardDefinition:async(r,env,d)=>{captured={body:await r.json(),owner:await d.ownerPrincipal()};return Response.json({result:{graph:{nodes:{start:{type:'start'}}}}});}});
 assert.ok((await response.json()).data.graph.nodes.start);assert.equal(captured.body.revision,'a'.repeat(64));assert.equal(captured.owner,'owner');
});
test('console adapter cannot execute arbitrary operations or accept caller-supplied identity',async()=>{
 const authorized={...deps,ownerPrincipal:async()=> 'owner'};
 for(const body of [{view:'cap.invoke'},{view:'process.start'},{view:'info',operation:'cap.invoke'},{view:'info',realm:'bad'},{view:'info',controller:'untrusted'},{view:'info',owner:'other'}])assert.equal((await consoleRead(req(body),{},authorized)).status,400);
});
test('pending native reads preserve status and never become an empty successful result',async()=>{
 const response=await consoleRead(req({view:'processes',request_id:'same-read'}),{},{...deps,ownerPrincipal:async()=> 'owner',dashboard:async()=>Response.json({error:'not replied'},{status:504})});assert.equal(response.status,504);assert.equal((await response.json()).error,'not replied');
});
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {bearerPrincipal} from '../lib/oauth.mjs';
import {ownerPrincipal} from '../lib/access.mjs';
test('real token lookup rejects expired, revoked and other-owner tokens; browser identity is not forged',async()=>{
 const db=new DatabaseSync(':memory:');db.exec('CREATE TABLE biome_owner(singleton INTEGER PRIMARY KEY,user_id TEXT); INSERT INTO biome_owner VALUES(1,\'owner\'); CREATE TABLE biome_oauth_tokens(hash TEXT,kind TEXT,principal TEXT,resource TEXT,expires REAL)');
 const token='a'.repeat(43),hash=createHash('sha256').update(token).digest('base64url');
 const env={BIOME_OAUTH_ORIGIN:origin,DB:{prepare(sql){const statement=db.prepare(sql);let bindings=[];return {bind(...v){bindings=v;return this},async first(){return statement.get(...bindings)||null}}}}};
 const authorized={...deps,bearerPrincipal,ownerPrincipal,discoverPage:async()=>({structuredContent:{controllers:[]}})};
 const read=()=>consoleRead(req({view:'controllers'},{Authorization:'Bearer '+token}),env,authorized);
 assert.equal((await read()).status,401);
 db.prepare('INSERT INTO biome_oauth_tokens VALUES(?,?,?,?,?)').run(hash,'access','owner',origin+'/api/mcp',Date.now()/1000+600);assert.equal((await read()).status,200);
 db.prepare('UPDATE biome_oauth_tokens SET expires=0').run();assert.equal((await read()).status,401);
 db.prepare('UPDATE biome_oauth_tokens SET expires=?,principal=?').run(Date.now()/1000+600,'other');assert.equal((await read()).status,401);
 db.exec('DELETE FROM biome_oauth_tokens');assert.equal((await read()).status,401);
});
test('unfiltered lease inventory reads each registered controller instead of presenting missing hosts as idle',async()=>{
 const calls=[];const response=await consoleRead(req({view:'leases',realm:'site'}),{},{...deps,ownerPrincipal:async()=> 'owner',discoverPage:async()=>({structuredContent:{controllers:[{controller:'mac',label:'Mac'},{controller:'pi',label:'Pi'}]}}),runTool:async(env,name,args)=>{calls.push(args);return {structuredContent:[{id:args.controller+'-lease',resource:'terminal',state:'bound'}]};}});const result=await response.json();assert.equal(result.data.leases.length,2);assert.deepEqual(calls.map(c=>c.controller),['mac','pi']);assert.ok(calls.every(c=>c.operation==='cap.leases'));assert.ok(result.data.coverage.every(c=>c.error===null));
});
test('expired event history resumes at the reported retention floor without inventing older events',async()=>{
 const bodies=[];const response=await consoleRead(req({view:'events',topic:'workflow.delegation.requested',request_id:'same-event-page'}),{},{...deps,ownerPrincipal:async()=> 'owner',dashboard:async r=>{const b=await r.json();bodies.push(b);return bodies.length===1?Response.json({error:'Event history expired; resynchronize from a snapshot (event floor 73090)'},{status:409}):Response.json({result:{events:[{seq:73091}]}});}});const result=await response.json();assert.equal(result.data.events[0].seq,73091);assert.equal(result.data.resynchronized_from,73090);assert.equal(result.data.history_before_unavailable,true);assert.equal(bodies[1].arguments.after,73090);assert.notEqual(bodies[0].id,bodies[1].id);
});
