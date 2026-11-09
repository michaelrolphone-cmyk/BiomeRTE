import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
const base='https://biome.test';
const sha=v=>createHash('sha256').update(v).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const DB={prepare(q){let args=[];const statement=sql.prepare(q);return {
  bind(...v){args=v;return this},async first(){return statement.get(...args)},
  async run(){return statement.run(...args)},async all(){return {results:statement.all(...args)}}
 }}};
 const token='f'.repeat(64),env={DB,BIOME_BRIDGE_PIN:sha(sha(token)+'\n')};
 const send=(path,body,headers={})=>gateway.fetch(new Request(base+path,{
  method:'POST',headers:{'Content-Type':'application/json',...headers},
  body:typeof body==='string'?body:JSON.stringify(body)}),env);
 const controller={Authorization:'Bearer '+token};
 const next=async()=>{for(let n=0;n<200;n++){const value=await(await send('/bridge/next',{},controller)).json();if(value.id)return value;await new Promise(r=>setTimeout(r,5))}throw Error('No queued request')};
 const complete=(job,status,body)=>send('/bridge/complete',{id:job.id,lease:job.lease,
  result:{content:[{type:'text',text:JSON.stringify({status,body})}]}},controller);
 return {sql,send,next,complete};
}
const actor='a'.repeat(64),signature=Buffer.alloc(256,17).toString('base64');
const packet=(operation='provider.poll',arguments_={provider:actor,limit:1})=>({
 site:'site-one',realm:'site',id:'stable-id',operation,arguments:arguments_,
 auth:{actor,signature}
});
test('native provider ingress rejects unsigned and owner operations before queueing',async()=>{
 const f=fixture();
 assert.equal((await f.send('/api/provider/call',{})).status,401);
 assert.equal((await f.send('/api/provider/call',packet('peer.trust',{}))).status,400);
 assert.equal((await f.send('/api/provider/call',packet('cap.define',{}))).status,400);
 assert.equal((await f.send('/api/provider/call',packet('provider.poll',{provider:'b'.repeat(64)}))).status,400);
 assert.equal((await f.send('/api/provider/call',packet(),{Origin:'https://wrong.test'})).status,403);
 assert.equal((await f.send('/api/provider/call',packet(),{'Content-Type':'text/plain'})).status,415);
 assert.equal(f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n,0);
 f.sql.close();
});
test('exact signed JSON survives transport and each retry rechecks live authorization',async()=>{
 const f=fixture();
 const p=packet('cap.offer',{provider:actor,contract:'relay.bank:1',resource:'board',configuration:{number:1}});
 const raw=JSON.stringify(p).replace('"number":1','"number":1.0,"big":9007199254740993');
 let previous;
 for(const [status,body] of [[200,{result:{id:'offer'}}],[403,{error:'Revoked credential'}]]){
  const waiting=f.send('/api/provider/call',raw),job=await f.next();
  assert.equal(job.request.name,'signed_provider_call');
  assert.deepEqual(job.request.arguments,{envelope:raw});
  assert.notEqual(job.id,previous);previous=job.id;
  await f.complete(job,status,body);
  const reply=await waiting;assert.equal(reply.status,status);assert.deepEqual(await reply.json(),body);
 }
 assert.equal(f.sql.prepare('SELECT count(*) AS n FROM bridge_jobs').get().n,2);
 f.sql.close();
});
test('public pairing forwards candidate and proof only; approval never exposed',async()=>{
 const f=fixture();
 assert.equal((await f.send('/api/provider/pair/approve',{fingerprint:actor})).status,404);
 const beginning={public_key:'-----BEGIN PUBLIC KEY-----\nexample\n-----END PUBLIC KEY-----',label:'Biome Relay test'};
 const proof={challenge:'1'.repeat(64),site:'site-one',key:actor,signature};
 for(const [phase,body,status,result] of [
  ['begin',beginning,200,{result:{challenge:proof.challenge,site:proof.site,key:actor}}],
  ['finish',proof,403,{error:'Fingerprint not approved'}]
 ]){
  const waiting=f.send('/api/provider/pair/'+phase,body),job=await f.next();
  assert.equal(job.request.name,'signed_provider_pair');
  assert.equal(job.request.arguments.phase,phase);
  assert.deepEqual(JSON.parse(job.request.arguments.envelope),body);
  await f.complete(job,status,result);
  const response=await waiting;assert.equal(response.status,status);assert.deepEqual(await response.json(),result);
 }
 assert.equal((await f.send('/api/provider/pair/begin',{...beginning,scopes:['*']})).status,400);
 f.sql.close();
});
