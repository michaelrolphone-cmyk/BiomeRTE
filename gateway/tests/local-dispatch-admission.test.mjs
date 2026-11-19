import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import {claimControllerJob} from '../lib/ha.mjs';

function fixture(){
 const sql=new DatabaseSync(':memory:'),now=Date.now()/1000;
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'test',1)");
 for(const id of ['mac','pi'])sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,0,2,?,1)').run(id,id,now);
 const base=sqliteD1(sql),queries=[];
 const db={prepare(q){queries.push(q);return base.prepare(q)}};
 let serial=0;
 const add=(owner='mac',options={})=>{
  const id='job-'+(++serial),request={name:'biome_invoke',arguments:{operation:'provider.poll',...(owner===null?{}:{controller_owner:owner})}};
  sql.prepare('INSERT INTO bridge_jobs(id,request,state,deadline,updated,controller,lease,lease_until) VALUES(?,?,?,?,?,?,?,?)').run(id,JSON.stringify(request),options.state??'queued',options.deadline??now+100,now-1,options.controller??null,'old-lease',options.leaseUntil??now+35);
  return id;
 };
 const claim=(id='mac')=>claimControllerJob(db,{id,enabled:1,ready:1,capacity:999,seen:now},{CONTROLLER_COORDINATION_MODE:'local'});
 return {sql,queries,add,claim,now};
}

test('local dispatch uses one database round trip and keeps exact controller affinity',async()=>{
 const f=fixture();try{
  f.add('pi');f.add(null);const id=f.add('mac');
  const job=await f.claim();assert.equal(job.id,id);assert.equal(job.primary,'mac');assert.notEqual(job.lease,'old-lease');
  assert.equal(f.queries.length,1);
  assert.equal(f.sql.prepare('SELECT controller FROM bridge_jobs WHERE id=?').get(id).controller,'mac');
 }finally{f.sql.close()}
});

test('concurrent socket and HTTP claims respect live capacity despite stale caller metadata',async()=>{
 const f=fixture();try{
  for(let i=0;i<6;i++)f.add();
  const jobs=await Promise.all(Array.from({length:6},()=>f.claim()));
  const claimed=jobs.filter(j=>j.id);assert.equal(claimed.length,2);assert.equal(new Set(claimed.map(j=>j.id)).size,2);
  f.sql.prepare("UPDATE bridge_jobs SET state='done' WHERE id=?").run(claimed[0].id);
  assert.ok((await f.claim()).id);
 }finally{f.sql.close()}
});

for(const [label,update] of [
 ['disabled cluster','UPDATE controller_config SET enabled=0'],
 ['disabled controller',"UPDATE controller_nodes SET enabled=0 WHERE id='mac'"],
 ['unready controller',"UPDATE controller_nodes SET ready=0 WHERE id='mac'"],
 ['stale heartbeat',"UPDATE controller_nodes SET seen=0 WHERE id='mac'"],
 ['reduced capacity',"UPDATE controller_nodes SET capacity=0 WHERE id='mac'"]
])test('local admission rechecks '+label+' inside the claim',async()=>{
 const f=fixture();try{f.add();f.sql.exec(update);assert.equal((await f.claim()).idle,true)}finally{f.sql.close()}
});

test('live claims and expired dispatch deadlines stay untouched; expired leases get a new fence',async()=>{
 const f=fixture();try{
  f.add('mac',{deadline:f.now-1});
  f.add('mac',{state:'claimed',controller:'mac'});
  const expired=f.add('mac',{state:'claimed',controller:'mac',leaseUntil:f.now-1});
  const job=await f.claim();assert.equal(job.id,expired);assert.notEqual(job.lease,'old-lease');
  assert.equal((await f.claim()).idle,true);
 }finally{f.sql.close()}
});

test('stale prior owner cannot change the destination controller',async()=>{
 const f=fixture();try{
  f.sql.exec("UPDATE controller_nodes SET seen=0 WHERE id='pi'");
  const recover=f.add('mac',{state:'claimed',controller:'pi'});
  f.add('pi',{state:'claimed',controller:'pi'});
  assert.equal((await f.claim()).id,recover);assert.equal((await f.claim()).idle,true);
 }finally{f.sql.close()}
});
