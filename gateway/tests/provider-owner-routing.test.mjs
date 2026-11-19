import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {createHash,generateKeyPairSync} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {sqliteD1} from './sqlite-d1.mjs';
import gateway from '../worker/index.js';
import {providerFingerprint} from '../lib/provider.mjs';
import {ownershipBootstrapDigest} from '../lib/ownership-transfer.mjs';
const base='https://biome.test',mac='a'.repeat(64),pi='b'.repeat(64),actor='c'.repeat(64);
const tokens={mac:'1'.repeat(64),pi:'2'.repeat(64)};
const signature=Buffer.alloc(256,17).toString('base64');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const key=generateKeyPairSync('rsa',{modulusLength:2048}).publicKey;
const pem=key.export({type:'spki',format:'pem'}),fingerprint=createHash('sha256').update(key.export({type:'spki',format:'der'})).digest('hex');
const packet=(controller)=>({site:'test',realm:'site',id:'original-id',operation:'provider.poll',arguments:{provider:actor,limit:1},auth:{actor,signature},...(controller===undefined?{}:{controller})});

test('temporary adoption gate binds exact request and cannot be supplied by a caller flag',async()=>{
 const f=fixture();try{
  const data={unit:'portable',expected_epoch:0,owner:pi,authority_digest:'d'.repeat(64),aliases:[actor],snapshot_digest:'e'.repeat(64),local_authority:true,durable:true};
  assert.equal((await f.device('ha/ownership/bootstrap',data)).status,403);
  assert.equal((await f.device('ha/ownership/bootstrap',{...data,bootstrapAuthorized:true})).status,400);
  f.env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS=JSON.stringify({portable:await ownershipBootstrapDigest(data)});
  assert.equal((await f.device('ha/ownership/bootstrap',{...data,snapshot_digest:'f'.repeat(64)})).status,403);
  assert.equal((await f.device('ha/ownership/bootstrap',data,'mac')).status,400);
  assert.equal((await f.device('ha/ownership/bootstrap',data)).status,200);
  delete f.env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS;
  assert.equal((await f.device('ha/ownership/bootstrap',data)).status,403);
  assert.equal((await f.device('ha/ownership/status',{unit:'portable'})).status,200);
 }finally{f.sql.close()}
});

test('generic current ownership routes unchanged legacy callers and changes only after staged CAS',async()=>{
 const f=fixture();try{
  delete f.env.PROVIDER_CONTROLLER_MAP;
  const adopt={unit:'portable',expected_epoch:0,owner:pi,authority_digest:'d'.repeat(64),aliases:[actor],snapshot_digest:'e'.repeat(64),local_authority:true,durable:true};
  f.env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS=JSON.stringify({portable:await ownershipBootstrapDigest(adopt)});
  assert.equal((await f.device('ha/ownership/bootstrap',adopt)).status,200);
  delete f.env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS;
  const raw=JSON.stringify(packet()).replace('"limit":1','"limit":1.0');
  let waiting=f.send('/api/provider/call',raw),job=await next(f);
  assert.equal(job.request.arguments.envelope,raw);await complete(f,job);assert.equal((await waiting).status,200);
  const proof={unit:'portable',epoch:1,owner:pi,target:mac,transfer:'finite',authority_digest:adopt.authority_digest,scope:[['offers','current']],aliases:[actor],snapshot_digest:'f'.repeat(64),authorization_digest:'0'.repeat(64),expires:Date.now()/1000+60};
  assert.equal((await f.device('ha/ownership/prepare',{...proof,source_frozen:true})).status,200);
  assert.equal((await f.send('/api/provider/call',raw)).status,409);
  assert.equal((await f.device('ha/ownership/commit',proof,'mac')).status,409);
  assert.equal((await f.device('ha/ownership/ready',{...proof,verified:true,staged:true},'mac')).status,200);
  assert.equal((await f.device('ha/ownership/commit',proof,'mac')).status,200);
  waiting=f.send('/api/provider/call',raw);job=await next(f,'mac');
  assert.equal(job.request.arguments.controller,mac);assert.equal(job.request.arguments.envelope,raw);
  await complete(f,job,200,{result:{ok:true}},'mac');assert.equal((await waiting).status,200);
  assert.equal((await f.device('ha/ownership/ack',{...proof,durable:true})).status,200);
  assert.equal((await f.device('ha/ownership/ack',{...proof,durable:true},'mac')).status,200);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM ownership_transfers').get().n,0);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM ownership_transfer_scope').get().n,0);
  assert.deepEqual(Object.keys(f.sql.prepare('SELECT * FROM ownership_current').get()).sort(),['authority_digest','epoch','owner','site','unit']);
 }finally{f.sql.close()}
});

test('stale static provider configuration cannot override current ownership',async()=>{
 const f=fixture();try{
  const adopt={unit:'portable',expected_epoch:0,owner:mac,authority_digest:'d'.repeat(64),aliases:[actor],snapshot_digest:'e'.repeat(64),local_authority:true,durable:true};
  f.env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS=JSON.stringify({portable:await ownershipBootstrapDigest(adopt)});
  assert.equal((await f.device('ha/ownership/bootstrap',adopt,'mac')).status,200);
  assert.equal((await f.send('/api/provider/call',packet())).status,409);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const f of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+f,'utf8'));
 sql.exec("INSERT INTO controller_config(id,site,enabled) VALUES(1,'test',1)");
 for(const [node,token] of Object.entries(tokens))sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready) VALUES(?,?,?,2,?,1)').run(node,createHash('sha256').update(token).digest('hex'),node==='mac'?0:1,Date.now()/1000);
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',CONTROLLER_NODE_MAP:JSON.stringify({[mac]:'mac',[pi]:'pi'}),PROVIDER_CONTROLLER_MAP:JSON.stringify({[actor]:pi,[fingerprint]:pi})};
 const send=(path,body={},headers={})=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'Content-Type':'application/json',...headers},body:typeof body==='string'?body:JSON.stringify(body)}),env);
 const device=(path,body={},node='pi')=>send('/bridge/'+path,body,{Authorization:'Bearer '+tokens[node]});
 return {sql,env,send,device};
}
async function next(f,node='pi'){
 for(let n=0;n<200;n++){const job=await(await f.device('next',{},node)).json();if(job.id)return job;await pause(5)}
 assert.fail('Owner-routed request not found');
}
async function complete(f,job,status=200,body={result:{ok:true}},node='pi'){
 const value={id:job.id,lease:job.lease,result:{content:[{type:'text',text:JSON.stringify({status,body})}]}};
 assert.equal((await f.device('complete',value,node==='pi'?'mac':'pi')).status,409);
 assert.equal((await f.device('complete',value,node)).status,200);
}
test('pairing fingerprint equals OpenSSL canonical RSA SubjectPublicKeyInfo identity',async()=>{
 const der=execFileSync('openssl',['rsa','-pubin','-outform','DER','-pubout'],{input:pem});
 assert.equal(await providerFingerprint(pem),createHash('sha256').update(der).digest('hex'));
 assert.equal(await providerFingerprint(pem.replace(/\n/g,'\r\n')),fingerprint);
 await assert.rejects(providerFingerprint('-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----'));
});
test('legacy signed calls route by exact verified actor map without rewriting JSON or using default',async()=>{
 const f=fixture();try{
  const raw=JSON.stringify(packet()).replace('"limit":1','"limit":1.0,"big":9007199254740993');
  let prior;
  for(const status of [200,403]){
   const waiting=f.send('/api/provider/call',raw),job=await next(f);
   assert.equal(job.request.arguments.controller,pi);assert.equal(job.request.arguments.controller_owner,'pi');
   assert.equal(job.request.arguments.envelope,raw);assert.notEqual(job.id,prior);prior=job.id;
   await complete(f,job,status,status===403?{error:'Revoked'}:{result:{ok:true}});
   assert.equal((await waiting).status,status);
  }
  assert.equal(f.sql.prepare("SELECT count(*) n FROM bridge_jobs WHERE json_extract(request,'$.arguments.controller_owner')='mac'").get().n,0);
 }finally{f.sql.close()}
});
test('modern signed destination is preserved and cannot override configured actor owner',async()=>{
 const f=fixture();try{
  assert.equal((await f.send('/api/provider/call',packet(mac))).status,409);
  assert.equal((await f.send('/api/provider/call?controller='+pi,packet())).status,400);
  const raw=JSON.stringify(packet(pi)),waiting=f.send('/api/provider/call',raw),job=await next(f);
  assert.equal(job.request.arguments.envelope,raw);await complete(f,job);assert.equal((await waiting).status,200);
 }finally{f.sql.close()}
});
test('unknown ownership, malformed maps, unknown or disabled controllers never fall back',async()=>{
 for(const map of [{}, {[actor]:'d'.repeat(64)}, {[actor]:'pi'}, null, []]){
  const f=fixture();try{
   f.env.PROVIDER_CONTROLLER_MAP=JSON.stringify(map);
   assert.ok((await f.send('/api/provider/call',packet())).status>=400);
   assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
  }finally{f.sql.close()}
 }
 const f=fixture();try{f.sql.exec("UPDATE controller_nodes SET enabled=0 WHERE id='pi'");assert.equal((await f.send('/api/provider/call',packet())).status,409);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0)}finally{f.sql.close()}
});
test('unavailable selected provider owner remains queued and cannot fail over to default',async()=>{
 const f=fixture();try{
  f.sql.exec("UPDATE controller_nodes SET ready=0 WHERE id='pi'");
  const waiting=f.send('/api/provider/call',packet());
  for(let n=0;n<100&&!f.sql.prepare('SELECT id FROM bridge_jobs').get();n++)await pause(5);
  assert.equal((await(await f.device('next',{},'mac')).json()).idle,true);
  assert.equal((await(await f.device('next',{},'pi')).json()).idle,true);
  f.sql.exec("UPDATE controller_nodes SET ready=1 WHERE id='pi'");
  const job=await next(f);await complete(f,job);assert.equal((await waiting).status,200);
 }finally{f.sql.close()}
});
test('legacy begin and finish resolve the same canonical fingerprint owner without firmware change',async()=>{
 const f=fixture();try{
  const steps=[['begin',{public_key:pem,label:'Existing relay'}],['finish',{key:fingerprint,challenge:'e'.repeat(64),site:'test',signature}]];
  for(const [phase,body] of steps){
   const raw=JSON.stringify(body),waiting=f.send('/api/provider/pair/'+phase,raw),job=await next(f);
   assert.equal(job.request.name,'signed_provider_pair');assert.equal(job.request.arguments.controller,pi);assert.equal(job.request.arguments.controller_owner,'pi');assert.equal(job.request.arguments.envelope,raw);
   await complete(f,job,phase==='finish'?403:200,phase==='finish'?{error:'Not approved'}:{result:{key:fingerprint,site:'test',challenge:'e'.repeat(64)}});
   assert.equal((await waiting).status,phase==='finish'?403:200);
  }
 }finally{f.sql.close()}
});
test('new pairing uses an explicit recipient and never exposes approval or changes proof JSON',async()=>{
 const f=fixture();try{
  f.env.PROVIDER_CONTROLLER_MAP='{}';
  const begin={public_key:pem,label:'New provider'},finish={key:fingerprint,challenge:'e'.repeat(64),site:'test',signature};
  assert.equal((await f.send('/api/provider/pair/begin',begin)).status,409);
  for(const [phase,body] of [['begin',begin],['finish',finish]]){
   const raw=JSON.stringify(body),waiting=f.send('/api/provider/pair/'+phase+'?controller='+pi,raw),job=await next(f);
   assert.equal(job.request.arguments.envelope,raw);assert.equal(job.request.arguments.controller,pi);
   await complete(f,job);assert.equal((await waiting).status,200);
  }
  assert.equal((await f.send('/api/provider/pair/approve?controller='+pi,{key:fingerprint})).status,404);
 }finally{f.sql.close()}
});
test('pairing owner conflicts and duplicate selectors reject before queueing',async()=>{
 const f=fixture();try{
  const begin={public_key:pem,label:'Relay'},finish={key:fingerprint,challenge:'e'.repeat(64),site:'test',signature};
  assert.equal((await f.send('/api/provider/pair/begin?controller='+mac,begin)).status,409);
  assert.equal((await f.send('/api/provider/pair/finish?controller='+mac,finish)).status,409);
  assert.equal((await f.send('/api/provider/pair/begin?controller='+pi+'&controller='+pi,begin)).status,400);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
 }finally{f.sql.close()}
});

test('signed legacy provider transport follows current gateway placement instead of obsolete affinity map',async()=>{
 const f=fixture();try{
  f.sql.exec('CREATE TABLE provider_placements(site TEXT,provider TEXT,controller TEXT)');f.sql.prepare('INSERT INTO provider_placements VALUES(?,?,?)').run('test',actor,mac);
  const raw=JSON.stringify(packet());const waiting=f.send('/api/provider/call',raw);const job=await next(f,'mac');assert.equal(job.request.arguments.controller,mac);assert.equal(job.request.arguments.envelope,raw);await complete(f,job,200,{result:{ok:true}},'mac');assert.equal((await waiting).status,200);
 }finally{f.sql.close()}
});

test('signed provider completions follow original dispatch rather than current actor placement',async()=>{
 const f=fixture();try{
  const {providerTopologySchema}=await import('../lib/provider-topology.mjs');
  await providerTopologySchema(f.env.DB);
  f.sql.prepare('INSERT INTO provider_dispatch_routes VALUES(?,?,?,?,?)').run('test','operation','original-effect',mac,Date.now()/1000+120);
  const data={...packet(),operation:'provider.result',arguments:{provider:actor,operation:'original-effect',generation:1,state:'failed',result:{exit_code:1}}};
  const raw=JSON.stringify(data),waiting=f.send('/api/provider/call',raw),job=await next(f,'mac');
  assert.equal(job.request.arguments.envelope,raw);
  await complete(f,job,200,{result:{id:'original-effect',state:'failed'}},'mac');
  assert.equal((await waiting).status,200);
  assert.equal((await f.send('/api/provider/call',{...data,controller:pi})).status,409);
  // Signature and revocation are still checked by a fresh native request.
  const retry=f.send('/api/provider/call',raw),second=await next(f,'mac');
  assert.notEqual(second.id,job.id);
  await complete(f,second,403,{error:'Revoked'},'mac');assert.equal((await retry).status,403);
 }finally{f.sql.close()}
});

test('signed provider poll binds returned actions before forwarding the reply',async()=>{
 const f=fixture();try{
  const waiting=f.send('/api/provider/call',packet()),job=await next(f);
  await complete(f,job,200,{result:{actions:[{id:'new-effect',lease:'new-lease'}],fences:[{id:'new-fence'}]}});
  assert.equal((await waiting).status,200);
  assert.equal(f.sql.prepare("SELECT controller FROM provider_dispatch_routes WHERE kind='operation' AND id='new-effect'").get().controller,pi);
  assert.equal(f.sql.prepare('SELECT count(*) n FROM provider_dispatch_routes').get().n,3);
 }finally{f.sql.close()}
});
