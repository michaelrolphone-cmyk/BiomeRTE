import {sqliteD1} from './sqlite-d1.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {maintenanceDevice,maintenanceCall,maintenanceResultRetention} from '../lib/maintenance.mjs';
const sha=value=>createHash('sha256').update(value).digest('hex');
function canonical(value){return JSON.stringify((function sorted(v){return Array.isArray(v)?v.map(sorted):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,sorted(v[k])])):v})(value)).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'))}
async function fixture(settings={}){
 const sql=new DatabaseSync(':memory:'),token='b'.repeat(64),host='mac-maintenance';
 const DB=sqliteD1(sql);
 const env={DB,...settings};
 const send=(body,key=token)=>maintenanceDevice(new Request('https://test.invalid/maintenance/device',{method:'POST',headers:{Authorization:'Bearer '+key},body:JSON.stringify(body)}),env);
 await send({operation:'poll'}); // Creates schema; host is not enrolled yet.
 sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,enabled,phone_key,phone_device,site) VALUES(?,?,1,?,?,?)').run(host,sha(token),'legacy-public-key','legacy-device','site');
 function job(id,state='claimed',owner=host){sql.prepare('INSERT INTO maintenance_jobs(id,host,request,state,created,updated) VALUES(?,?,?,?,0,0)').run(id,owner,'{}',state)}
 const admit=async id=>{await maintenanceCall(env,'maintenance.submit',{host,argv:['/bin/echo',id]},id);await send({operation:'poll'})};
 return {sql,send,job,host,env,admit};
}

test('maintenance ACK binds durable command id, host and canonical result digest',async()=>{
 const f=await fixture();try{
  f.job('one');const result={state:'succeeded',exit_code:0,stdout:'café ☀ 😀',stderr:'',output_directory:'/private/path'};
  const response=await f.send({operation:'complete',id:'one',result});assert.equal(response.status,200);
  assert.deepEqual(await response.json(),{accepted:true,id:'one',host:f.host,result_sha256:sha(canonical(result))});
  const stored=f.sql.prepare('SELECT state,result FROM maintenance_jobs WHERE id=?').get('one');
  assert.equal(stored.state,'succeeded');assert.deepEqual(JSON.parse(stored.result),result);
 }finally{f.sql.close()}
});

test('lost ACK, concurrent identical replay and reordered keys return the same receipt',async()=>{
 const f=await fixture();try{
  f.job('one');const result={state:'failed',exit_code:3,stdout:'kept',stderr:'err'};
  const lost=await (await f.send({operation:'complete',id:'one',result})).json();
  const replay={stderr:'err',stdout:'kept',exit_code:3,state:'failed'};
  const receipts=await Promise.all([result,replay,result].map(async r=>{const v=await f.send({operation:'complete',id:'one',result:r});assert.equal(v.status,200);return v.json()}));
  for(const receipt of receipts)assert.deepEqual(receipt,lost);
 }finally{f.sql.close()}
});

test('unknown, foreign-host, unclaimed and conflicting results are never acknowledged',async()=>{
 const f=await fixture();try{
  f.job('foreign','claimed','another-host');f.job('queued','queued');f.job('pending','awaiting_confirmation');f.job('one');
  const result={state:'succeeded',exit_code:0,stdout:'first'};
  for(const id of ['missing','foreign','queued','pending']){const response=await f.send({operation:'complete',id,result});assert.ok([404,409].includes(response.status));assert.notEqual((await response.json()).accepted,true)}
  await f.send({operation:'complete',id:'one',result});
  const conflict=await f.send({operation:'complete',id:'one',result:{...result,stdout:'conflict'}});assert.equal(conflict.status,409);assert.notEqual((await conflict.json()).accepted,true);
  assert.deepEqual(JSON.parse(f.sql.prepare('SELECT result FROM maintenance_jobs WHERE id=?').get('one').result),result);
  assert.equal(f.sql.prepare('SELECT state FROM maintenance_jobs WHERE id=?').get('queued').state,'queued');
 }finally{f.sql.close()}
});

test('uncertain result can be durably acknowledged without claiming successful execution',async()=>{
 const f=await fixture();try{
  f.job('one');const result={state:'uncertain',reason:'Process outcome requires reconciliation'};
  const response=await f.send({operation:'complete',id:'one',result});assert.equal(response.status,200);assert.equal((await response.json()).result_sha256,sha(canonical(result)));
  assert.equal(f.sql.prepare('SELECT state FROM maintenance_jobs WHERE id=?').get('one').state,'uncertain');
 }finally{f.sql.close()}
});

test('device authentication and result validation remain enforced',async()=>{
 const f=await fixture();try{
  f.job('one');assert.equal((await f.send({operation:'complete',id:'one',result:{state:'succeeded'}},'c'.repeat(64))).status,401);
  for(const body of [{operation:'complete',result:{state:'succeeded'}},{operation:'complete',id:'one',result:{state:'running'}}])assert.equal((await f.send(body)).status,400);
  assert.equal(f.sql.prepare('SELECT state FROM maintenance_jobs WHERE id=?').get('one').state,'claimed');
 }finally{f.sql.close()}
});


test('only new admission receives the retention marker; old history stays unmarked',async()=>{
 const f=await fixture({MAINTENANCE_RESULT_MAX_COUNT:0});try{
  await f.admit('new');assert.equal(f.sql.prepare('SELECT retention_version FROM maintenance_jobs WHERE id=?').get('new').retention_version,1);
  await maintenanceCall(f.env,'maintenance.submit',{host:f.host,argv:['/bin/echo','legacy']},'legacy');
  f.sql.prepare('UPDATE maintenance_jobs SET retention_version=NULL WHERE id=?').run('legacy');
  await maintenanceCall(f.env,'maintenance.submit',{host:f.host,argv:['/bin/echo','legacy']},'legacy');
  assert.equal(f.sql.prepare('SELECT retention_version FROM maintenance_jobs WHERE id=?').get('legacy').retention_version,null);
  await f.send({operation:'poll'});const result={state:'succeeded',stdout:'old history'};
  await f.send({operation:'complete',id:'legacy',result});
  assert.deepEqual(JSON.parse(f.sql.prepare('SELECT result FROM maintenance_jobs WHERE id=?').get('legacy').result),result);
 }finally{f.sql.close()}
});

test('compacted completion recovers identical delayed ACK without rehydration and rejects altered replay',async()=>{
 const f=await fixture({MAINTENANCE_RESULT_MAX_COUNT:0});try{
  await f.admit('one');const result={state:'succeeded',exit_code:0,stdout:'future diagnostic'};
  const lost=await (await f.send({operation:'complete',id:'one',result})).json();
  let row=f.sql.prepare('SELECT state,result,result_retired,result_digest FROM maintenance_jobs WHERE id=?').get('one');
  assert.equal(row.result_retired,1);assert.equal(JSON.parse(row.result).result_retired,true);assert.equal(row.result_digest,lost.result_sha256);
  const replay=await f.send({operation:'complete',id:'one',result});assert.equal(replay.status,200);assert.deepEqual(await replay.json(),lost);
  const changed=await f.send({operation:'complete',id:'one',result:{...result,stdout:'changed'}});assert.equal(changed.status,409);
  row=f.sql.prepare('SELECT state,result,result_retired FROM maintenance_jobs WHERE id=?').get('one');assert.equal(row.state,'succeeded');assert.equal(row.result_retired,1);assert.equal(JSON.parse(row.result).stdout,undefined);
  const visible=await maintenanceCall(f.env,'maintenance.result',{id:'one'},'read');assert.equal(visible.result.result_retired,true);assert.equal(visible.result.result_sha256,lost.result_sha256);
  await maintenanceCall(f.env,'maintenance.submit',{host:f.host,argv:['/bin/echo','one']},'one');assert.deepEqual((await (await f.send({operation:'poll'})).json()).jobs,[]);
 }finally{f.sql.close()}
});

test('future count byte and age budgets bound payloads without pruning active uncertain or legacy work',async()=>{
 const f=await fixture({MAINTENANCE_RESULT_MAX_COUNT:2,MAINTENANCE_RESULT_MAX_BYTES:1000,MAINTENANCE_RESULT_MAX_AGE_SECONDS:1000});try{
  f.job('old');await f.send({operation:'complete',id:'old',result:{state:'succeeded',stdout:'legacy'}});
  for(let i=0;i<4;i++){await f.admit('new'+i);await f.send({operation:'complete',id:'new'+i,result:{state:'succeeded',stdout:'x'.repeat(40)}})}
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs WHERE retention_version=1 AND result_retired=0').get().n,2);
  await f.admit('active');await f.admit('uncertain');await f.send({operation:'complete',id:'uncertain',result:{state:'uncertain',reason:'reconcile'}});
  f.env.MAINTENANCE_RESULT_MAX_BYTES=1;await maintenanceResultRetention(f.env,f.host,Date.now()/1000,true);
  assert.equal(f.sql.prepare("SELECT COUNT(*) AS n FROM maintenance_jobs WHERE retention_version=1 AND state='succeeded' AND result_retired=0").get().n,0);
  assert.equal(f.sql.prepare('SELECT state,result_retired FROM maintenance_jobs WHERE id=?').get('active').state,'claimed');
  assert.equal(f.sql.prepare('SELECT result_retired FROM maintenance_jobs WHERE id=?').get('uncertain').result_retired,0);
  assert.equal(JSON.parse(f.sql.prepare('SELECT result FROM maintenance_jobs WHERE id=?').get('old').result).stdout,'legacy');
  f.env.MAINTENANCE_RESULT_MAX_BYTES=1000;await f.admit('aged');await f.send({operation:'complete',id:'aged',result:{state:'failed',exit_code:2,stdout:'age'}});
  await maintenanceResultRetention(f.env,f.host,Date.now()/1000+1001,true);assert.equal(f.sql.prepare('SELECT result_retired FROM maintenance_jobs WHERE id=?').get('aged').result_retired,1);
 }finally{f.sql.close()}
});

test('cleanup scans bounded indexed metadata and drains at most 200 payloads per invocation',async()=>{
 const f=await fixture({MAINTENANCE_RESULT_MAX_COUNT:0});try{
  const result={state:'succeeded',stdout:'future'},raw=JSON.stringify(result),hash=sha(canonical(result));
  const insert=f.sql.prepare("INSERT INTO maintenance_jobs(id,host,request,state,result,created,updated,retention_version,result_digest,result_bytes,completed,result_retired) VALUES(?,?,?,'succeeded',?,0,0,1,?,?,?,0)");
  for(let i=0;i<450;i++)insert.run('id-'+String(i).padStart(4,'0'),f.host,'{}',raw,hash,raw.length,i);
  const first=await maintenanceResultRetention(f.env,f.host,1000,true);assert.equal(first.retired,200);assert.equal(first.pending,true);
  const second=await maintenanceResultRetention(f.env,f.host,1000,false);assert.equal(second.retired,200);
  const third=await maintenanceResultRetention(f.env,f.host,1000,false);assert.equal(third.retired,50);
  assert.equal((await maintenanceResultRetention(f.env,f.host,1000,false)).checked,false);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs').get().n,450);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs WHERE result_retired=0').get().n,0);
 }finally{f.sql.close()}
});

test('cleanup failure never falsely retires a result or prevents its exact durable ACK',async()=>{
 const f=await fixture({MAINTENANCE_RESULT_MAX_COUNT:0});try{
  await f.admit('one');f.sql.exec("CREATE TRIGGER fail_retire BEFORE UPDATE OF result_retired ON maintenance_jobs BEGIN SELECT RAISE(ABORT,'disk fault'); END");
  const result={state:'succeeded',stdout:'durable'};const response=await f.send({operation:'complete',id:'one',result});assert.equal(response.status,200);
  const receipt=await response.json();assert.equal(receipt.accepted,true);assert.equal(receipt.result_sha256,sha(canonical(result)));assert.equal(receipt.retention.pending,true);
  assert.equal(f.sql.prepare('SELECT result_retired FROM maintenance_jobs WHERE id=?').get('one').result_retired,0);
  f.sql.exec('DROP TRIGGER fail_retire');const retry=await (await f.send({operation:'complete',id:'one',result})).json();assert.equal(retry.result_sha256,receipt.result_sha256);
  assert.equal(f.sql.prepare('SELECT result_retired FROM maintenance_jobs WHERE id=?').get('one').result_retired,1);
 }finally{f.sql.close()}
});


test('metadata scan is index-pinned and capped independently of history size',async()=>{
 const f=await fixture();try{
  const prepare=f.env.DB.prepare.bind(f.env.DB),queries=[];
  f.env.DB.prepare=query=>{queries.push(query);return prepare(query)};
  await maintenanceResultRetention(f.env,f.host,1000,true);
  const scan=queries.find(q=>q.startsWith('WITH candidates'));
  assert.match(scan,/INDEXED BY maintenance_future_results/);assert.match(scan,/ORDER BY completed DESC,id DESC LIMIT \?/);assert.match(scan,/LIMIT 200$/);
  assert.ok(!scan.includes('SELECT result FROM'));assert.ok(!scan.includes('SUM(length(result'));
  f.env.MAINTENANCE_RESULT_MAX_COUNT=10001;
  await assert.rejects(maintenanceResultRetention(f.env,f.host,1000,true),/bounded scan budget/);
 }finally{f.sql.close()}
});


test('existing deployed schema migrates without enrolling or changing historical payloads',async()=>{
 const sql=new DatabaseSync(':memory:'),token='e'.repeat(64),host='legacy-mac';
 sql.exec("CREATE TABLE maintenance_hosts(id TEXT PRIMARY KEY,key_hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,seen REAL,phone_key TEXT NOT NULL,phone_device TEXT NOT NULL,site TEXT NOT NULL);CREATE TABLE maintenance_jobs(id TEXT PRIMARY KEY,host TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,result TEXT,confirmation TEXT,created REAL NOT NULL,updated REAL NOT NULL)");
 sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run(host,sha(token),'key','device','site');
 const history=JSON.stringify({state:'succeeded',stdout:'untouched historical output'});
 sql.prepare("INSERT INTO maintenance_jobs VALUES(?,?,?,'succeeded',?,NULL,1,1)").run('old',host,'{}',history);
 const DB=sqliteD1(sql);
 try{
  const response=await maintenanceDevice(new Request('https://test.invalid/maintenance/device',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify({operation:'poll'})}),{DB,MAINTENANCE_RESULT_MAX_COUNT:0});
  assert.equal(response.status,200);const row=sql.prepare('SELECT result,retention_version,result_retired FROM maintenance_jobs WHERE id=?').get('old');
  assert.equal(row.result,history);assert.equal(row.retention_version,null);assert.equal(row.result_retired,0);
 }finally{sql.close()}
});

test('empty boolean or malformed policy cannot accidentally become destructive zero limits',async()=>{
 const f=await fixture();try{
  await f.admit('one');await f.send({operation:'complete',id:'one',result:{state:'succeeded',stdout:'kept'}});
  for(const value of ['',null,false,'-1','1.5']){
   f.env.MAINTENANCE_RESULT_MAX_COUNT=value;
   await assert.rejects(maintenanceResultRetention(f.env,f.host,Date.now()/1000,true),/Invalid maintenance retention limit/);
  }
  assert.equal(f.sql.prepare('SELECT result_retired FROM maintenance_jobs WHERE id=?').get('one').result_retired,0);
 }finally{f.sql.close()}
});

import worker from '../worker/index.js';
import {runTool} from '../lib/bridge.mjs';
test('canonical worker exposes maintenance device without depending on controller routing',async()=>{
 const response=await worker.fetch(new Request('https://test.invalid/maintenance/device'),{});
 assert.equal(response.status,405);
});
test('canonical bridge routes maintenance admission directly and preserves unsigned delivery',async()=>{
 const f=await fixture();try{
  const value=await runTool(f.env,'biome_invoke',{operation:'maintenance.submit',arguments:{host:f.host,argv:['/bin/echo','test']},request_id:'canonical-route'});
  assert.ok(!value.isError);assert.equal(JSON.parse(value.content[0].text).state,'queued');
  const response=await f.send({operation:'poll'});const body=await response.json();
  assert.equal(body.jobs[0].id,'canonical-route');assert.equal(body.jobs[0].confirmation,undefined);
 }finally{f.sql.close()}
});
