import {sqliteD1} from './sqlite-d1.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash,generateKeyPairSync,sign} from 'node:crypto';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
const base='https://biome.test';
const sha=v=>createHash('sha256').update(v).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec(readFileSync('drizzle/0000_biome_gateway.sql','utf8'));sql.exec(readFileSync('drizzle/0001_controller_ha.sql','utf8'));
 const DB=sqliteD1(sql);
 const deviceKey='f'.repeat(64);
 const objects=new Map(),JOURNAL_ARCHIVE={async put(k,v){objects.set(k,v)},async get(k){return objects.has(k)?{async text(){return objects.get(k)}}:null}};
 const env={DB,JOURNAL_ARCHIVE,BIOME_RECEIVER_WRAP_KEY:Buffer.alloc(32,33).toString('base64'),BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',BIOME_BRIDGE_PIN:sha(sha(deviceKey)+'\n')};
 const owner={'oai-authenticated-user-id':'owner','oai-authenticated-user-email':'owner@test.invalid'};
 const send=(path,body,headers={})=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'content-type':'application/json',...headers},body:JSON.stringify(body)}),env);
 const rpc=(method,params={})=>({jsonrpc:'2.0',id:1,method,params});
 return {sql,send,rpc,owner,deviceKey,env};
}
test('external discovery challenges, native tools enforce owner, a device cannot invoke MCP',async()=>{
 const {sql,send,rpc,deviceKey}=fixture();
 const ext=await send('/api/mcp',rpc('initialize'));
 assert.equal(ext.status,401);assert.match(ext.headers.get('www-authenticate'),/oauth-protected-resource/);
 assert.equal((await send('/mcp',rpc('tools/call',{name:'biome_status'}))).status,403);
 assert.equal((await send('/api/mcp',rpc('tools/list'),{Authorization:'Bearer '+deviceKey})).status,401);
 assert.equal((await send('/bridge/next',{})).status,401);
 assert.equal((await send('/mcp',rpc('initialize'),{Origin:'https://untrusted.test'})).status,403);
 const list=await (await send('/mcp',rpc('tools/list'))).json();assert.deepEqual(list.result.tools.map(t=>t.name),['biome_status','biome_discover','biome_work_list','biome_invoke','biome_register_grok_webhook','biome_list_grok_webhooks','biome_unregister_grok_webhook']);
 assert.deepEqual(list.result.tools.find(t=>t.name==='biome_discover').inputSchema.properties.limit,{type:'integer',minimum:1,default:25});
 sql.close();
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

test('native and OAuth discovery forward controller pages, including continuation and realm',async()=>{
 const {sql,send,rpc,owner,deviceKey}=fixture();
 await send('/mcp',rpc('tools/call',{name:'bad'}),owner);
 const token='p'.repeat(43),auth={Authorization:'Bearer '+token},link={Authorization:'Bearer '+deviceKey};
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)").run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+60);
 const pages=[
  {section:'operations',items:[{name:'cap.acquire'}],next_cursor:'native-cursor',complete:false,next_call:{operation:'system.discovery',arguments:{cursor:'native-cursor'},request_id:'next'}},
  {section:'offers',items:[{contract:'host.terminal.run:1'}],next_cursor:null,complete:true},
  {section:'guide',items:[{caller:['cap.acquire']}],next_cursor:null,complete:true}
 ];
 const calls=[
  ['/api/mcp',auth,'biome_discover',{}, {},'site'],
  ['/mcp',owner,'biome_discover',{section:'offers',limit:25},{section:'offers',limit:25},'site'],
  ['/mcp',owner,'biome_invoke',{operation:'discovery.page',arguments:{cursor:'native-cursor',limit:7},realm:'local',request_id:'alias'},{cursor:'native-cursor',limit:7},'local']
 ];
 for(const [i,[path,headers,name,args,expected,realm]] of calls.entries()){
  const pending=send(path,rpc('tools/call',{name,arguments:args}),headers);
  let job;
  for(let n=0;n<100;n++){await new Promise(r=>setTimeout(r,5));job=await(await send('/bridge/next',{},link)).json();if(job.id)break;}
  assert.equal(job.request.name,'biome_invoke');
  assert.equal(job.request.arguments.operation,'system.discovery');
  assert.deepEqual(job.request.arguments.arguments,expected);
  assert.equal(job.request.arguments.realm,realm);
  const answer={content:[{type:'text',text:JSON.stringify(pages[i])}]};
  await send('/bridge/complete',{id:job.id,lease:job.lease,result:answer},link);
  const response=await pending;assert.equal(response.status,200);
  assert.deepEqual((await response.json()).result,answer);
 }
 for(const args of [{section:'bad'},{cursor:5},{cursor:'discovery-legacy:offers:0'},...[0,-1,1.5,'25',null].map(limit=>({limit}))]){
  const res=await send('/mcp',rpc('tools/call',{name:'biome_discover',arguments:args}),owner);
  assert.equal(res.status,200);assert.equal((await res.json()).result.isError,true);
 }
 assert.equal(sql.prepare("SELECT count(*) AS n FROM bridge_jobs WHERE state!='done'").get().n,0);
 sql.close();
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

test('independent maintenance authenticates separately and survives controller unavailability',async()=>{
 const {sql,send,rpc,owner,deviceKey}=fixture();
 const pair=generateKeyPairSync('rsa',{modulusLength:2048});
 const publicKey=pair.publicKey.export({type:'spki',format:'pem'}),phone=sha(publicKey);
 const canonical=v=>JSON.stringify((function order(x){return Array.isArray(x)?x.map(order):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,order(x[k])])):x})(v));
 const signed=(command,payload,id)=>{const e={device:phone,site:'test',id,command,payload:Buffer.from(JSON.stringify(payload)).toString('base64')};return {...e,signature:sign('sha256',Buffer.from(canonical(e)),pair.privateKey).toString('base64')};};
 const key='b'.repeat(64),call=(operation,a,id)=>send('/mcp',rpc('tools/call',{name:'biome_invoke',arguments:{operation,arguments:a,request_id:id}}),owner);
 assert.equal((await send('/maintenance/device',{operation:'poll'})).status,401);
 assert.equal((await send('/mcp',rpc('tools/call',{name:'biome_invoke',arguments:{operation:'maintenance.enroll',arguments:{host:'mac-maint',key_hash:sha(key)},request_id:'enroll'}}))).status,403);
 const enrolling=call('maintenance.enroll',{host:'mac-maint',key_hash:sha(key)},'enroll');
 let job;for(;;){job=await (await send('/bridge/next',{}, {Authorization:'Bearer '+deviceKey})).json();if(job.id)break;await new Promise(r=>setTimeout(r,10));}
 assert.equal(job.request.arguments.operation,'decision.maintenance_identity');
 await send('/bridge/complete',{id:job.id,lease:job.lease,result:{content:[{type:'text',text:JSON.stringify({device:phone,key:publicKey,site:'test'})}]}},{Authorization:'Bearer '+deviceKey});
 await enrolling;
 await call('maintenance.submit',{host:'mac-maint',argv:['/bin/echo','independent']},'cmd');
 const conflict=await (await call('maintenance.submit',{host:'mac-maint',argv:['/bin/echo','different']},'cmd')).json();
 assert.equal(conflict.result.isError,true);
 const auth={Authorization:'Bearer '+key};
 const polled=await (await send('/maintenance/device',{operation:'poll'},auth)).json();
 assert.equal(polled.jobs[0].id,'cmd');
 assert.equal(polled.jobs[0].confirmation,undefined);
 assert.equal(sql.prepare("SELECT state FROM maintenance_jobs WHERE id='cmd'").get().state,'claimed');
 assert.equal(sql.prepare("SELECT count(*) AS n FROM bridge_jobs WHERE state!='done'").get().n,0);
 await send('/maintenance/device',{operation:'complete',id:'cmd',result:{state:'succeeded',exit_code:0,stdout:'independent'}},auth);
 const result=await (await call('maintenance.result',{id:'cmd'},'result')).json();
 assert.equal(JSON.parse(result.result.content[0].text).state,'succeeded');
 await call('maintenance.revoke',{host:'mac-maint'},'revoke');
 assert.equal((await send('/maintenance/device',{operation:'poll'},auth)).status,401);
 sql.close();
});
test('journal accepts a transaction beyond the removed software ceilings',async()=>{
 const {sql,send,deviceKey}=fixture(),link={Authorization:'Bearer '+deviceKey};
 await send('/bridge/ha/configure',{site:'large-journal',enabled:true,nodes:[{id:'mac',token_hash:sha(deviceKey),priority:0,capacity:1}]},link);
 const entry=JSON.stringify({seq:1,prev:'',changes:[['test','large',{data:'x'.repeat(1600000)}]]});
 const response=await send('/bridge/ha/append',{expected:0,entry},link);
 assert.equal(response.status,200);
 const read=await (await send('/bridge/ha/read',{key:'log/00000000000000000001'},link)).json();
 assert.equal(read.encoded,entry);
 sql.close();
});
test('shared journal CAS, preferred routing, overflow, takeover and return',async()=>{
 const {sql,send,deviceKey}=fixture();
 const pi={Authorization:'Bearer '+deviceKey},macKey='a'.repeat(64),mac={Authorization:'Bearer '+macKey};
 const config={site:'ha-test',enabled:true,nodes:[{id:'pi',token_hash:sha(deviceKey),priority:0,capacity:1},{id:'mac',token_hash:sha(macKey),priority:10,capacity:2}]};
 assert.equal((await send('/bridge/ha/configure',config,pi)).status,200);
 const heartbeat={position:0,ready:true,load:0,host:'test'};
 await send('/bridge/ha/heartbeat',heartbeat,pi);await send('/bridge/ha/heartbeat',heartbeat,mac);
 const entry='{"seq":1,"prev":"","changes":[["state","x",{"value":1.0,"big":9007199254740993}]]}';
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

test('decision phone ingress is narrowed and always delegates signature verification to Biome',async()=>{
 const {sql,send,deviceKey}=fixture(),link={Authorization:'Bearer '+deviceKey};
 assert.equal((await send('/api/decisions',{command:'authorization.dispatch'})).status,400);
 assert.equal((await send('/api/decisions',{command:'respond'})).status,401);
 assert.equal((await send('/api/decisions',{command:'inbox'},{Origin:'https://untrusted.test'})).status,403);
 const envelope={command:'inbox',device:'a'.repeat(64),site:'site',id:'same-unverified-id',payload:'e30=',signature:'phone-signature'};
 for(let attempt=0;attempt<2;attempt++){
  const pending=send('/api/decisions',envelope);let job;
  for(let i=0;i<100;i++){await new Promise(r=>setTimeout(r,5));job=await(await send('/bridge/next',{},link)).json();if(job.id)break}
  assert.equal(job.request.arguments.operation,'decision.mobile.inbox');assert.equal(job.request.arguments.realm,'site');
  assert.deepEqual(job.request.arguments.arguments,{envelope});
  await send('/bridge/complete',{id:job.id,lease:job.lease,result:{content:[{type:'text',text:JSON.stringify({site:'site',decisions:[],next_cursor:''})}]}},link);
  assert.deepEqual(await(await pending).json(),{site:'site',decisions:[],next_cursor:''});
 }
 sql.close();
});

async function maintenanceReplyFixture(){
 const f=fixture();
 const call=async(operation,a,id)=>{const r=await f.send('/mcp',f.rpc('tools/call',{name:'biome_invoke',arguments:{operation,arguments:a,request_id:id}}),f.owner);return (await r.json()).result;};
 await call('maintenance.status',{},'schema');
 const pair=generateKeyPairSync('rsa',{modulusLength:2048}),publicKey=pair.publicKey.export({type:'spki',format:'pem'}),phone=sha(publicKey),key='e'.repeat(64);
 f.sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run('mac',sha(key),publicKey,phone,'test');
 const canonical=v=>JSON.stringify((function order(x){return Array.isArray(x)?x.map(order):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,order(x[k])])):x})(v)).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
 const signed=(command,payload,id)=>{const e={device:phone,site:'test',id,command,payload:Buffer.from(JSON.stringify(payload)).toString('base64')};return {...e,signature:sign('sha256',Buffer.from(canonical(e)),pair.privateKey).toString('base64')};};
 // Seed legacy pending records to verify existing history is not auto-released.
 const submit=async(id)=>{await call('maintenance.submit',{host:'mac',argv:['/bin/echo',id]},id);f.sql.prepare("UPDATE maintenance_jobs SET state='awaiting_confirmation' WHERE id=?").run(id);const row=f.sql.prepare('SELECT request FROM maintenance_jobs WHERE id=?').get(id),request=JSON.parse(row.request).approval;return {decision_id:sha(canonical([request.id,1])),digest:sha(canonical(request)),proposal_revision:1};};
 const answer=(card,choice,text,id)=>f.send('/api/decisions',signed('respond',{...card,choice,text},id));
 const poll=async()=>await(await f.send('/maintenance/device',{operation:'poll'},{Authorization:'Bearer '+key})).json();
 return {...f,call,signed,submit,answer,poll};
}
test('signed text replies drain without authorizing execution or reopening their question',async()=>{
 const f=await maintenanceReplyFixture(),card=await f.submit('text');
 const first=await(await f.answer(card,'','approve only after review café','reply')).json();
 assert.equal(first.authenticated,true);assert.equal(first.outcome,'needs_review');assert.equal(first.text,'approve only after review café');
 assert.equal(f.sql.prepare('SELECT state FROM maintenance_jobs WHERE id=?').get('text').state,'needs_review');assert.deepEqual((await f.poll()).jobs,[]);
 assert.deepEqual(await(await f.answer(card,'','approve only after review café','reply')).json(),first);
 assert.equal((await f.answer(card,'approve','','reply')).status,403);
 assert.equal((await(await f.answer(card,'approve','','late')).json()).outcome,'already_settled');
 const retry=await f.call('maintenance.submit',{host:'mac',argv:['/bin/echo','text']},'text');assert.equal(JSON.parse(retry.content[0].text).state,'needs_review');
 assert.deepEqual((await f.poll()).jobs,[]);f.sql.close();
});
test('owner cancellation preserves history and drains a late saved reply without executing',async()=>{
 const f=await maintenanceReplyFixture(),card=await f.submit('cancel');
 const unauth=await f.send('/mcp',f.rpc('tools/call',{name:'biome_invoke',arguments:{operation:'maintenance.cancel',arguments:{id:'cancel'},request_id:'bad'}}));assert.equal(unauth.status,403);
 const result=await f.call('maintenance.cancel',{id:'cancel',reason:'reset approval queue'},'cancel-1');assert.equal(JSON.parse(result.content[0].text).state,'cancelled');
 assert.deepEqual(await f.call('maintenance.cancel',{id:'cancel',reason:'reset approval queue'},'cancel-1'),result);
 assert.equal((await f.call('maintenance.cancel',{id:'another'},'cancel-1')).isError,true);
 const late=await(await f.answer(card,'approve','','late-cancel')).json();assert.equal(late.authenticated,true);assert.equal(late.outcome,'cancelled');assert.deepEqual((await f.poll()).jobs,[]);
 const row=f.sql.prepare('SELECT * FROM maintenance_jobs WHERE id=?').get('cancel');assert.ok(row.request);assert.equal(row.confirmation,null);
 const audit=JSON.parse(f.sql.prepare("SELECT body FROM maintenance_audit WHERE action='maintenance.cancel'").get().body);assert.equal(audit.request.owner,'owner');
 const retry=await f.call('maintenance.submit',{host:'mac',argv:['/bin/echo','cancel']},'cancel');assert.equal(JSON.parse(retry.content[0].text).state,'cancelled');f.sql.close();
});
test('approval requires exact signature and unconditional choice; cancellation cannot stop approved work',async()=>{
 const f=await maintenanceReplyFixture();
 for(const [id,choice,text,state] of [['condition','approve','only if safe','needs_review'],['reject','reject','reason','rejected'],['approved','approve','','queued']]){
  const card=await f.submit(id);
  assert.equal((await f.answer({...card,digest:'wrong'},choice,text,'wrong-'+id)).status,403);
  const bad=f.signed('respond',{...card,choice,text},'bad-'+id);bad.signature=Buffer.alloc(256).toString('base64');assert.equal((await f.send('/api/decisions',bad)).status,403);
  const receipt=await(await f.answer(card,choice,text,'ok-'+id)).json();assert.equal(receipt.outcome,state==='queued'?'approved':state);
  assert.equal(f.sql.prepare('SELECT state FROM maintenance_jobs WHERE id=?').get(id).state,state);
 }
 assert.equal((await f.call('maintenance.cancel',{id:'approved'},'cancel-approved')).isError,true);
 assert.deepEqual((await f.poll()).jobs.map(j=>j.id),['approved']);f.sql.close();
});
test('approval and owner cancellation race has one consistent durable winner',async()=>{
 const f=await maintenanceReplyFixture();
 for(let i=0;i<8;i++){
  const id='race-'+i,card=await f.submit(id);
  const [cancel,answer]=await Promise.all([f.call('maintenance.cancel',{id},'cancel-'+id),f.answer(card,'approve','','approve-'+id)]);
  const response=await answer.json(),row=f.sql.prepare('SELECT state,confirmation FROM maintenance_jobs WHERE id=?').get(id);
  if(row.state==='cancelled'){assert.equal(response.outcome,'cancelled');assert.equal(row.confirmation,null);assert.ok(!cancel.isError);}
  else{assert.equal(row.state,'queued');assert.equal(response.outcome,'approved');assert.equal(cancel.isError,true);}
 }
 f.sql.close();
});
test('conflicting concurrent cancellation and phone IDs are reserved before effects',async()=>{
 const f=await maintenanceReplyFixture(),a=await f.submit('one'),b=await f.submit('two');
 const cancellations=await Promise.all([f.call('maintenance.cancel',{id:'one'},'shared'),f.call('maintenance.cancel',{id:'two'},'shared')]);
 assert.equal(cancellations.filter(x=>!x.isError).length,1);
 assert.equal(f.sql.prepare("SELECT count(*) n FROM maintenance_jobs WHERE state='cancelled'").get().n,1);
 const c=await f.submit('three'),d=await f.submit('four');
 const responses=await Promise.all([f.answer(c,'approve','','same-phone-id'),f.answer(d,'approve','','same-phone-id')]);
 assert.deepEqual(responses.map(x=>x.status).sort(),[200,403]);
 assert.equal(f.sql.prepare("SELECT count(*) n FROM maintenance_jobs WHERE state='queued'").get().n,1);f.sql.close();
});
test('interrupted reservations and transition receipts resume without another effect',async()=>{
 const f=await maintenanceReplyFixture(),card=await f.submit('resume');
 const request={target:'resume',reason:'reset',owner:'owner'};
 f.sql.prepare('INSERT INTO maintenance_audit(id,action,host,body,created) VALUES(?,?,?,?,?)').run('cancel:resume-cancel','maintenance.cancel',null,JSON.stringify({request,response:null}),1);
 assert.equal(JSON.parse((await f.call('maintenance.cancel',{id:'resume',reason:'reset'},'resume-cancel')).content[0].text).state,'cancelled');
 const card2=await f.submit('phone-resume'),payload={...card2,choice:'approve',text:''},envelope=f.signed('respond',payload,'phone-resume-id');
 f.sql.prepare('INSERT INTO maintenance_audit(id,action,host,body,created) VALUES(?,?,?,?,?)').run('phone:phone-resume-id','maintenance.confirmation','mac',JSON.stringify({signed_envelope:envelope,response_pending:true}),1);
 const first=await(await f.send('/api/decisions',envelope)).json();assert.equal(first.outcome,'approved');
 f.sql.prepare('UPDATE maintenance_audit SET body=? WHERE id=?').run(JSON.stringify({signed_envelope:envelope,response_pending:true}),'phone:phone-resume-id');
 const resumed=await(await f.send('/api/decisions',envelope)).json();assert.deepEqual(resumed,first);f.sql.close();
});


test('controller catalog and every native continuation stay on the nondefault owner',async()=>{
 const {sql,send,rpc,owner,env}=fixture();
 const mac='a'.repeat(64),pi='b'.repeat(64),token='d'.repeat(64);
 env.CONTROLLER_COORDINATION_MODE='local';env.LOCAL_CONTROLLER_ID='mac';env.CONTROLLER_NODE_MAP=JSON.stringify({[mac]:'mac',[pi]:'pi'});
 sql.prepare("INSERT INTO controller_config(id,site,enabled) VALUES(1,'catalog',1)").run();
 sql.prepare("INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES('mac',?,0,1,0,0)").run(sha('mac-token'));
 sql.prepare("INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES('pi',?,1,1,?,1)").run(sha(token),Date.now()/1000);
 const catalog=await(await send('/mcp',rpc('tools/call',{name:'biome_discover',arguments:{section:'controllers'}}),owner)).json();
 let args=JSON.parse(catalog.result.content[0].text).controllers.find(c=>c.controller===pi).next_call;
 for(let page=0;page<3;page++){
  const pending=send('/mcp',rpc('tools/call',{name:'biome_invoke',arguments:args}),owner);let job;
  for(let n=0;n<100;n++){await new Promise(r=>setTimeout(r,5));job=await(await send('/bridge/next',{}, {Authorization:'Bearer '+token})).json();if(job.id)break;}
  assert.equal(job.request.arguments.controller,pi);assert.equal(job.request.arguments.controller_owner,'pi');
  assert.deepEqual(job.request.arguments.arguments,page?{cursor:'page-'+page}:{section:'offers'});
  const body={section:'offers',items:[],complete:page===2};
  if(page<2)body.next_call={operation:'system.discovery',arguments:{cursor:'page-'+(page+1)},request_id:'next-'+page};
  await send('/bridge/complete',{id:job.id,lease:job.lease,result:{content:[{type:'text',text:JSON.stringify(body)}]}},{Authorization:'Bearer '+token});
  const result=JSON.parse((await(await pending).json()).result.content[0].text);assert.equal(result.controller,pi);
  if(page<2){args=result.next_call;assert.equal(args.operation,'controller.invoke');assert.equal(args.controller,undefined);assert.equal(args.arguments.controller,pi);}
 }
 assert.equal(sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE json_extract(request,'$.arguments.controller_owner')='mac'").get().n,0);sql.close();
});
