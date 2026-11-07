import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {generateKeyPairSync,createHash,sign} from 'node:crypto';
import {readFileSync} from 'node:fs';
import gateway from '../worker/index.js';
import {claimControllerJob} from '../lib/ha.mjs';
const pair=generateKeyPairSync('rsa',{modulusLength:2048});
const public_key=pair.publicKey.export({format:'pem',type:'spki'});
const actor=createHash('sha256').update(pair.publicKey.export({format:'der',type:'spki'})).digest('hex');
const canonical=v=>Array.isArray(v)?'['+v.map(canonical).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>canonical(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const f of ['0000_biome_gateway.sql','0001_controller_ha.sql','0002_bridge_result_chunks.sql'])sql.exec(readFileSync('drizzle/'+f,'utf8'));
 sql.exec('PRAGMA foreign_keys=ON');
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'site',1)");
 const DB={prepare(q){const s=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){return s.get(...args)},async run(){return s.run(...args)},async all(){return {results:s.all(...args)}}}}};
 const env={DB,BIOME_PROVIDER_ACTORS:actor};
 const send=(d,headers={})=>gateway.fetch(new Request('https://biome.test/api/provider/call',{method:'POST',body:JSON.stringify(d),headers}),env);
 const envelope={id:'one',realm:'site',operation:'provider.poll',arguments:{provider:actor,note:'café 😀'},auth:{actor}};
 const p={site:'site',...envelope};delete p.auth;
 envelope.auth.signature=sign('sha256',Buffer.from(canonical(p)),pair.privateKey).toString('base64');
 const ack=d=>gateway.fetch(new Request('https://biome.test/api/provider/ack',{method:'POST',body:JSON.stringify(d)}),env);
 return {sql,DB,env,send,ack,d:{site:'site',public_key,envelope}};
}
test('native proof is required, site and origin bound, no unsigned/admin invocation',async()=>{
 const {sql,send,d,env}=fixture();
 for(const mutate of [x=>delete x.envelope.auth,x=>x.envelope.auth.signature='AAAA',x=>x.envelope.arguments.note='changed',x=>x.envelope.realm='local',x=>x.site='another',x=>x.envelope.cached=true,x=>x.envelope.auth.actor='f'.repeat(64)]){
  const x=structuredClone(d);mutate(x);assert.ok((await send(x)).status>=400);
 }
 assert.equal((await send(d,{Origin:'https://evil.test'})).status,403);
 env.BIOME_PROVIDER_ACTORS='';assert.equal((await send(d)).status,403);
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);sql.close();
});
test('same proof durably polls one job, new read ID creates new job, controller result retained',async()=>{
 const {sql,send,d}=fixture();
 assert.equal((await send(d)).status,202);assert.equal((await send(d)).status,202);
 let row=sql.prepare('SELECT * FROM bridge_jobs').get();assert.equal(row.deadline,0);
 assert.deepEqual(JSON.parse(JSON.parse(row.request).arguments.envelope),d.envelope);
 const answer={content:[{type:'text',text:JSON.stringify({status:200,body:{result:{actions:[]}}})}]};
 sql.prepare("UPDATE bridge_jobs SET state='done',result=?,updated=0").run(JSON.stringify(answer));
 assert.deepEqual(await (await send(d)).json(),{result:{actions:[]},relay_receipt:row.id});
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);
 d.envelope.id='two';const p={site:d.site,...d.envelope};delete p.auth;
 d.envelope.auth.signature=sign('sha256',Buffer.from(canonical(p)),pair.privateKey).toString('base64');
 assert.equal((await send(d)).status,202);assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,2);sql.close();
});
test('expired HTTP waits do not expire native job; HA can claim and reclaim it',async()=>{
 const {sql,DB,send,d}=fixture();await send(d);
 const now=Date.now()/1000;
 sql.prepare("INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES('pi','token',1,4,?,1)").run(now);
 let node=sql.prepare('SELECT * FROM controller_nodes').get();
 const first=await claimControllerJob(DB,node);assert.ok(first.id);assert.equal(JSON.parse(first.request).name,'biome_native_provider');
 sql.exec('UPDATE bridge_jobs SET lease_until=0');
 const again=await claimControllerJob(DB,node);assert.equal(again.id,first.id);assert.notEqual(again.lease,first.lease);sql.close();
});
test('controller denials remain denials, transient transport errors requeue identical envelope',async()=>{
 const {sql,send,d}=fixture();await send(d);
 sql.prepare("UPDATE bridge_jobs SET state='done',result=?").run(JSON.stringify({isError:true,content:[{type:'text',text:'timeout'}]}));
 assert.equal((await send(d)).status,202);assert.equal(sql.prepare('SELECT state FROM bridge_jobs').get().state,'queued');
 sql.prepare("UPDATE bridge_jobs SET state='done',result=?").run(JSON.stringify({content:[{type:'text',text:JSON.stringify({status:403,body:{error:'Revoked credential'}})}]}));
 assert.equal((await send(d)).status,403);sql.close();
});
test('raw native numeric spelling and explicit signed bytes survive forwarding',async()=>{
 const {sql,send,d}=fixture();
 const envelope={...d.envelope,arguments:{number:1.5}};
 const p={site:d.site,...envelope};delete p.auth;
 d.signed_payload=canonical(p).replace('1.5','1.50');
 envelope.auth.signature=sign('sha256',Buffer.from(d.signed_payload),pair.privateKey).toString('base64');
 d.envelope=JSON.stringify(envelope).replace('1.5','1.50');
 assert.equal((await send(d)).status,202);
 assert.equal(JSON.parse(sql.prepare('SELECT request FROM bridge_jobs').get().request).arguments.envelope,d.envelope);
 d.signed_payload=d.signed_payload.replace('1.50','2.0');assert.equal((await send(d)).status,400);sql.close();
});
test('ordinary bridge cleanup preserves old pending and completed native jobs',async()=>{
 const {sql,DB,send,d}=fixture();await send(d);
 sql.exec('UPDATE bridge_jobs SET updated=0');
 sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,result,updated) VALUES('ordinary','{}','done',0,?,?)").run(JSON.stringify({content:[]}),Math.floor(Date.now()/1000));
 const {enqueue}=await import('../lib/bridge.mjs');
 await enqueue({DB},'biome_status',{},'ordinary');
 assert.equal(sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE id LIKE 'native-%'").get().n,1);sql.close();
});
test('native pending queue has bounded admission; existing proofs remain pollable when full',async()=>{
 const {sql,send,d}=fixture();await send(d);
 for(let i=0;i<39;i++)sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,'{}','queued',0,0)").run('native-filler-'+i);
 assert.equal((await send(d)).status,202);
 d.envelope.id='different';const p={site:d.site,...d.envelope};delete p.auth;
 d.envelope.auth.signature=sign('sha256',Buffer.from(canonical(p)),pair.privateKey).toString('base64');
 assert.equal((await send(d)).status,503);sql.close();
});
function acknowledgement(d,receipt,keypair=pair){
 const value={site:d.site,public_key:d.public_key,actor:d.envelope.auth.actor,receipt};
 const payload={domain:'biomerte-provider-relay-ack-v1',operation:'acknowledge',site:value.site,actor:value.actor,receipt};
 value.signature=sign('sha256',Buffer.from(canonical(payload)),keypair.privateKey).toString('base64');return value;
}
function complete(sql,status=200){
 const value={status,body:status===200?{result:{actions:[]}}:{error:'Permission denied'}};
 sql.prepare("UPDATE bridge_jobs SET state='done',result=?").run(JSON.stringify({content:[{type:'text',text:JSON.stringify(value)}]}));
}
test('signed acknowledgement deletes only owned completed result and chunks; repeats add no tombstones',async()=>{
 const {sql,send,ack,d}=fixture();await send(d);complete(sql);
 const response=await (await send(d)).json(),receipt=response.relay_receipt;
 assert.match(receipt,/^native-[a-f0-9]{64}$/);
 sql.prepare('INSERT INTO bridge_result_chunks(job,lease,digest,part,data) VALUES(?,?,?,?,?)').run(receipt,'lease','digest',0,'chunk');
 const request=acknowledgement(d,receipt);
 for(let i=0;i<3;i++)assert.deepEqual(await (await ack(request)).json(),{acknowledged:true,receipt});
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_result_chunks').get().n,0);
 // Explicit release ends gateway replay retention; old call can be submitted
 // again. Controller mutation receipts, not deleted gateway state, dedup it.
 assert.equal((await send(d)).status,202);sql.close();
});
test('pending, claimed and transient failed jobs cannot be acknowledged',async()=>{
 const {sql,send,ack,d}=fixture();await send(d);
 const receipt=sql.prepare('SELECT id FROM bridge_jobs').get().id,request=acknowledgement(d,receipt);
 assert.equal((await ack(request)).status,409);
 sql.exec("UPDATE bridge_jobs SET state='claimed'");assert.equal((await ack(request)).status,409);
 sql.prepare("UPDATE bridge_jobs SET state='done',result=?").run(JSON.stringify({isError:true,content:[]}));
 assert.equal((await ack(request)).status,409);assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);sql.close();
});
test('acknowledgement binds its domain, operation, site, actor and exact receipt',async()=>{
 const {sql,env,send,ack,d}=fixture();await send(d);complete(sql);
 const receipt=sql.prepare('SELECT id FROM bridge_jobs').get().id,request=acknowledgement(d,receipt);
 for(const mutate of [x=>x.signature=d.envelope.auth.signature,x=>x.site='other',x=>x.receipt='native-'+'0'.repeat(64),x=>x.actor='f'.repeat(64)]){
  const x=structuredClone(request);mutate(x);assert.equal((await ack(x)).status,403);
 }
 for(const change of [{domain:'different-domain'},{operation:'invoke'}]){
  const payload={domain:'biomerte-provider-relay-ack-v1',operation:'acknowledge',site:d.site,actor:d.envelope.auth.actor,receipt,...change};
  const x={...request,signature:sign('sha256',Buffer.from(canonical(payload)),pair.privateKey).toString('base64')};
  assert.equal((await ack(x)).status,403);
 }
 const other=generateKeyPairSync('rsa',{modulusLength:2048}),public_key=other.publicKey.export({format:'pem',type:'spki'});
 const actor=createHash('sha256').update(other.publicKey.export({format:'der',type:'spki'})).digest('hex');
 env.BIOME_PROVIDER_ACTORS+=','+actor;
 const wrongOwner=acknowledgement({site:d.site,public_key,envelope:{auth:{actor}}},receipt,other);
 assert.equal((await ack(wrongOwner)).status,403);
 assert.equal(sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);sql.close();
});
test('terminal controller errors include an acknowledgeable receipt',async()=>{
 const {sql,send,ack,d}=fixture();await send(d);complete(sql,403);
 const response=await send(d);assert.equal(response.status,403);
 const body=await response.json();assert.equal(body.error,'Permission denied');assert.ok(body.relay_receipt);
 assert.equal((await ack(acknowledgement(d,body.relay_receipt))).status,200);sql.close();
});

test('acknowledgement compare-and-delete cannot delete a concurrently requeued job',async()=>{
 const {sql,DB,send,ack,d}=fixture();await send(d);complete(sql);
 const receipt=sql.prepare('SELECT id FROM bridge_jobs').get().id;
 const prepare=DB.prepare.bind(DB);let raced=false;
 DB.prepare=q=>{
  if(q.startsWith('DELETE FROM bridge_jobs')&&!raced){
   raced=true;sql.exec("UPDATE bridge_jobs SET state='queued',result=NULL");
  }
  return prepare(q);
 };
 assert.equal((await ack(acknowledgement(d,receipt))).status,409);
 assert.equal(sql.prepare('SELECT state FROM bridge_jobs').get().state,'queued');sql.close();
});
