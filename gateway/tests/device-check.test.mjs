import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import {device,hexHash} from '../lib/bridge.mjs';

async function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const mac='a'.repeat(64),pi='b'.repeat(64),tokens={mac:'1'.repeat(64),pi:'2'.repeat(64),legacy:'3'.repeat(64)};
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'site',1)");
 for(const id of ['mac','pi'])sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,1,2,?,?)')
  .run(id,await hexHash(tokens[id]),id==='mac'?Date.now()/1000:0,id==='mac'?1:0);
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'pi',
  CONTROLLER_NODE_MAP:JSON.stringify({[mac]:'mac',[pi]:'pi'})};
 const send=(path,body={},who='mac',method='POST')=>device(new Request('https://gateway'+path,{
  method,headers:who?{Authorization:'Bearer '+(tokens[who]??who)}:{},
  ...(method==='POST'?{body:JSON.stringify(body)}:{})}),env,path);
 return {sql,env,send,mac,pi,tokens};
}
async function queued(f){
 for(let i=0;i<100;i++){
  const job=f.sql.prepare("SELECT * FROM bridge_jobs WHERE state='queued' ORDER BY updated,id LIMIT 1").get();
  if(job)return job;
  await new Promise(resolve=>setTimeout(resolve,5));
 }
 assert.fail('Delivery check did not enqueue a controller request');
}
async function finish(f,pending,owner='mac',result=null){
 const job=await queued(f),request=JSON.parse(job.request);
 const value=result??{content:[{type:'text',text:JSON.stringify({node:f[owner],health:{runtime:'up'}})}]};
 f.sql.prepare("UPDATE bridge_jobs SET state='claimed',controller=?,lease='delivery',lease_until=? WHERE id=?")
  .run(owner,Date.now()/1000+60,job.id);
 const complete={id:job.id,lease:'delivery',result:value};
 assert.equal((await f.send('/bridge/complete',complete,owner==='mac'?'pi':'mac')).status,409);
 assert.equal((await f.send('/bridge/complete',complete,owner)).status,200);
 return {response:await pending,job,request,value};
}

test('registered device checks its own route while the default controller is offline',async()=>{
 const f=await fixture();try{
  const {response,request,value}=await finish(f,f.send('/bridge/check'));
  assert.equal(response.status,200);
  assert.deepEqual(await response.json(),value);
  assert.equal(request.name,'biome_status');
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
 }finally{f.sql.close()}
});
test('body cannot redirect the authenticated device or choose a different operation',async()=>{
 const f=await fixture();try{
  const {request}=await finish(f,f.send('/bridge/check',{controller:f.pi,controller_owner:'pi',operation:'cap.acquire',arguments:{}}));
  assert.equal(request.name,'biome_status');
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
 }finally{f.sql.close()}
});
test('controller tool failure is preserved instead of synthesizing a healthy check',async()=>{
 const f=await fixture();try{
  const result={isError:true,content:[{type:'text',text:'Controller unavailable'}]};
  const {response}=await finish(f,f.send('/bridge/check'),'mac',result);
  assert.deepEqual(await response.json(),result);
 }finally{f.sql.close()}
});
test('missing, incorrect, and disabled device credentials never enqueue a check',async()=>{
 const f=await fixture();try{
  assert.equal((await f.send('/bridge/check',{},null)).status,401);
  assert.equal((await f.send('/bridge/check',{},'9'.repeat(64))).status,401);
  f.sql.prepare("UPDATE controller_nodes SET enabled=0 WHERE id='mac'").run();
  assert.equal((await f.send('/bridge/check')).status,401);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
test('registered device rejects absent, ambiguous, and malformed mappings without default fallback',async()=>{
 const f=await fixture();try{
  const cases=[
   [undefined,409],
   [JSON.stringify({[f.pi]:'pi'}),409],
   [JSON.stringify({[f.mac]:'mac',[f.pi]:'mac'}),409],
   ['{',503],
   ['null',503],
   ['[]',503],
   ['1',503]
  ];
  for(const [mapping,status] of cases){
   f.env.CONTROLLER_NODE_MAP=mapping;
   assert.equal((await f.send('/bridge/check')).status,status,String(mapping));
  }
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
test('only POST is accepted for an authenticated device check',async()=>{
 const f=await fixture();try{
  assert.equal((await f.send('/bridge/check',{},'mac','GET')).status,405);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
test('unregistered legacy bridge credential retains its existing default check',async()=>{
 const f=await fixture();try{
  f.env.BIOME_BRIDGE_PIN=await hexHash(await hexHash(f.tokens.legacy)+'\n');
  const {request}=await finish(f,f.send('/bridge/check',{},'legacy'),'pi');
  assert.equal(request.name,'biome_status');
  assert.deepEqual(request.arguments,{controller_owner:'pi'});
 }finally{f.sql.close()}
});
test('registered identity takes precedence when the credential also matches legacy authorization',async()=>{
 const f=await fixture();try{
  f.env.BIOME_BRIDGE_PIN=await hexHash(await hexHash(f.tokens.mac)+'\n');
  const {request}=await finish(f,f.send('/bridge/check'));
  assert.deepEqual(request.arguments,{controller:f.mac,controller_owner:'mac'});
 }finally{f.sql.close()}
});
