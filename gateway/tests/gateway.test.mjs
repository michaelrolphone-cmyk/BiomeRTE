import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
const base='https://biome.test';
const sha=v=>createHash('sha256').update(v).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec(readFileSync('drizzle/0000_biome_gateway.sql','utf8'));sql.exec(readFileSync('drizzle/0001_controller_ha.sql','utf8'));
 const DB={prepare(query){let args=[];const stmt=sql.prepare(query);return {bind(...values){args=values;return this},async first(){return stmt.get(...args)},async run(){return stmt.run(...args)},async all(){return {results:stmt.all(...args)}}}}};
 const deviceKey='f'.repeat(64);
 const env={DB,BIOME_RECEIVER_WRAP_KEY:Buffer.alloc(32,33).toString('base64'),BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',BIOME_BRIDGE_PIN:sha(sha(deviceKey)+'\n')};
 const owner={'oai-authenticated-user-id':'owner','oai-authenticated-user-email':'owner@test.invalid'};
 const send=(path,body,headers={})=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'content-type':'application/json',...headers},body:JSON.stringify(body)}),env);
 const rpc=(method,params={})=>({jsonrpc:'2.0',id:1,method,params});
 return {sql,send,rpc,owner,deviceKey};
}
test('external discovery challenges, native tools enforce owner, a device cannot invoke MCP',async()=>{
 const {sql,send,rpc,deviceKey}=fixture();
 const ext=await send('/api/mcp',rpc('initialize'));
 assert.equal(ext.status,401);assert.match(ext.headers.get('www-authenticate'),/oauth-protected-resource/);
 assert.equal((await send('/mcp',rpc('tools/call',{name:'biome_status'}))).status,403);
 assert.equal((await send('/api/mcp',rpc('tools/list'),{Authorization:'Bearer '+deviceKey})).status,401);
 assert.equal((await send('/bridge/next',{})).status,401);
 assert.equal((await send('/mcp',rpc('initialize'),{Origin:'https://untrusted.test'})).status,403);
 const list=await (await send('/mcp',rpc('tools/list'))).json();assert.deepEqual(list.result.tools.map(t=>t.name),['biome_status','biome_discover','biome_work_list','biome_invoke','biome_register_grok_webhook','biome_list_grok_webhooks','biome_unregister_grok_webhook']);sql.close();
});
test('controller claims and completes an authenticated OAuth call, same ID recovers a result',async()=>{
 const {sql,send,rpc,owner,deviceKey}=fixture();
 await send('/mcp',rpc('tools/call',{name:'bad'}),owner);
 const at='x'.repeat(43),hash=createHash('sha256').update(at).digest('base64url');
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)").run(hash,base+'/api/mcp',Date.now()/1000+60);
 const auth={Authorization:'Bearer '+at},link={Authorization:'Bearer '+deviceKey};
 const args={operation:'work.list',arguments:{},request_id:'unit-1'};
 const request=()=>send('/api/mcp',rpc('tools/call',{name:'biome_invoke',arguments:args}),auth);
 const pending=request();
 let job;
 for(let i=0;i<20;i++){await new Promise(r=>setTimeout(r,10));job=await (await send('/bridge/next',{},link)).json();if(job.id)break}
 assert.equal(job.request.arguments.operation,'work.list');
 const answer={content:[{type:'text',text:'live list'}]};
 assert.equal((await send('/bridge/complete',{id:job.id,lease:'wrong',result:answer},link)).status,409);
 assert.equal((await send('/bridge/complete',{id:job.id,lease:job.lease,result:answer},link)).status,200);
 assert.deepEqual((await (await pending).json()).result,answer);
 assert.deepEqual((await (await request()).json()).result,answer);
 args.operation='work.create';assert.equal((await (await request()).json()).result.isError,true);
 assert.deepEqual(await (await send('/bridge/next',{},link)).json(),{idle:true});
 sql.close();
});

test('MCP events are advertised on both handshakes; subscriptions and grant checks enforce ownership',async()=>{
 const {sql,send,rpc,owner,deviceKey}=fixture();
 for(const method of ['initialize','server/discover']){
  assert.deepEqual((await (await send('/mcp',rpc(method))).json()).result.capabilities.events,{});
 }
 const list=await (await send('/mcp',rpc('events/list',{_meta:{progressToken:'scan'}}))).json();
 assert.deepEqual(list.result.events.map(e=>e.name),['workflow.delegation.requested','lease.available']);
 assert.equal((await send('/mcp',rpc('events/subscribe'))).status,403);
 assert.equal((await send('/api/mcp',rpc('events/list'))).status,401);
 const bad=await (await send('/mcp',rpc('events/subscribe',{name:'bad'}),owner)).json();
 assert.equal(bad.error.code,-32602);
 const h={Authorization:'Bearer '+deviceKey};
 assert.equal((await (await send('/bridge/event-authorize',{owner:'owner',grant:null},h)).json()).allowed,true);
 assert.equal((await (await send('/bridge/event-authorize',{owner:'owner',grant:'revoked'},h)).json()).allowed,false);
 assert.equal((await (await send('/bridge/event-authorize',{owner:'impostor',grant:null},h)).json()).allowed,false);
 sql.close();
});

test('Grok OAuth registration is sealed in transit storage and returned only to the authenticated Pi',async()=>{
 const {sql,send,rpc,owner,deviceKey}=fixture();
 await send('/mcp',rpc('tools/call',{name:'bad'}),owner);
 const token='q'.repeat(43),hash=createHash('sha256').update(token).digest('base64url');
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grok-grant','client','owner',?,?)").run(hash,base+'/api/mcp',Date.now()/1000+60);
 const args={receiver:'bot',url:'https://api2.cursor.sh/automations/webhook/opaque-example',key:'private-test-key',request_id:'register-1'};
 const body=rpc('tools/call',{name:'biome_register_grok_webhook',arguments:args});
 assert.equal((await send('/mcp',body)).status,403);
 const pending=send('/api/mcp',body,{Authorization:'Bearer '+token});
 let row;
 for(let i=0;i<100&&!row;i++){await new Promise(r=>setTimeout(r,5));row=sql.prepare('SELECT request FROM bridge_jobs LIMIT 1').get();}
 assert.ok(!row.request.includes(args.url)&&!row.request.includes(args.key));
 assert.equal((await send('/bridge/next',{})).status,401);
 const link={Authorization:'Bearer '+deviceKey};
 const job=await (await send('/bridge/next',{},link)).json();
 assert.equal(job.request.name,'grok_receiver_change');
 assert.deepEqual(job.request.arguments,{owner:'owner',grant:'grok-grant',action:'register',args});
 await send('/bridge/complete',{id:job.id,lease:job.lease,result:{content:[{type:'text',text:'registered'}]}},link);
 assert.equal((await (await pending).json()).result.content[0].text,'registered');
 assert.equal(sql.prepare('SELECT request FROM bridge_jobs').get().request,'{}');
 sql.close();
});

test('cached clients reach the same receiver registration through biome_invoke',async()=>{
 const {grokCall}=await import('../lib/grok.mjs');
 const settings={receiver:'field',url:'https://unit.test/entry',key:'test'};
 const outer={operation:'grok.receiver.register',realm:'local',request_id:'new',arguments:settings};
 assert.deepEqual(grokCall({name:'biome_invoke',arguments:outer}),{action:'register',args:{...settings,realm:'local',request_id:'new'}});
 assert.equal(grokCall({name:'biome_invoke',arguments:{operation:'work.list',arguments:{}}}),null);
});

test('OAuth discovery lists Grok tools and cached clients can follow a complete bounded snapshot',async()=>{
 const {sql,send,rpc,deviceKey}=fixture();
 await send('/mcp',rpc('tools/call',{name:'bad'}),{'oai-authenticated-user-id':'owner','oai-authenticated-user-email':'owner@test.invalid'});
 const token='p'.repeat(43),auth={Authorization:'Bearer '+token},link={Authorization:'Bearer '+deviceKey};
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)").run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+60);
 const call=async(method,params)=>(await (await send('/api/mcp',rpc(method,params),auth)).json()).result;
 const names=(await call('tools/list')).tools.map(t=>t.name);
 for(const name of ['biome_register_grok_webhook','biome_list_grok_webhooks'])assert.ok(names.includes(name));
 const pending=call('tools/call',{name:'biome_discover',arguments:{}});
 let job;
 for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,10));job=await(await send('/bridge/next',{},link)).json();if(job.id)break;}
 const large='😀"'.repeat(9000);
 const doc={operations:[{name:'test'}],capabilities:{contracts:[],offers:[{large}]},capability_caller_guide:{a:1}};
 await send('/bridge/complete',{id:job.id,lease:job.lease,result:{content:[{type:'text',text:JSON.stringify(doc)}]}},link);
 let response=await pending, fragments=[],seen=[];
 for(let i=0;i<200;i++){
  assert.ok(Buffer.byteLength(JSON.stringify(response))<20000);
  const page=JSON.parse(response.content[0].text);
  for(const item of page.items){seen.push(item.section);if(item.section==='offers')fragments.push(item.fragment);}
  if(!page.next_cursor)break;
  response=await call('tools/call',page.next_call);
 }
 assert.equal(JSON.parse(fragments.join('')).large,large);assert.ok(seen.includes('operations')&&seen.includes('guide'));sql.close();
});

test('private receiver verifies challenge, deduplicates signed deliveries and acknowledges only its inbox',async()=>{
 const {sql,send,rpc,owner}=fixture();
 await send('/mcp',rpc('tools/call',{name:'bad'}),owner);
 const token='a'.repeat(43), hash=createHash('sha256').update(token).digest('base64url');
 sql.prepare("INSERT INTO biome_oauth_tokens VALUES (?,'access','mac-grant','mac','owner',?,?)").run(hash,base+'/api/mcp',Date.now()/1000+300);
 const auth={Authorization:'Bearer '+token},root='/agent-events/lmstudio/';
 assert.equal((await send(root+'connect',{})).status,401);
 const {url,secret}=await (await send(root+'connect',{},auth)).json(),path=new URL(url).pathname;
 const key=Buffer.from(secret.slice(6),'base64'),ts=String(Math.floor(Date.now()/1000));
 async function post(id,body){const {createHmac}=await import('node:crypto');return send(path,body,{'webhook-id':id,'webhook-timestamp':ts,'webhook-signature':'v1,'+createHmac('sha256',key).update(id+'.'+ts+'.'+JSON.stringify(body)).digest('base64')})}
 assert.deepEqual(await (await post('verify_1',{type:'verification',challenge:'hello'})).json(),{challenge:'hello'});
 const ev={eventId:'delegate_1',name:'workflow.delegation.requested',data:{delegation_id:'1'}};
 assert.equal((await send(path,ev)).status,401);await post(ev.eventId,ev);await post(ev.eventId,ev);
 const next=async(p=root)=>await (await send(p+'next',{},auth)).json();
 assert.deepEqual((await next()).events,[ev]);assert.deepEqual((await next('/agent-events/grok/')).events,[]);
 await send(root+'ack',{}, {...auth,'x-event-id':ev.eventId});assert.deepEqual((await next()).events,[]);sql.close();
});

test('shared journal CAS, preferred routing, overflow, takeover and return',async()=>{
 const {sql,send,deviceKey}=fixture();
 const pi={Authorization:'Bearer '+deviceKey},macKey='a'.repeat(64),mac={Authorization:'Bearer '+macKey};
 const config={site:'ha-test',enabled:true,nodes:[{id:'pi',token_hash:sha(deviceKey),priority:0,capacity:1},{id:'mac',token_hash:sha(macKey),priority:10,capacity:2}]};
 assert.equal((await send('/bridge/ha/configure',config,pi)).status,200);
 const heartbeat={position:0,ready:true,load:0,host:'test'};
 await send('/bridge/ha/heartbeat',heartbeat,pi);await send('/bridge/ha/heartbeat',heartbeat,mac);
 const entry=JSON.stringify({seq:1,prev:'',changes:[['state','x',{value:1}]]});
 assert.equal((await send('/bridge/ha/append',{expected:0,entry},pi)).status,200);
 assert.equal((await send('/bridge/ha/append',{expected:0,entry},mac)).status,200);
 assert.equal((await send('/bridge/ha/append',{expected:0,entry:entry.replace('value":1','value":2')},mac)).status,409);
 assert.equal((await (await send('/bridge/ha/read',{key:'head'},mac)).json()).value.hash,sha(entry));
 assert.equal((await (await send('/bridge/ha/read',{key:'log/00000000000000000001'},mac)).json()).encoded,entry);
 const put=id=>sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,?,'queued',?,?)").run(id,JSON.stringify({name:'biome_status',arguments:{}}),Date.now()/1000+60,Date.now()/1000);
 put('primary');assert.equal((await (await send('/bridge/next',{},mac)).json()).idle,true);
 const first=await (await send('/bridge/next',{},pi)).json();assert.equal(first.id,'primary');
 put('overflow');assert.equal((await (await send('/bridge/next',{},mac)).json()).id,'overflow');
 sql.prepare("UPDATE controller_nodes SET seen=0 WHERE id='pi'").run();
 const takeover=await (await send('/bridge/next',{},mac)).json();assert.equal(takeover.id,'primary');
 const answer={content:[{type:'text',text:'done'}]};
 assert.equal((await send('/bridge/complete',{id:first.id,lease:first.lease,result:answer},pi)).status,409);
 assert.equal((await send('/bridge/complete',{id:takeover.id,lease:takeover.lease,result:answer},mac)).status,200);
 assert.equal((await (await send('/bridge/ha/heartbeat',heartbeat,pi)).json()).primary,'pi');
 const claim=await (await send('/bridge/ha/events/claim',{},pi)).json();assert.equal(claim.acquired,true);
 sql.prepare('UPDATE controller_events SET until=0').run();
 const next=await (await send('/bridge/ha/events/claim',{},pi)).json();
 assert.ok(next.fence>claim.fence);
 assert.equal((await send('/bridge/ha/events/commit',{fence:claim.fence,body:[]},pi)).status,409);
 assert.equal((await send('/bridge/ha/events/commit',{fence:next.fence,body:[]},pi)).status,200);
 sql.close();
});
