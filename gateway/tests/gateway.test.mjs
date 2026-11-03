import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
const base='https://biome.test';
const sha=v=>createHash('sha256').update(v).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec(readFileSync('drizzle/0000_biome_gateway.sql','utf8'));
 const DB={prepare(query){let args=[];const stmt=sql.prepare(query);return {bind(...values){args=values;return this},async first(){return stmt.get(...args)},async run(){return stmt.run(...args)},async all(){return {results:stmt.all(...args)}}}}};
 const deviceKey='f'.repeat(64);
 const env={DB,BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',BIOME_BRIDGE_PIN:sha(sha(deviceKey)+'\n')};
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
 const list=await (await send('/mcp',rpc('tools/list'))).json();assert.equal(list.result.tools.length,4);sql.close();
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
 assert.equal(list.result.events[0].name,'workflow.delegation.requested');
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
