import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {controllerSocket} from '../lib/transport.mjs';

const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(predicate){
 const end=Date.now()+1500;
 while(!predicate()){
  assert.ok(Date.now()<end,'Expected socket frame was not emitted');
  await sleep(5);
 }
}

async function fixture(run,beforeFirst=async()=>{}){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const now=Date.now()/1000;
 sql.prepare('INSERT INTO controller_config(id,site,enabled) VALUES(1,?,1)').run('test-site');
 sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,0,1,?,1)').run('test-node','test-token-hash',now);
 sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,?,'queued',?,?)")
  .run('test-job',JSON.stringify({name:'biome_native_call',arguments:{body:'unchanged signed envelope'}}),now+60,now);
 const DB={prepare(query){const statement=sql.prepare(query);let args=[];return {
  bind(...values){args=values;return this},async first(){await beforeFirst(query,args);return statement.get(...args)},
  async run(){return statement.run(...args)},async all(){return {results:statement.all(...args)}}}}};
 class Socket{
  constructor(){this.listeners={};this.frames=[]}
  accept(){}
  addEventListener(name,listener){(this.listeners[name]??=[]).push(listener)}
  send(frame){this.frames.push(JSON.parse(frame))}
  receive(frame){for(const listener of this.listeners.message??[])listener({data:JSON.stringify(frame)})}
  close(){for(const listener of this.listeners.close??[])listener({})}
 }
 const previousResponse=globalThis.Response,previousPair=globalThis.WebSocketPair;
 const client=new Socket(),server=new Socket();
 globalThis.WebSocketPair=class{constructor(){this[0]=client;this[1]=server}};
 globalThis.Response=class{constructor(body,options){this.status=options.status;this.webSocket=options.webSocket}};
 try{
  const response=controllerSocket(new Request('https://test.invalid/bridge/connect',{headers:{Upgrade:'websocket'}}),{DB},{id:'test-node'});
  assert.equal(response.status,101);
  await until(()=>server.frames.some(frame=>frame.type==='job'));
  const job=server.frames.find(frame=>frame.type==='job');
  await run({sql,server,job});
 }finally{
  server.close();
  await sleep(120);
  globalThis.Response=previousResponse;
  if(previousPair===undefined)delete globalThis.WebSocketPair;else globalThis.WebSocketPair=previousPair;
  sql.close();
 }
}

test('completion ACK echoes exactly the accepted durable job lease',async()=>{
 await fixture(async({sql,server,job})=>{
  const result={content:[{type:'text',text:'native response'}]};
  server.receive({type:'complete',id:job.id,lease:job.lease,result});
  await until(()=>server.frames.some(frame=>frame.type==='accepted'));
  assert.deepEqual(server.frames.find(frame=>frame.type==='accepted'),
   {type:'accepted',id:job.id,lease:job.lease,accepted:true});
  const row=sql.prepare('SELECT state,result,lease FROM bridge_jobs WHERE id=?').get(job.id);
  assert.equal(row.state,'done');assert.equal(row.lease,job.lease);assert.deepEqual(JSON.parse(row.result),result);
 });
});

test('wrong socket lease remains ignored and ownership rejection carries its exact lease',async()=>{
 await fixture(async({sql,server,job})=>{
  const result={content:[]};
  server.receive({type:'complete',id:job.id,lease:'stale-lease',result});
  await sleep(10);
  assert.equal(server.frames.filter(frame=>frame.type==='accepted').length,0);
  assert.equal(sql.prepare('SELECT state FROM bridge_jobs WHERE id=?').get(job.id).state,'claimed');
  sql.prepare('UPDATE bridge_jobs SET controller=? WHERE id=?').run('new-controller',job.id);
  server.receive({type:'complete',id:job.id,lease:job.lease,result});
  await until(()=>server.frames.some(frame=>frame.type==='accepted'));
  assert.deepEqual(server.frames.find(frame=>frame.type==='accepted'),
   {type:'accepted',id:job.id,lease:job.lease,accepted:false});
  assert.equal(sql.prepare('SELECT state FROM bridge_jobs WHERE id=?').get(job.id).state,'claimed');
 });
});

test('a delayed old completion cannot delete a newer delivery for the same job',async()=>{
 let release,blocked=false;
 const held=new Promise(resolve=>{release=resolve});
 try{
  await fixture(async({sql,server,job})=>{
   server.receive({type:'complete',id:job.id,lease:job.lease,result:{content:[]}});
   await until(()=>blocked);
   sql.prepare('UPDATE bridge_jobs SET lease_until=? WHERE id=?').run(Date.now()/1000-1,job.id);
   await until(()=>server.frames.filter(frame=>frame.type==='job').length===2);
   const replacement=server.frames.filter(frame=>frame.type==='job')[1];
   assert.notEqual(replacement.lease,job.lease);
   release();
   await until(()=>server.frames.some(frame=>frame.type==='accepted'&&frame.lease===job.lease));
   assert.equal(server.frames.find(frame=>frame.type==='accepted'&&frame.lease===job.lease).accepted,false);
   server.receive({type:'complete',id:replacement.id,lease:replacement.lease,result:{content:[]}});
   await until(()=>server.frames.some(frame=>frame.type==='accepted'&&frame.lease===replacement.lease));
   assert.equal(server.frames.find(frame=>frame.type==='accepted'&&frame.lease===replacement.lease).accepted,true);
   assert.equal(sql.prepare('SELECT state FROM bridge_jobs WHERE id=?').get(job.id).state,'done');
  },async query=>{
   if(query.startsWith("UPDATE bridge_jobs SET state='done'")&&!blocked){blocked=true;await held}
  });
 }finally{release()}
});
