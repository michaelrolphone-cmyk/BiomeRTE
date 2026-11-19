import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {device,hexHash,readBridgeResult,runTool} from '../lib/bridge.mjs';
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec('PRAGMA foreign_keys=ON');
 for(const p of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+p,'utf8'));
 const DB={prepare(q){const stmt=sql.prepare(q);let args=[];return{bind(...a){args=a;return this},async first(){return stmt.get(...args)},async run(){return stmt.run(...args)},async all(){return{results:stmt.all(...args)}}}}};
 const token='a'.repeat(64),env={DB};
 const ready=hexHash(token).then(x=>hexHash(x+'\n')).then(x=>{env.BIOME_BRIDGE_PIN=x});
 const send=async(path,body,credential=token)=>{await ready;return device(new Request('https://test'+path,{method:'POST',headers:{Authorization:'Bearer '+credential},body:JSON.stringify(body)}),env,path)};
 sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated,lease) VALUES('test','{}','claimed',9999999999,0,'lease')").run();
 sql.prepare("UPDATE bridge_jobs SET lease_until=? WHERE id='test'").run(Math.floor(Date.now()/1000)+35);
 return{sql,DB,send};
}
test('chunk delivery retains a multi-megabyte Unicode MCP result and durable replay',async()=>{
 const {sql,DB,send}=fixture();
 const result={content:[{type:'text',text:'☃😀\\"'.repeat(280000)}],structuredContent:{everything:true}};
 const encoded=JSON.stringify(result),digest=await hexHash(encoded),identity={id:'test',lease:'lease',digest};
 // Use code points so this test also covers UTF-8 non-ASCII producers.
 const chunks=[];let chunk='',bytes=0;
 for(const c of encoded){const n=Buffer.byteLength(c);if(bytes+n>65536){chunks.push(chunk);chunk='';bytes=0}chunk+=c;bytes+=n}chunks.push(chunk);
 assert.equal((await send('/bridge/result-commit',{...identity,parts:chunks.length})).status,409);
 for(let part=0;part<chunks.length;part++)assert.equal((await send('/bridge/result-chunk',{...identity,part,data:chunks[part]})).status,200);
 assert.equal((await send('/bridge/result-chunk',{...identity,part:0,data:chunks[0]})).status,200);
 assert.equal((await send('/bridge/result-chunk',{...identity,part:0,data:'changed'})).status,409);
 assert.equal((await send('/bridge/result-commit',{...identity,parts:chunks.length})).status,200);
 assert.equal((await send('/bridge/result-commit',{...identity,parts:chunks.length})).status,200);
 assert.deepEqual(await readBridgeResult(DB,'test',sql.prepare("SELECT result FROM bridge_jobs WHERE id='test'").get().result),result);
 sql.prepare("DELETE FROM bridge_jobs WHERE id='test'").run();assert.equal(sql.prepare('SELECT count(*) n FROM bridge_result_chunks').get().n,0);sql.close();
});
test('authentication, lease fencing, digest validation and ordinary completions remain intact',async()=>{
 const {sql,DB,send}=fixture(),result={content:[{type:'text',text:'hello'}]},data=JSON.stringify(result),digest=await hexHash(data);
 const frame={id:'test',lease:'lease',digest,part:0,data};
 assert.equal((await send('/bridge/result-chunk',frame,'b'.repeat(64))).status,401);
 assert.equal((await send('/bridge/result-chunk',{...frame,lease:'stale'})).status,409);
 assert.equal((await send('/bridge/result-chunk',{...frame,digest:'0'.repeat(64)})).status,200);
 assert.equal((await send('/bridge/result-commit',{...frame,digest:'0'.repeat(64),parts:1})).status,400);
 assert.equal((await send('/bridge/complete',{id:'test',lease:'lease',result})).status,200);
 assert.deepEqual(await readBridgeResult(DB,'test',sql.prepare("SELECT result FROM bridge_jobs WHERE id='test'").get().result),result);sql.close();
});
test('chunk progress renews ownership beyond the initial lease, then stalls allow takeover',async(t)=>{
 const realNow=Date.now;let now=Math.floor(realNow()/1000)*1000;
 t.mock.method(Date,'now',()=>now);
 const {sql,send}=fixture(),digest='a'.repeat(64),frame={id:'test',lease:'lease',digest};
 // Upload lasts 90 seconds, much longer than the initial 35-second claim.
 for(let part=0;part<3;part++){
  now+=30000;
  assert.equal((await send('/bridge/result-chunk',{...frame,part,data:'x'})).status,200);
  assert.equal(sql.prepare("SELECT lease_until FROM bridge_jobs WHERE id='test'").get().lease_until,now/1000+35);
  assert.equal((await (await send('/bridge/next',{})).json()).idle,true,'active upload cannot be stolen');
 }
 // A stalled upload does not keep ownership forever. Extend only the inbox
 // eligibility as a caller retry would, not the controller claim itself.
 now+=36000;sql.prepare("UPDATE bridge_jobs SET deadline=? WHERE id='test'").run(now/1000+22);
 const taken=await (await send('/bridge/next',{})).json();assert.equal(taken.id,'test');assert.notEqual(taken.lease,'lease');
 assert.equal((await send('/bridge/result-chunk',{...frame,part:3,data:'x'})).status,409);
 assert.equal(sql.prepare("SELECT lease FROM bridge_jobs WHERE id='test'").get().lease,taken.lease);
 sql.close();
});
test('expired uploads cannot resurrect their lease without a new claim',async(t)=>{
 let now=Date.now();t.mock.method(Date,'now',()=>now);
 const {sql,send}=fixture();now+=36000;
 assert.equal((await send('/bridge/result-chunk',{id:'test',lease:'lease',digest:'a'.repeat(64),part:0,data:'x'})).status,409);
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_result_chunks').get().n,0);sql.close();
});
test('timed-out discovery resumes the same complete snapshot, while status reads remain fresh',async(t)=>{
 const realTimeout=setTimeout;t.mock.method(globalThis,'setTimeout',fn=>realTimeout(fn,0));
 const {sql,DB,send}=fixture(),env={DB};
 const args={operation:'cap.discover',arguments:{},request_id:'same-logical-discovery'};
 const first=await runTool(env,'biome_invoke',args);assert.equal(first.isError,true);
 const job=sql.prepare("SELECT id FROM bridge_jobs WHERE state='queued'").get();assert.ok(job);
 sql.prepare("UPDATE bridge_jobs SET state='claimed',lease='slow-upload',lease_until=? WHERE id=?").run(Date.now()/1000+35,job.id);
 const result={content:[{type:'text',text:'complete discovery snapshot'}]},data=JSON.stringify(result),digest=await hexHash(data);
 const identity={id:job.id,lease:'slow-upload',digest};
 assert.equal((await send('/bridge/result-chunk',{...identity,part:0,data})).status,200);
 assert.equal((await send('/bridge/result-commit',{...identity,parts:1})).status,200);
 assert.deepEqual(await runTool(env,'biome_invoke',args),result);
 assert.equal(sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE id!='test'").get().n,1,'retry did not orphan the slow result');
 let sequence=0;const ids=[];
 const timer=setInterval(()=>{
  for(const row of sql.prepare("SELECT id FROM bridge_jobs WHERE state='queued'").all()){
   ids.push(row.id);sql.prepare("UPDATE bridge_jobs SET state='done',result=? WHERE id=?").run(JSON.stringify({content:[{type:'text',text:String(++sequence)}]}),row.id);
  }
 },1);
 try{
  const status={operation:'cap.status',arguments:{},request_id:'same-status-request'};
  assert.notDeepEqual(await runTool(env,'biome_invoke',status),await runTool(env,'biome_invoke',status));
  assert.equal(new Set(ids).size,2);
  await runTool(env,'biome_invoke',{...args,request_id:'fresh-discovery'});
  assert.equal(new Set(ids).size,3,'a new discovery request_id produces a fresh snapshot');
 }finally{clearInterval(timer);sql.close()}
});
