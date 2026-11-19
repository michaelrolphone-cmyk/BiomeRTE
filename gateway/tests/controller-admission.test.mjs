import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {claimControllerJob,controllerStatus} from '../lib/ha.mjs';

function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const now=Date.now()/1000;
 sql.prepare("INSERT INTO controller_config(id,site,enabled) VALUES(1,'test-site',1)").run();
 sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready,load) VALUES(?,?,0,2,?,1,0)').run('pi','test-token',now);
 sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES('direction',?,'queued',?,?)")
  .run(JSON.stringify({name:'biome_invoke',arguments:{realm:'site',operation:'cap.invoke'}}),now+60,now);
 const queries=[];
 const DB={prepare(query){
  queries.push(query);
  // Model diagnostics being unavailable while native admission remains healthy.
  if(/checkpoint|controller_journal/.test(query))throw Error('Diagnostic store unavailable');
  const statement=sql.prepare(query);let args=[];
  return {bind(...values){args=values;return this},async first(){return statement.get(...args)},async all(){return {results:statement.all(...args)}},async run(){return statement.run(...args)}};
 }};
 return {sql,DB,queries,node:sql.prepare("SELECT * FROM controller_nodes WHERE id='pi'").get()};
}

test('controller inbox keeps dispatching while checkpoint diagnostics are unavailable',async()=>{
 const f=fixture();
 try{
  await assert.rejects(controllerStatus(f.DB),/Diagnostic store unavailable/);
  f.queries.length=0;
  const job=await claimControllerJob(f.DB,f.node);
  assert.equal(job.id,'direction');assert.equal(job.primary,'pi');
  assert.equal(f.sql.prepare("SELECT controller FROM bridge_jobs WHERE id='direction'").get().controller,'pi');
  assert.ok(!f.queries.some(query=>/checkpoint|controller_journal/.test(query)));
  assert.equal(f.queries.filter(query=>/SELECT COUNT\(\*\)/.test(query)).length,1);
 }finally{f.sql.close()}
});

test('lightweight admission still rejects unavailable and full controllers',async()=>{
 const f=fixture();
 try{
  assert.equal((await claimControllerJob(f.DB,{...f.node,ready:0})).idle,true);
  f.sql.prepare("UPDATE controller_nodes SET capacity=1 WHERE id='pi'").run();
  const node=f.sql.prepare("SELECT * FROM controller_nodes WHERE id='pi'").get();
  const first=await claimControllerJob(f.DB,node);assert.equal(first.id,'direction');
  assert.equal((await claimControllerJob(f.DB,node)).idle,true);
  assert.equal(f.sql.prepare("SELECT lease FROM bridge_jobs WHERE id='direction'").get().lease,first.lease);
 }finally{f.sql.close()}
});
