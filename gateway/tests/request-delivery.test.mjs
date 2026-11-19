import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import gateway from '../worker/index.js';
import {providerFrame} from '../lib/transport.mjs';

function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const DB=sqliteD1(sql),base='https://biome.test',token='r'.repeat(43);
 sql.prepare('INSERT INTO biome_owner VALUES(1,?)').run('owner');
 sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)")
  .run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+60);
 const device='f'.repeat(64);const env={DB,BIOME_OAUTH_ORIGIN:base,BIOME_OWNER_EMAIL:'owner@test.invalid',BIOME_BRIDGE_PIN:createHash('sha256').update(createHash('sha256').update(device).digest('hex')+'\n').digest('hex')};
 const send=(body,auth=true)=>gateway.fetch(new Request(base+'/api/mcp',{method:'POST',headers:{'content-type':'application/json',...(auth?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(body)}),env);
 const link=(path,body)=>gateway.fetch(new Request(base+path,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+device},body:JSON.stringify(body)}),env);
 return {sql,env,send,base,link};
}
const rpc=args=>({jsonrpc:'2.0',id:'delivery-test',method:'tools/call',params:{name:'biome_invoke',arguments:args}});
async function completeQueued(f,waiting,check){
 let job;
 for(let i=0;i<100&&!job?.id;i++){await new Promise(r=>setTimeout(r,5));job=await(await f.link('/bridge/next',{})).json();}
 assert.ok(job.id,'Payload must reach the authenticated controller delivery path');check(job.request);
 const answer={content:[{type:'text',text:JSON.stringify({accepted:true})}]};
 assert.equal((await f.link('/bridge/complete',{id:job.id,lease:'wrong',result:answer})).status,409);
 assert.equal((await f.link('/bridge/complete',{id:job.id,lease:job.lease,result:answer})).status,200);
 const response=await waiting;assert.equal(response.status,200);assert.deepEqual((await response.json()).result,answer);
 return job.id;
}

test('authenticated 413 KB provider results cross HTTP and durable queue unchanged',async()=>{
 const f=fixture();try{
  const args={operation:'provider.result',arguments:{provider:'provider',operation:'original-vision-operation',generation:1,state:'succeeded',result:{result:{metadata:'x'.repeat(413000)}}},request_id:'original-provider-result'};
  args.arguments.result.result.metadata='';const body=rpc(args);
  args.arguments.result.result.metadata='x'.repeat(413161-Buffer.byteLength(JSON.stringify(body)));
  assert.equal(Buffer.byteLength(JSON.stringify(body)),413161);
  assert.equal((await f.send(body,false)).status,401);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,0);
  const id=await completeQueued(f,f.send(body),job=>assert.deepEqual(job.arguments,args));
  assert.equal((await f.send(body)).status,200);assert.equal(f.sql.prepare('SELECT count(*) n FROM bridge_jobs').get().n,1);
  const changed=structuredClone(body);changed.params.arguments.arguments.result.result.metadata+='changed';
  assert.equal((await (await f.send(changed)).json()).result.isError,true);
  assert.equal(f.sql.prepare('SELECT id FROM bridge_jobs').get().id,id);
 }finally{f.sql.close()}
});

test('a declared maximum native artifact chunk fits HTTP, socket and queue budgets',async()=>{
 const f=fixture();try{
  const data=Buffer.alloc(262144,17).toString('base64');
  const args={operation:'artifact.put',arguments:{data,transient:true},request_id:'one-artifact-chunk'};
  const socket={type:'invoke',id:'one-rpc',arguments:args};assert.deepEqual(providerFrame(JSON.stringify(socket)),socket);
  await completeQueued(f,f.send(rpc(args)),job=>assert.equal(job.arguments.arguments.data,data));
 }finally{f.sql.close()}
});

test('Unicode requests beyond the abandoned replacement cap cross HTTP, socket and queue intact',async()=>{
 const f=fixture();try{
  const args={operation:'provider.result',arguments:{result:'😀'.repeat(270000)},request_id:'large-unicode'};
  const body=rpc(args),encoded=JSON.stringify(body);assert.ok(Buffer.byteLength(encoded)>1048576);
  const socket={type:'invoke',id:'large-unicode',arguments:args};
  assert.deepEqual(providerFrame(JSON.stringify(socket)),socket);
  assert.throws(()=>providerFrame(new Uint8Array()),/Invalid frame/);
  await completeQueued(f,f.send(body),job=>assert.deepEqual(job.arguments,args));
 }finally{f.sql.close()}
});
