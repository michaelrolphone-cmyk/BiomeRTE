import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
function fixture(extra={}){
 const sql=new DatabaseSync(':memory:');for(const f of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+f,'utf8'));
 sql.exec("INSERT INTO controller_config(id,site) VALUES(1,'site')");
 const DB={prepare(q){const stmt=sql.prepare(q);let a=[];return {bind(...x){a=x;return this},async first(){return stmt.get(...a)},async run(){return stmt.run(...a)},async all(){return {results:stmt.all(...a)}}}}};
 const body={site:'site',id:'native id é',realm:'site',operation:'system.info',arguments:{n:1},auth:{actor:'a'.repeat(64),signature:'AAAA'}};
 const send=(raw=JSON.stringify(body),headers={})=>gateway.fetch(new Request('https://biome.test/v1/call',{method:'POST',headers:{'content-type':'application/json',...headers},body:raw}),{DB,...extra});
 return {sql,body,send,DB};
}
async function complete(sql,pending,status=200,body={result:{ok:true}}){
 let job;for(let i=0;i<100;i++){job=sql.prepare("SELECT * FROM bridge_jobs WHERE state='queued' ORDER BY updated,id LIMIT 1").get();if(job)break;await new Promise(r=>setTimeout(r,5))}
 assert.ok(job,'Native job queued');
 sql.prepare("UPDATE bridge_jobs SET state='done',result=? WHERE id=?").run(JSON.stringify({content:[{type:'text',text:JSON.stringify({status,body})}]}),job.id);
 return {response:await pending,job};
}
test('native raw bytes including floating spelling survive; every retry gets new transport job',async()=>{
 const {sql,body,send}=fixture();const raw=JSON.stringify(body).replace('"n":1','"n":1.0');
 const first=await complete(sql,send(raw));assert.equal(first.response.status,200);
 assert.equal(JSON.parse(first.job.request).arguments.body,raw);
 assert.equal(JSON.parse(first.job.request).name,'biome_native_call');
 const second=await complete(sql,send(raw),403,{error:'Revoked credential'});
 assert.notEqual(second.job.id,first.job.id);assert.equal(second.response.status,403);
 assert.deepEqual(await second.response.json(),{error:'Revoked credential'});sql.close();
});
test('reject unsigned, routing overrides, wrong site, local realm and foreign origins before queue',async()=>{
 const {sql,body,send}=fixture();
 for(const change of [x=>delete x.auth,x=>x.url='https://evil.test',x=>x.node='pi',x=>x.realm='local',x=>x.site='other',x=>x.auth.actor='admin']){
  const e=structuredClone(body);change(e);assert.ok((await send(JSON.stringify(e))).status>=400);
 }
 assert.equal((await send(JSON.stringify(body),{Origin:'https://evil.test'})).status,403);
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);sql.close();
});
test('forged but structurally valid signature reaches controller and preserves denial, never operator authority',async()=>{
 const {sql,send}=fixture();const {response,job}=await complete(sql,send(),403,{error:'Invalid signature'});
 assert.equal(response.status,403);assert.deepEqual(Object.keys(JSON.parse(job.request).arguments).sort(),['body','realm','site']);sql.close();
});
test('native payloads cross former parser and queue limits without truncation',async()=>{
 const {sql,body,send}=fixture();
 for(const size of [50000,97000,126000,1100000]){
  body.arguments={text:'x'.repeat(size)};const raw=JSON.stringify(body);
  const {response,job}=await complete(sql,send(raw));assert.equal(response.status,200);
  assert.equal(JSON.parse(job.request).arguments.body,raw);
 }
 sql.close();
});
test('transport failures and backend5xx become retryable503',async()=>{
 const {sql,send}=fixture();const {response}=await complete(sql,send(),503,{error:'Uncertain'});assert.equal(response.status,503);assert.equal((await response.json()).retryable,true);
 for(let i=0;i<40;i++)sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,'{}','queued',?,?)").run('busy-'+i,Date.now()/1000+100,Date.now()/1000);
 const busy=await send();assert.equal(busy.status,503);sql.close();
});

test('signed explicit controller resolves configured application identity without default remap',async()=>{
 const controller='b'.repeat(64);const {sql,body,send}=fixture({CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',CONTROLLER_NODE_MAP:JSON.stringify({[controller]:'pi'})});
 sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity) VALUES(?,?,?,?)').run('pi','c'.repeat(64),2,1);
 body.controller=controller;const raw=JSON.stringify(body);const {response,job}=await complete(sql,send(raw));assert.equal(response.status,200);
 const args=JSON.parse(job.request).arguments;assert.equal(args.controller_owner,'pi');assert.equal(args.controller,controller);assert.equal(args.body,raw);
 body.controller='d'.repeat(64);assert.equal((await send(JSON.stringify(body))).status,409);sql.close();
});

test('OAuth controller selectors pin owners and scope mutation deduplication',async()=>{
 const {runTool}=await import('../lib/bridge.mjs');const {sql,DB}=fixture();const a='a'.repeat(64),b='b'.repeat(64);
 for(const [id,hash] of [['pi',a],['mac',b]])sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity) VALUES(?,?,1,1)').run(id,hash);
 const env={DB,CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',CONTROLLER_NODE_MAP:JSON.stringify({[a]:'pi',[b]:'mac'})};
 const call=controller=>runTool(env,'biome_invoke',{controller,operation:'cap.acquire',arguments:{},request_id:'same-id'});
 const first=await complete(sql,call(a));const second=await complete(sql,call(b));
 assert.notEqual(first.job.id,second.job.id);assert.equal(JSON.parse(first.job.request).arguments.controller_owner,'pi');assert.equal(JSON.parse(second.job.request).arguments.controller_owner,'mac');
 assert.equal((await call('c'.repeat(64))).isError,true);sql.close();
});
