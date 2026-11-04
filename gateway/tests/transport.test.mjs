import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import gateway from '../worker/index.js';

function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const p of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+p,'utf8'));
 const DB={prepare(q){const stmt=sql.prepare(q);let args=[];return{bind(...a){args=a;return this},async first(){return stmt.get(...args)},async run(){return stmt.run(...args)},async all(){return{results:stmt.all(...args)}}}}};
 const base='https://biome.test',token='s'.repeat(43);
 sql.prepare('INSERT INTO biome_owner VALUES(1,?)').run('owner');
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)")
  .run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+60);
 const env={DB,BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid'};
 const send=(path,body,auth=true)=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(body)}),env);
 return{sql,send};
}
test('completion stream requires OAuth and rejects invalid work before submission',async()=>{
 const {sql,send}=fixture();
 assert.equal((await send('/api/exchange',{},false)).status,401);
 assert.equal((await send('/api/exchange',{contract:'host.terminal.run:1'})).status,400);
 assert.equal((await send('/api/provider/connect',{},false)).status,401);
 assert.equal(sql.prepare('SELECT COUNT(*) n FROM bridge_jobs').get().n,0);sql.close();
});
test('one streaming request submits once and delivers completion; same identity resumes',async()=>{
 const {sql,send}=fixture();let submissions=0,statuses=0;
 const body={contract:'chat.private.exchange:2',resource:'privatechat.mac',envelope:{id:'same-operation',kind:'hello'}};
 const seen=new Set();
 const timer=setInterval(()=>{
  for(const job of sql.prepare("SELECT id,request FROM bridge_jobs WHERE state='queued'").all()){
   const rpc=JSON.parse(job.request).arguments;let value;
   if(rpc.operation==='cap.submit'){
    submissions++;assert.equal(rpc.arguments.operation,body.envelope.id);assert.deepEqual(rpc.arguments.inputs,{envelope:body.envelope});
    value={id:body.envelope.id,state:'queued'};
   }else{
    assert.equal(rpc.operation,'cap.submission');statuses++;
    value={id:body.envelope.id,state:'succeeded',result:{envelope:{kind:'hello',id:body.envelope.id}}};
   }
   seen.add(job.id);
   sql.prepare("UPDATE bridge_jobs SET state='done',result=? WHERE id=?").run(JSON.stringify({content:[{type:'text',text:JSON.stringify(value)}]}),job.id);
  }
 },5);
 try{
  for(let retry=0;retry<2;retry++){
   const response=await send('/api/exchange',body);
   assert.match(response.headers.get('content-type'),/ndjson/);
   const frames=(await response.text()).trim().split('\n').map(JSON.parse);
   assert.equal(frames[0].type,'waiting');assert.equal(frames.at(-1).status.state,'succeeded');
   assert.equal(frames.at(-1).status.id,'same-operation');
  }
  assert.equal(submissions,1,'durable submission receipt prevents replay');assert.equal(statuses,2,'status reads stay live');
 }finally{clearInterval(timer);sql.close()}
});
