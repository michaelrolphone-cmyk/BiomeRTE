import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import {device,hexHash} from '../lib/bridge.mjs';
async function fixture(){
 const sql=new DatabaseSync(':memory:');for(const f of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+f,'utf8'));
 const a='a'.repeat(64),b='b'.repeat(64),tokens={a:'1'.repeat(64),b:'2'.repeat(64)};
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'site',1)");
 for(const id of ['a','b'])sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity) VALUES(?,?,1,2)').run(id,await hexHash(tokens[id]));
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'a',CONTROLLER_NODE_MAP:JSON.stringify({[a]:'a',[b]:'b'})};
 const send=(path,data,who='a')=>device(new Request('https://gateway'+path,{method:'POST',headers:{Authorization:'Bearer '+tokens[who]},body:JSON.stringify(data)}),env,path);
 const bytes=new TextEncoder().encode('{"signed":"packet"}');const packet={session:'session',to:b,transfer:await hexHash(new TextDecoder().decode(bytes)),index:0,total:1,bytes:bytes.length,chunk:btoa(String.fromCharCode(...bytes))};
 return {sql,env,send,packet,a,b};
}
async function complete(f,pending,override=null){let job;for(let i=0;i<100;i++){job=f.sql.prepare("SELECT * FROM bridge_jobs WHERE state='queued' LIMIT 1").get();if(job)break;await new Promise(r=>setTimeout(r,5));}assert.ok(job);
 const args=JSON.parse(job.request).arguments,receipt={accepted:true,transfer:args.transfer,index:args.index,complete:true,receipt:{session:args.session}};
 f.sql.prepare("UPDATE bridge_jobs SET state='claimed',controller='b',lease='lease',lease_until=? WHERE id=?").run(Date.now()/1000+60,job.id);
 const response=await f.send('/bridge/complete',{id:job.id,lease:'lease',result:override??{structuredContent:receipt,content:[{type:'text',text:JSON.stringify(receipt)}]}},'b');assert.equal(response.status,200);
 return {response:await pending,job,args};
}
test('authenticated packets route after session deletion and completion scrubs payload while retry recovers',async()=>{const f=await fixture();const {response,job,args}=await complete(f,f.send('/bridge/ha/packet',f.packet));assert.equal(response.status,200);assert.equal(args.from,f.a);assert.equal(args.controller_owner,'b');assert.equal(args.chunk,f.packet.chunk);assert.equal(JSON.parse(job.request).name,'biome_session_packet');
 const saved=JSON.parse(f.sql.prepare('SELECT request FROM bridge_jobs WHERE id=?').get(job.id).request);assert.equal(saved.arguments.chunk,undefined);assert.equal(typeof saved.arguments.chunk_digest,'string');
 const retry=await f.send('/bridge/ha/packet',f.packet);assert.equal(retry.status,200);assert.equal((await retry.json()).result.accepted,true);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);f.sql.close();});
test('packet bounds, spoofed source and unmapped destination fail before queueing',async()=>{const f=await fixture();for(const change of [{bytes:67108865},{total:2},{index:1},{chunk:'!bad'},{from:f.b},{bytes:1},{to:'c'.repeat(64)}])assert.ok((await f.send('/bridge/ha/packet',{...f.packet,...change})).status>=400);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);f.sql.close();});
test('same transfer position with changed bytes cannot reuse a cached receipt',async()=>{const f=await fixture();await complete(f,f.send('/bridge/ha/packet',f.packet));const chunk=btoa('x'.repeat(f.packet.bytes));const response=await f.send('/bridge/ha/packet',{...f.packet,chunk});assert.equal(response.status,503);f.sql.close();});
test('per-sender packet pending quota rejects without unbounded admission',async()=>{const f=await fixture();for(let i=0;i<8;i++)f.sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,?,'queued',?,?)").run('packet-'+i,JSON.stringify({arguments:{from:f.a}}),Date.now()/1000+60,Date.now()/1000);assert.equal((await f.send('/bridge/ha/packet',f.packet)).status,503);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,8);f.sql.close();});
test('packet cache is count-bounded and expired transport bodies disappear on heartbeat',async()=>{const f=await fixture(),now=Date.now()/1000;
 for(let i=0;i<140;i++)f.sql.prepare("INSERT INTO bridge_jobs(id,request,state,result,deadline,updated) VALUES(?,'{}','done','{}',?,?)").run('packet-old-'+i,now+60,now-i);
 await complete(f,f.send('/bridge/ha/packet',f.packet));assert.ok(f.sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE id LIKE 'packet-%'").get().n<=129);
 f.sql.prepare("UPDATE bridge_jobs SET updated=? WHERE id LIKE 'packet-%'").run(now-3601);
 const heartbeat=await f.send('/bridge/ha/heartbeat',{position:0,ready:true,load:0,host:'a'});assert.equal(heartbeat.status,200);assert.equal(f.sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE id LIKE 'packet-%'").get().n,0);f.sql.close();});

test('explicit structured recipient rejection preserves status and never becomes a retryable transport failure',async()=>{
 for(const status of [400,401,403,404,409,410,413,422]){
  const f=await fixture();const rejection={isError:true,structuredContent:{error:{status,message:'Recipient rejected packet'}},content:[{type:'text',text:'Recipient rejected packet'}]};
  const {response}=await complete(f,f.send('/bridge/ha/packet',f.packet),rejection);assert.equal(response.status,status);assert.equal((await response.json()).retryable,false);
  const retry=await f.send('/bridge/ha/packet',f.packet);assert.equal(retry.status,status);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);f.sql.close();
 }
});
test('unstructured or server-side recipient failures remain uncertain and retryable',async()=>{
 for(const error of [{status:503,message:'Storage unavailable'},{status:200,message:'Invalid failure shape'},null]){
  const f=await fixture();const result={isError:true,structuredContent:{error},content:[{type:'text',text:'Uncertain result'}]};const {response}=await complete(f,f.send('/bridge/ha/packet',f.packet),result);assert.equal(response.status,503);assert.equal((await response.json()).retryable,true);f.sql.close();
 }
});
