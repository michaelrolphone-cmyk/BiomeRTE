import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash,generateKeyPairSync,sign} from 'node:crypto';
import {maintenanceCall,maintenanceDevice,maintenancePhone,maintenanceResultRetention} from '../lib/maintenance.mjs';
import {sqliteD1} from './sqlite-d1.mjs';
const sha=value=>createHash('sha256').update(value).digest('hex');
function canonical(value){return JSON.stringify((function sorted(v){return Array.isArray(v)?v.map(sorted):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,sorted(v[k])])):v})(value)).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'))}
async function fixture(settings={}){
 const sql=new DatabaseSync(':memory:'),token='d'.repeat(64),host='mac',env={DB:sqliteD1(sql),...settings};
 const send=body=>maintenanceDevice(new Request('https://test.invalid/maintenance/device',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(body)}),env);
 await send({operation:'poll'});
 sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run(host,sha(token),'key','phone','site');
 const input=(id,extra={})=>({host,argv:['/bin/bash','-c','echo '+id],...extra});
 const submit=(id,args=input(id))=>maintenanceCall(env,'maintenance.submit',args,id);
 const finish=async(id,result={state:'succeeded',stdout:'done'})=>{await send({operation:'poll'});const response=await send({operation:'complete',id,result});assert.equal(response.status,200);return response.json()};
 const row=id=>sql.prepare('SELECT * FROM maintenance_jobs WHERE id=?').get(id);
 const audit=id=>sql.prepare('SELECT * FROM maintenance_audit WHERE id=?').get(id);
 return {sql,env,host,token,send,input,submit,finish,row,audit};
}

test('exact submit retries before and after compaction preserve identity without rerun or regrowth',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0,MAINTENANCE_RESULT_MAX_COUNT:0});try{
  const args=f.input('one',{argv:['/bin/bash','-c','printf "café ☀ 😀"; '+ 'x'.repeat(65536)],cwd:'/tmp',diagnostic:'audit-only'});
  await Promise.all([f.submit('one',args),f.submit('one',args)]);
  const request=f.row('one').request,originalAudit=f.audit('one').body;
  assert.equal(f.row('one').request_digest,sha(request));assert.equal(f.row('one').request_bytes,Buffer.byteLength(request));
  assert.equal(f.audit('one').body_digest,sha(originalAudit));assert.equal(f.audit('one').body_bytes,Buffer.byteLength(originalAudit));
  const receipt=await f.finish('one');
  assert.equal(f.row('one').request_retired,1);assert.equal(f.row('one').request_bytes,0);assert.equal(f.audit('one').body_retired,1);
  assert.deepEqual(JSON.parse(f.row('one').request),{request_retired:true,request_sha256:sha(request)});
  assert.deepEqual(JSON.parse(f.audit('one').body),{body_retired:true,body_sha256:sha(originalAudit),request_sha256:sha(request)});
  const compact={job:f.row('one'),audit:f.audit('one')};
  // Extra arguments and object-key order never belonged to command identity.
  const retries=await Promise.all([f.submit('one',args),f.submit('one',{cwd:'/tmp',argv:args.argv,host:f.host,diagnostic:'changed extra'})]);
  for(const reply of retries)assert.deepEqual(reply,{id:'one',accepted:true,state:'succeeded'});
  assert.deepEqual(f.row('one'),compact.job);assert.deepEqual(f.audit('one'),compact.audit);
  assert.deepEqual((await (await f.send({operation:'poll'})).json()).jobs,[]);
  assert.equal((await (await f.send({operation:'complete',id:'one',result:{state:'succeeded',stdout:'done'}})).json()).result_sha256,receipt.result_sha256);
  assert.equal((await f.send({operation:'complete',id:'one',result:{state:'succeeded',stdout:'altered'}})).status,409);
  f.sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run('other','other-key','key','phone','site');
  for(const changed of [{...args,argv:[...args.argv,'altered']},{...args,cwd:'/'},{...args,host:'other'}])await assert.rejects(f.submit('one',changed),/different command/);
  assert.equal((await f.send({operation:'complete',id:'another-id',result:{state:'succeeded',stdout:'done'}})).status,404);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs').get().n,1);
 }finally{f.sql.close()}
});

test('omitted and null cwd retain existing retry equivalence while argv order stays exact',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0});try{
  const args=f.input('one');await f.submit('one',args);await f.submit('one',{...args,cwd:null});
  await assert.rejects(f.submit('one',{...args,argv:[args.argv[0],args.argv[2],args.argv[1]]}),/different command/);
  await f.finish('one');await f.submit('one',{...args,cwd:null});
  await assert.rejects(f.submit('one',{...args,cwd:''}),/different command/);
 }finally{f.sql.close()}
});

test('new request/audit cutover never backfills legacy or previous result-retention rows',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0,MAINTENANCE_RESULT_MAX_COUNT:0});try{
  for(const [id,version] of [['legacy',null],['prior-result',1]]){
   await f.submit(id);f.sql.prepare('UPDATE maintenance_jobs SET retention_version=?,request_digest=NULL,request_bytes=NULL,approval_digest=NULL WHERE id=?').run(version,id);
   f.sql.prepare('UPDATE maintenance_audit SET retention_version=NULL,body_digest=NULL,body_bytes=NULL WHERE id=?').run(id);
   const request=f.row(id).request,audit=f.audit(id).body;
   await f.submit(id);await f.finish(id);
   assert.equal(f.row(id).request,request);assert.equal(f.row(id).request_digest,null);assert.equal(f.row(id).request_retired,0);
   assert.equal(f.audit(id).body,audit);assert.equal(f.audit(id).retention_version,null);assert.equal(f.audit(id).body_retired,0);
  }
  await f.submit('new');await f.finish('new');assert.equal(f.row('new').request_retired,1);assert.equal(f.audit('new').body_retired,1);
 }finally{f.sql.close()}
});

test('active uncertain uncommitted and authority-dependent records remain intact',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0});try{
  for(const state of ['queued','claimed','awaiting_confirmation','needs_review','cancelled','rejected','uncertain']){
   await f.submit(state);f.sql.prepare('UPDATE maintenance_jobs SET state=?,result_digest=?,completed=1 WHERE id=?').run(state,'0'.repeat(64),state);
  }
  for(const id of ['no-result-digest','no-completion','signed-authority']){await f.submit(id);await f.finish(id,{state:'uncertain'});}
  f.sql.prepare("UPDATE maintenance_jobs SET state='succeeded',result_digest=NULL WHERE id='no-result-digest'").run();
  f.sql.prepare("UPDATE maintenance_jobs SET state='succeeded',completed=NULL WHERE id='no-completion'").run();
  f.sql.prepare("UPDATE maintenance_jobs SET state='succeeded',confirmation=? WHERE id='signed-authority'").run(JSON.stringify({signed_envelope:{id:'pending-approval'},outcome:'approved'}));
  const before=f.sql.prepare('SELECT id,request,confirmation FROM maintenance_jobs ORDER BY id').all();
  for(const action of ['maintenance.confirmation','maintenance.cancel','maintenance.enroll','maintenance.revoke']){
   f.sql.prepare('INSERT INTO maintenance_audit(id,action,host,body,created) VALUES(?,?,?,?,1)').run(action,action,f.host,JSON.stringify({response_pending:true,request:{reason:'keep full authority evidence'}}));
  }
  const audits=f.sql.prepare('SELECT id,body FROM maintenance_audit ORDER BY id').all();
  await maintenanceResultRetention(f.env,f.host,Date.now()/1000,true);
  assert.deepEqual(f.sql.prepare('SELECT id,request,confirmation FROM maintenance_jobs ORDER BY id').all(),before);
  assert.deepEqual(f.sql.prepare('SELECT id,body FROM maintenance_audit ORDER BY id').all(),audits);
 }finally{f.sql.close()}
});

test('request count byte and age limits include approval and audit copies per host',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:2});try{
  for(let i=0;i<4;i++){await f.submit('n'+i);await f.finish('n'+i)}
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs WHERE request_retired=0').get().n,2);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_audit WHERE body_retired=0').get().n,2);
  f.env.MAINTENANCE_REQUEST_MAX_COUNT=1000;
  const id='unicode',args=f.input(id,{argv:['/bin/echo','☀'.repeat(2048)]});await f.submit(id,args);await f.finish(id);
  const bytes=f.row(id).request_bytes+f.audit(id).body_bytes;
  assert.ok(bytes>Buffer.byteLength(JSON.stringify(args))*2,'approval repeats argv and must be included');
  f.env.MAINTENANCE_REQUEST_MAX_BYTES=bytes-1;
  await maintenanceResultRetention(f.env,f.host,Date.now()/1000,true);
  assert.equal(f.row(id).request_retired,1);assert.equal(f.audit(id).body_retired,1);
  const remaining=f.sql.prepare("SELECT COALESCE(SUM(j.request_bytes+a.body_bytes),0) AS bytes FROM maintenance_jobs j JOIN maintenance_audit a ON a.id=j.id WHERE j.request_retired=0").get().bytes;
  assert.ok(remaining<=bytes-1);
  f.env.MAINTENANCE_REQUEST_MAX_BYTES=4*1024*1024;f.env.MAINTENANCE_REQUEST_MAX_AGE_SECONDS=10;
  await f.submit('aged');await f.finish('aged');const completed=f.row('aged').completed;
  await maintenanceResultRetention(f.env,f.host,completed+10,true);assert.equal(f.row('aged').request_retired,0);
  await maintenanceResultRetention(f.env,f.host,completed+10.01,true);assert.equal(f.row('aged').request_retired,1);assert.equal(f.audit('aged').body_retired,1);
 }finally{f.sql.close()}
});

test('bounded request sweep scans indexed metadata and drains backlog in atomic 200-pair batches',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0});try{
  const insert=f.sql.prepare("INSERT INTO maintenance_jobs(id,host,request,state,created,updated,retention_version,request_digest,request_bytes,approval_digest,result_digest,completed) VALUES(?,?,?,'succeeded',0,0,1,?,100,?,?,?)");
  const audit=f.sql.prepare("INSERT INTO maintenance_audit(id,action,host,body,created,retention_version,body_digest,body_bytes) VALUES(?,'maintenance.submit',?,?,0,1,?,100)");
  for(let i=0;i<450;i++){const id='id-'+String(i).padStart(4,'0');insert.run(id,f.host,'{}',sha('request'+i),sha('approval'+i),sha('result'+i),i);audit.run(id,f.host,'{}',sha('audit'+i));}
  const prepare=f.env.DB.prepare.bind(f.env.DB),queries=[];
  f.env.DB.prepare=query=>{queries.push(query);return prepare(query)};
  const first=await maintenanceResultRetention(f.env,f.host,1000,true);assert.equal(first.requests_retired,200);assert.equal(first.pending,true);
  const second=await maintenanceResultRetention(f.env,f.host,1000,false);assert.equal(second.requests_retired,200);
  const third=await maintenanceResultRetention(f.env,f.host,1000,false);assert.equal(third.requests_retired,50);
  assert.equal((await maintenanceResultRetention(f.env,f.host,1000,false)).checked,false);
  const scan=queries.find(q=>q.startsWith('WITH candidates')&&q.includes('maintenance_future_requests'));
  assert.match(scan,/INDEXED BY maintenance_future_requests/);assert.match(scan,/ORDER BY completed DESC,id DESC LIMIT \?/);assert.match(scan,/LIMIT 200$/);
  assert.ok(!scan.includes('SELECT request FROM'));assert.ok(!scan.includes('length('));
  const plan=f.sql.prepare('EXPLAIN QUERY PLAN '+scan).all(f.host,200,1000,0,1000,1000,0).map(r=>r.detail).join('\n');
  assert.match(plan,/USING INDEX maintenance_future_requests/);assert.match(plan,/SEARCH a USING INDEX sqlite_autoindex_maintenance_audit_1/);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_jobs WHERE request_retired=1').get().n,450);
  assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM maintenance_audit WHERE body_retired=1').get().n,450);
  f.env.MAINTENANCE_REQUEST_MAX_COUNT=10001;await assert.rejects(maintenanceResultRetention(f.env,f.host,1000,true),/bounded scan budget/);
 }finally{f.sql.close()}
});

test('failed admission and interrupted pair compaction roll back without losing exact ACK or replay',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0});try{
  f.sql.exec("CREATE TRIGGER fail_audit_insert BEFORE INSERT ON maintenance_audit BEGIN SELECT RAISE(ABORT,'interrupted admission'); END");
  await assert.rejects(f.submit('one'),/interrupted admission/);assert.equal(f.row('one'),undefined);
  f.sql.exec('DROP TRIGGER fail_audit_insert');await f.submit('one');
  const request=f.row('one').request,audit=f.audit('one').body;
  f.sql.exec("CREATE TRIGGER fail_audit_retirement BEFORE UPDATE OF body_retired ON maintenance_audit BEGIN SELECT RAISE(ABORT,'interrupted compaction'); END");
  const receipt=await f.finish('one');assert.equal(receipt.accepted,true);assert.equal(receipt.retention.pending,true);
  assert.equal(f.row('one').request,request);assert.equal(f.row('one').request_retired,0);assert.equal(f.audit('one').body,audit);
  await f.submit('one');assert.deepEqual((await (await f.send({operation:'poll'})).json()).jobs,[]);
  f.sql.exec('DROP TRIGGER fail_audit_retirement');const retry=await f.finish('one');assert.equal(retry.result_sha256,receipt.result_sha256);
  assert.equal(f.row('one').request_retired,1);assert.equal(f.audit('one').body_retired,1);
  // Simulate process loss after commit but before the HTTP submit result is used.
  assert.deepEqual(await f.submit('one'),{id:'one',accepted:true,state:'succeeded'});
 }finally{f.sql.close()}
});

test('late signed phone receipts recover after compaction without retaining the full approval question',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_COUNT:0});try{
  const pair=generateKeyPairSync('rsa',{modulusLength:2048}),publicKey=pair.publicKey.export({type:'spki',format:'pem'});
  f.sql.prepare('UPDATE maintenance_hosts SET phone_key=? WHERE id=?').run(publicKey,f.host);
  await f.submit('one');const approval=JSON.parse(f.row('one').request).approval;
  const args={decision_id:sha(canonical([approval.id,1])),digest:sha(canonical(approval)),proposal_revision:1,choice:'approve',text:''};
  await f.finish('one');assert.equal(f.row('one').request_retired,1);
  const unsigned={command:'respond',device:'phone',site:'site',id:'late-phone',payload:Buffer.from(JSON.stringify(args)).toString('base64')};
  const envelope={...unsigned,signature:sign('RSA-SHA256',Buffer.from(canonical(unsigned)),pair.privateKey).toString('base64')};
  const response=await maintenancePhone(f.env,envelope);assert.equal(response.outcome,'already_settled');assert.equal(response.request_id,approval.id);
  assert.deepEqual(await maintenancePhone(f.env,envelope),response);assert.equal(f.row('one').state,'succeeded');
  const changed={...unsigned,payload:Buffer.from(JSON.stringify({...args,choice:'reject'})).toString('base64')};
  await assert.rejects(maintenancePhone(f.env,{...changed,signature:sign('RSA-SHA256',Buffer.from(canonical(changed)),pair.privateKey).toString('base64')}),/Phone response ID reused/);
  assert.equal(f.row('one').request_retired,1);assert.deepEqual((await (await f.send({operation:'poll'})).json()).jobs,[]);
 }finally{f.sql.close()}
});

test('malformed request budgets fail closed and cannot silently become destructive zero values',async()=>{
 const f=await fixture();try{
  await f.submit('one');await f.finish('one');
  for(const name of ['MAINTENANCE_REQUEST_MAX_COUNT','MAINTENANCE_REQUEST_MAX_BYTES','MAINTENANCE_REQUEST_MAX_AGE_SECONDS']){
   for(const value of ['',null,false,'-1','1.5',Number.NaN]){f.env[name]=value;await assert.rejects(maintenanceResultRetention(f.env,f.host,Date.now()/1000,true),/Invalid maintenance retention limit/)}
   delete f.env[name];
  }
  assert.equal(f.row('one').request_retired,0);assert.equal(f.audit('one').body_retired,0);
 }finally{f.sql.close()}
});

test('historical five-column audit schema migrates with payloads and markers untouched',async()=>{
 const sql=new DatabaseSync(':memory:');
 sql.exec("CREATE TABLE maintenance_hosts(id TEXT PRIMARY KEY,key_hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,seen REAL,retention_checked REAL,phone_key TEXT NOT NULL,phone_device TEXT NOT NULL,site TEXT NOT NULL);CREATE TABLE maintenance_jobs(id TEXT PRIMARY KEY,host TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,result TEXT,confirmation TEXT,created REAL NOT NULL,updated REAL NOT NULL,retention_version INTEGER,result_digest TEXT,result_bytes INTEGER,completed REAL,result_retired INTEGER NOT NULL DEFAULT 0);CREATE TABLE maintenance_audit(id TEXT PRIMARY KEY,action TEXT NOT NULL,host TEXT,body TEXT,created REAL NOT NULL)");
 sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run('mac','hash','key','phone','site');
 const args={host:'mac',argv:['/bin/echo','historical']},approval={id:'maintenance:old',workflow_instance_id:'maintenance:old',proposal_revision:1,question:'Allow this one maintenance command on mac?\nExecutable and arguments: '+JSON.stringify(args.argv)+'\nWorking directory: service default',choices:[{id:'approve',label:'Allow this command once',meaning:'approved'},{id:'reject',label:'Reject',meaning:'rejected'}]};
 const request=JSON.stringify({argv:args.argv,cwd:null,approval}),audit=JSON.stringify(args),result=JSON.stringify({state:'succeeded',stdout:'old'});
 sql.prepare("INSERT INTO maintenance_jobs(id,host,request,state,result,created,updated,retention_version,result_digest,result_bytes,completed) VALUES('old','mac',?,'succeeded',?,1,1,1,?,?,1)").run(request,result,sha(canonical(JSON.parse(result))),result.length);
 sql.prepare("INSERT INTO maintenance_audit VALUES('old','maintenance.submit','mac',?,1)").run(audit);
 const env={DB:sqliteD1(sql),MAINTENANCE_REQUEST_MAX_COUNT:0};
 try{
  await maintenanceCall(env,'maintenance.submit',args,'old');await maintenanceResultRetention(env,'mac',1000,true);
  const job=sql.prepare("SELECT * FROM maintenance_jobs WHERE id='old'").get(),record=sql.prepare("SELECT * FROM maintenance_audit WHERE id='old'").get();
  assert.equal(job.request,request);assert.equal(job.request_digest,null);assert.equal(job.request_retired,0);assert.equal(job.retention_version,1);
  assert.equal(record.body,audit);assert.equal(record.retention_version,null);assert.equal(record.body_digest,null);assert.equal(record.body_retired,0);
 }finally{sql.close()}
});

test('audit-only oversized payloads are budgeted and another host is never retired',async()=>{
 const f=await fixture({MAINTENANCE_REQUEST_MAX_BYTES:2048});try{
  const args=f.input('one',{extra:'x'.repeat(32768)});await f.submit('one',args);assert.ok(f.row('one').request_bytes<2048);
  await f.submit('foreign');f.sql.prepare("UPDATE maintenance_jobs SET host='other',state='succeeded',result_digest=?,completed=0 WHERE id='foreign'").run(sha('result'));
  f.sql.prepare("UPDATE maintenance_audit SET host='other' WHERE id='foreign'").run();const foreign=f.row('foreign'),foreignAudit=f.audit('foreign');
  await f.finish('one');assert.equal(f.row('one').request_retired,1);assert.equal(f.audit('one').body_retired,1);
  assert.deepEqual(f.row('foreign'),foreign);assert.deepEqual(f.audit('foreign'),foreignAudit);
 }finally{f.sql.close()}
});

test('changed authority after candidate selection prevents paired compaction',async()=>{
 const f=await fixture();try{
  await f.submit('one');await f.finish('one');const request=f.row('one').request,audit=f.audit('one').body;
  f.env.MAINTENANCE_REQUEST_MAX_COUNT=0;
  const prepare=f.env.DB.prepare.bind(f.env.DB);let intercepted=false;
  f.env.DB.prepare=query=>{
   const statement=prepare(query);
   if(query.startsWith('WITH candidates')&&query.includes('maintenance_future_requests')){
    const all=statement.all;statement.all=async()=>{const result=await all();intercepted=true;f.sql.prepare("UPDATE maintenance_jobs SET confirmation=? WHERE id='one'").run(JSON.stringify({response_pending:true}));return result};
   }
   return statement;
  };
  await maintenanceResultRetention(f.env,f.host,Date.now()/1000,true);assert.equal(intercepted,true);
  assert.equal(f.row('one').request,request);assert.equal(f.row('one').request_retired,0);assert.equal(f.audit('one').body,audit);assert.equal(f.audit('one').body_retired,0);
 }finally{f.sql.close()}
});

test('submit retry racing compaction cannot replace the original audit or restore payloads',async()=>{
 const f=await fixture();try{
  await f.submit('one');await f.finish('one');const originalAuditDigest=f.audit('one').body_digest;
  f.env.MAINTENANCE_REQUEST_MAX_COUNT=0;
  await Promise.all([maintenanceResultRetention(f.env,f.host,Date.now()/1000,true),f.submit('one',f.input('one',{extra:'retry-only payload'}))]);
  assert.equal(f.row('one').request_retired,1);assert.equal(f.audit('one').body_retired,1);assert.equal(f.audit('one').body_digest,originalAuditDigest);
  assert.deepEqual((await (await f.send({operation:'poll'})).json()).jobs,[]);
 }finally{f.sql.close()}
});

test('simultaneous conflicting admissions keep one winning request and its matching audit',async()=>{
 const f=await fixture();try{
  f.sql.prepare('INSERT INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site) VALUES(?,?,?,?,?)').run('other','other-key','key','phone','site');
  for(const [id,loser] of [['argv-race',f.input('changed')],['host-race',f.input('host-race',{host:'other'})]]){
   const inputs=[f.input(id),loser],outcomes=await Promise.allSettled(inputs.map(args=>f.submit(id,args)));
   assert.equal(outcomes.filter(r=>r.status==='fulfilled').length,1);assert.equal(outcomes.filter(r=>r.status==='rejected').length,1);
   const winner=inputs[outcomes.findIndex(r=>r.status==='fulfilled')];
   assert.equal(f.row(id).host,winner.host);assert.deepEqual(JSON.parse(f.row(id).request).argv,winner.argv);
   assert.equal(f.audit(id).host,winner.host);assert.deepEqual(JSON.parse(f.audit(id).body),winner);assert.equal(f.audit(id).body_digest,sha(JSON.stringify(winner)));
  }
 }finally{f.sql.close()}
});
