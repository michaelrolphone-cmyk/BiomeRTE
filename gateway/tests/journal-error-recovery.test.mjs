import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {enqueue} from '../lib/bridge.mjs';
import {sqliteD1} from './sqlite-d1.mjs';

const error = text => ({isError:true,content:[{type:'text',text}]});
const answer = {content:[{type:'text',text:'durable result'}]};
const args = {arguments:{operation:'existing-operation'},operation:'cap.submit',request_id:'original-request'};
const payload = JSON.stringify({arguments:args,name:'biome_invoke'});
function fixture(result=error('Gateway journal HTTP 503'),age=10) {
 const sql=new DatabaseSync(':memory:');
 for(const p of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+p,'utf8'));
 const now=Math.floor(Date.now()/1000);
 sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated,lease,lease_until,result) VALUES('invoke-original',?,'done',?,?, 'old-lease',?,?)")
  .run(payload,now+22,now-age,now+35,JSON.stringify(result));
 return {sql,env:{DB:sqliteD1(sql)},now};
}
function completeQueued(sql,observed) {
 return setInterval(()=>{
  for(const row of sql.prepare("SELECT * FROM bridge_jobs WHERE state='queued'").all()) {
   observed.push(row);
   sql.prepare("UPDATE bridge_jobs SET state='done',result=?,updated=? WHERE id=? AND state='queued'")
    .run(JSON.stringify(answer),Math.floor(Date.now()/1000),row.id);
  }
 },1);
}
test('a transient journal failure can recover with its unchanged logical identity',async()=>{
 const {sql,env}=fixture(),observed=[],timer=completeQueued(sql,observed);
 try {
  assert.deepEqual(await enqueue(env,'biome_invoke',args,'invoke-original'),answer);
  assert.equal(observed.length,1);
  assert.equal(observed[0].id,'invoke-original');
  assert.equal(observed[0].request,payload);
  assert.equal(observed[0].lease,null);
  assert.equal(observed[0].lease_until,null);
  assert.equal(observed[0].controller,null);
  assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);
  assert.deepEqual(await enqueue(env,'biome_invoke',args,'invoke-original'),answer);
  assert.equal(observed.length,1,'success is never resubmitted');
 } finally {clearInterval(timer);sql.close()}
});
test('concurrent retries do not create multiple bridge dispatches',async()=>{
 const {sql,env}=fixture(),observed=[],timer=completeQueued(sql,observed);
 try {
  const results=await Promise.all(Array.from({length:8},()=>enqueue(env,'biome_invoke',args,'invoke-original')));
  assert.ok(results.every(x=>JSON.stringify(x)===JSON.stringify(answer)));
  assert.equal(observed.length,1);
 } finally {clearInterval(timer);sql.close()}
});
test('successful and business-error results remain final',async()=>{
 for(const result of [answer,error('Unknown contract'),error('Permission denied'),error('provider said Gateway journal HTTP 503'),{content:[{type:'text',text:'Gateway journal HTTP 503'}]}]) {
  const {sql,env}=fixture(result);
  try {
   assert.deepEqual(await enqueue(env,'biome_invoke',args,'invoke-original'),result);
   assert.equal(sql.prepare('SELECT state FROM bridge_jobs').get().state,'done');
  } finally {sql.close()}
 }
});
test('cooldown and queue admission still apply to recovery',async()=>{
 for(const busy of [false,true]) {
  const result=error('Gateway journal HTTP 503'),{sql,env,now}=fixture(result,busy?10:0);
  if(busy)for(let i=0;i<40;i++)sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,'{}','queued',?,?)").run('busy-'+i,now+60,now);
  try {
   assert.deepEqual(await enqueue(env,'biome_invoke',args,'invoke-original'),result);
   assert.equal(sql.prepare("SELECT state FROM bridge_jobs WHERE id='invoke-original'").get().state,'done');
  } finally {sql.close()}
 }
});
test('changed input never inherits the failed request identity',async()=>{
 const {sql,env}=fixture();
 try {
  const r=await enqueue(env,'biome_invoke',{...args,arguments:{operation:'different-operation'}},'invoke-original');
  assert.match(r.content[0].text,/different request/);
  assert.equal(sql.prepare('SELECT state FROM bridge_jobs').get().state,'done');
 } finally {sql.close()}
});
