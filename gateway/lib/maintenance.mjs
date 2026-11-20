import {hexHash,reply,runTool} from './bridge.mjs';
function maintenanceCanonical(value){return JSON.stringify((function order(v){return Array.isArray(v)?v.map(order):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,order(v[k])])):v})(value)).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'))}
function maintenanceBytes(value){return Uint8Array.from(atob(value),c=>c.charCodeAt(0))}
export async function maintenancePhone(env,envelope){
 await maintenanceSchema(env.DB);
 const hosts=(await env.DB.prepare('SELECT * FROM maintenance_hosts WHERE phone_device=? AND site=? AND enabled=1').bind(envelope.device??'',envelope.site??'').all()).results;
 if(!hosts.length)return null;
 const {signature,...unsigned}=envelope;
 if(typeof signature!=='string'||!['inbox','respond'].includes(envelope.command))throw Error('Signed phone response required');
 const key=await crypto.subtle.importKey('spki',maintenanceBytes(hosts[0].phone_key.replace(/-----[^-]+-----|\s/g,'')),{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
 if(!await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,maintenanceBytes(signature),new TextEncoder().encode(maintenanceCanonical(unsigned))))throw Error('Invalid phone signature');
 const prior=await env.DB.prepare("SELECT body FROM maintenance_audit WHERE id=? AND action='maintenance.confirmation'").bind('phone:'+envelope.id).first();
 if(prior){const receipt=JSON.parse(prior.body);if(maintenanceCanonical(receipt.signed_envelope)!==maintenanceCanonical(envelope))throw Error('Phone response ID reused');if(!receipt.response_pending)return receipt;}
 const args=JSON.parse(new TextDecoder().decode(maintenanceBytes(envelope.payload)));
 const jobs=(await env.DB.prepare("SELECT j.* FROM maintenance_jobs j JOIN maintenance_hosts h ON h.id=j.host WHERE h.phone_device=? AND h.site=? AND h.enabled=1"+(envelope.command==='inbox'?" AND j.state='awaiting_confirmation'":"")+" ORDER BY j.created").bind(envelope.device,envelope.site).all()).results;
 const cards=[];
 for(const job of jobs){
  // Retired commands keep the exact proposal digest for late signed replies.
  const request=job.request_retired===1?{id:'maintenance:'+job.id}:JSON.parse(job.request).approval,id=await hexHash(maintenanceCanonical([request.id,1])),digest=job.request_retired===1?job.approval_digest:await hexHash(maintenanceCanonical(request));
  cards.push({job,id,request,digest,state:'pending',responses:[],created_at:job.created});
 }
 if(envelope.command==='inbox')return cards.length?{decisions:cards.filter(c=>c.id>(args.cursor??'')).map(({job,...card})=>card),next_cursor:'',site:envelope.site,observed_at:Date.now()/1000}:null;
 const card=cards.find(c=>c.id===args.decision_id);
 if(!card)return null;
 const text=args.text??'';
 if(args.digest!==card.digest||args.proposal_revision!==1||!['approve','reject',''].includes(args.choice)||typeof text!=='string'||new TextEncoder().encode(text).length>16384||(!args.choice&&!text.trim()))throw Error('Exact current maintenance proposal required');
 const auditId='phone:'+envelope.id;
 await env.DB.prepare('INSERT OR IGNORE INTO maintenance_audit(id,action,host,body,created) VALUES(?,?,?,?,?)').bind(auditId,'maintenance.confirmation',card.job.host,JSON.stringify({signed_envelope:envelope,response_pending:true}),Date.now()/1000).run();
 const reservation=await env.DB.prepare("SELECT body FROM maintenance_audit WHERE id=? AND action='maintenance.confirmation'").bind(auditId).first();
 if(!reservation)throw Error('Response ID belongs to another operation');
 const reserved=JSON.parse(reservation.body);
 if(maintenanceCanonical(reserved.signed_envelope)!==maintenanceCanonical(envelope))throw Error('Phone response ID reused');
 if(!reserved.response_pending)return reserved;
 // A signed written reply is evidence, not permission. Acknowledge it so the
 // Android outbox can drain, retaining it for a fresh proposal after review.
 const state=args.choice==='reject'?'rejected':args.choice==='approve'&&!text.trim()?'queued':'needs_review';
 let response={id:await hexHash(maintenanceCanonical([envelope.device,envelope.id])),request_id:card.request.id,proposal_revision:1,text,received_at:Date.now()/1000,authenticated:true,device:envelope.device,decision_id:card.id,question_digest:card.digest,choice:args.choice,outcome:state==='queued'?'approved':state,signed_envelope:envelope};
 const priorConfirmation=card.job.confirmation?JSON.parse(card.job.confirmation):null;
 if(priorConfirmation?.signed_envelope?.id===envelope.id){
  if(maintenanceCanonical(priorConfirmation.signed_envelope)!==maintenanceCanonical(envelope))throw Error('Phone response ID reused');
  response=priorConfirmation;
 }else{
  const changed=card.job.state==='awaiting_confirmation'?await env.DB.prepare("UPDATE maintenance_jobs SET state=?,confirmation=?,updated=? WHERE id=? AND state='awaiting_confirmation' RETURNING id").bind(state,JSON.stringify(response),Date.now()/1000,card.job.id).first():null;
  if(!changed){
   const current=await env.DB.prepare('SELECT state,confirmation FROM maintenance_jobs WHERE id=?').bind(card.job.id).first();
   const saved=current?.confirmation?JSON.parse(current.confirmation):null;
   if(saved?.signed_envelope?.id===envelope.id){
    if(maintenanceCanonical(saved.signed_envelope)!==maintenanceCanonical(envelope))throw Error('Phone response ID reused');
    response=saved;
   }else response={...response,outcome:current?.state==='cancelled'?'cancelled':'already_settled'};
  }
 }
 await env.DB.prepare("UPDATE maintenance_audit SET body=? WHERE id=? AND action='maintenance.confirmation' AND body=?").bind(JSON.stringify(response),auditId,reservation.body).run();
 return JSON.parse((await env.DB.prepare('SELECT body FROM maintenance_audit WHERE id=?').bind(auditId).first()).body);
}
async function maintenanceSchema(db){
 await db.prepare('CREATE TABLE IF NOT EXISTS maintenance_hosts(id TEXT PRIMARY KEY,key_hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,seen REAL,retention_checked REAL,phone_key TEXT NOT NULL,phone_device TEXT NOT NULL,site TEXT NOT NULL,agent TEXT,diagnostics TEXT)').run();
 await db.prepare('CREATE TABLE IF NOT EXISTS maintenance_jobs(id TEXT PRIMARY KEY,host TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,result TEXT,confirmation TEXT,created REAL NOT NULL,updated REAL NOT NULL,retention_version INTEGER,result_digest TEXT,result_bytes INTEGER,completed REAL,result_retired INTEGER NOT NULL DEFAULT 0,request_digest TEXT,request_bytes INTEGER,approval_digest TEXT,request_retired INTEGER NOT NULL DEFAULT 0,execution_digest TEXT)').run();
 const hostColumns=(await db.prepare('PRAGMA table_info(maintenance_hosts)').all()).results;
 if(!hostColumns.some(c=>c.name==='retention_checked')){try{await db.prepare('ALTER TABLE maintenance_hosts ADD COLUMN retention_checked REAL').run()}catch(e){if(!String(e.message).includes('duplicate column'))throw e}}
 for(const name of ['agent','diagnostics'])if(!hostColumns.some(c=>c.name===name)){try{await db.prepare('ALTER TABLE maintenance_hosts ADD COLUMN '+name+' TEXT').run()}catch(e){if(!String(e.message).includes('duplicate column'))throw e}}
 const columns=(await db.prepare('PRAGMA table_info(maintenance_jobs)').all()).results;
 for(const [name,type] of [['retention_version','INTEGER'],['result_digest','TEXT'],['result_bytes','INTEGER'],['completed','REAL'],['result_retired','INTEGER NOT NULL DEFAULT 0'],['request_digest','TEXT'],['request_bytes','INTEGER'],['approval_digest','TEXT'],['request_retired','INTEGER NOT NULL DEFAULT 0'],['execution_digest','TEXT']]){
  if(!columns.some(c=>c.name===name)){try{await db.prepare('ALTER TABLE maintenance_jobs ADD COLUMN '+name+' '+type).run()}catch(e){if(!String(e.message).includes('duplicate column'))throw e}}
 }
 await db.prepare("CREATE INDEX IF NOT EXISTS maintenance_future_results ON maintenance_jobs(host,completed DESC,id DESC) WHERE retention_version=1 AND result_retired=0 AND state IN ('succeeded','failed') AND result_digest IS NOT NULL AND completed IS NOT NULL").run();
 if(!columns.some(c=>c.name==='confirmation')){try{await db.prepare('ALTER TABLE maintenance_jobs ADD COLUMN confirmation TEXT').run()}catch(e){if(!String(e.message).includes('duplicate column'))throw e}}
 await db.prepare('CREATE TABLE IF NOT EXISTS maintenance_audit(id TEXT PRIMARY KEY,action TEXT NOT NULL,host TEXT,body TEXT,created REAL NOT NULL,retention_version INTEGER,body_digest TEXT,body_bytes INTEGER,body_retired INTEGER NOT NULL DEFAULT 0)').run();
 const auditColumns=(await db.prepare('PRAGMA table_info(maintenance_audit)').all()).results;
 for(const [name,type] of [['retention_version','INTEGER'],['body_digest','TEXT'],['body_bytes','INTEGER'],['body_retired','INTEGER NOT NULL DEFAULT 0']]){
  if(!auditColumns.some(c=>c.name===name)){try{await db.prepare('ALTER TABLE maintenance_audit ADD COLUMN '+name+' '+type).run()}catch(e){if(!String(e.message).includes('duplicate column'))throw e}}
 }
 await db.prepare('CREATE INDEX IF NOT EXISTS maintenance_history_host_id ON maintenance_jobs(host,id)').run();
 // The additional non-null digests are insert-only cutover fences. Upgrading or
 // retrying an older result-retention job never enrolls its request or audit.
 await db.prepare("CREATE INDEX IF NOT EXISTS maintenance_future_requests ON maintenance_jobs(host,completed DESC,id DESC) WHERE retention_version=1 AND request_retired=0 AND state IN ('succeeded','failed') AND request_digest IS NOT NULL AND approval_digest IS NOT NULL AND request_bytes IS NOT NULL AND result_digest IS NOT NULL AND completed IS NOT NULL AND confirmation IS NULL").run();
}
export async function maintenanceCall(env,op,a,id){
 const db=env.DB,now=Date.now()/1000;await maintenanceSchema(db);
 if(op==='maintenance.retention.plan')return maintenanceHistoryPlan(env,a);
 if(op==='maintenance.retention.apply'){
  if(typeof env.maintenanceActor!=='string'||!env.maintenanceActor)throw Error('Authenticated owner required for historical retention');
  return maintenanceHistoryApply(env,a.plan,a.expected_sha256);
 }
 if(op==='maintenance.status')return {hosts:(await db.prepare('SELECT id,enabled,seen,agent,diagnostics FROM maintenance_hosts').all()).results.map(h=>({id:h.id,enabled:h.enabled,seen:h.seen,...(h.agent?{agent:h.agent,diagnostics:h.diagnostics?JSON.parse(h.diagnostics):null}:{})})),jobs:(await db.prepare('SELECT id,host,state,created,updated FROM maintenance_jobs ORDER BY created DESC').all()).results};
 if(op==='maintenance.result'){
  const row=await db.prepare('SELECT id,host,state,result,created,updated FROM maintenance_jobs WHERE id=?').bind(a.id).first();
  return row?{...row,result:row.result?JSON.parse(row.result):null}:null;
 }
 if(op==='maintenance.cancel'){
  if(typeof env.maintenanceActor!=='string'||!env.maintenanceActor)throw Error('Authenticated owner required for cancellation');
  if(typeof a.id!=='string'||!a.id||typeof (a.reason??'')!=='string')throw Error('Expected command id and optional reason');
  const request={target:a.id,reason:a.reason??'',owner:env.maintenanceActor},auditId='cancel:'+id;
  await db.prepare('INSERT OR IGNORE INTO maintenance_audit(id,action,host,body,created) VALUES(?,?,?,?,?)').bind(auditId,op,null,JSON.stringify({request,response:null}),now).run();
  const reservation=await db.prepare("SELECT body FROM maintenance_audit WHERE id=? AND action='maintenance.cancel'").bind(auditId).first();
  if(!reservation)throw Error('Request ID belongs to another operation');
  const reserved=JSON.parse(reservation.body);
  if(maintenanceCanonical(reserved.request)!==maintenanceCanonical(request))throw Error('Request ID already belongs to a different cancellation');
  if(reserved.response)return reserved.response;
  const result={state:'cancelled',executed:false,reason:request.reason,request_id:id};
  const changed=await db.prepare("UPDATE maintenance_jobs SET state='cancelled',result=?,updated=? WHERE id=? AND state IN ('awaiting_confirmation','needs_review') RETURNING host").bind(JSON.stringify(result),now,a.id).first();
  const row=await db.prepare('SELECT host,state,result FROM maintenance_jobs WHERE id=?').bind(a.id).first();
  if(!row)throw Error('Unknown maintenance command');
  if(row.state!=='cancelled')throw Error('Only unapproved maintenance commands can be cancelled; approved or executing work is unchanged');
  const savedResult=row.result?JSON.parse(row.result):null;
  if(!changed&&savedResult?.request_id===id&&savedResult.reason!==request.reason)throw Error('Request ID already belongs to a different cancellation');
  const response={id:a.id,state:'cancelled',accepted:true,executed:false};
  await db.prepare("UPDATE maintenance_audit SET host=?,body=? WHERE id=? AND action='maintenance.cancel' AND body=?").bind(row.host,JSON.stringify({request,response}),auditId,reservation.body).run();
  return JSON.parse((await db.prepare('SELECT body FROM maintenance_audit WHERE id=?').bind(auditId).first()).body).response;
 }
 if(op==='maintenance.enroll'){
  if(a.agent!==undefined&&a.agent!=='linux-v1')throw Error('Unknown maintenance agent protocol');
  if(a.agent==='linux-v1'){
   if(typeof env.maintenanceActor!=='string'||!env.maintenanceActor)throw Error('Authenticated owner required for Linux enrollment');
   // New Linux hosts use the owner-authenticated gateway directly. Legacy Mac
   // enrollment and signed phone settlement retain their original protocol.
   a={...a,phone_key:'',phone_device:'',site:''};
  }else{
   const identity=await runTool(env,'biome_invoke',{operation:'decision.maintenance_identity',arguments:{},request_id:'maintenance-identity-'+id,realm:'site'});
   if(identity.isError)throw Error('Controller-verified Android identity required for initial enrollment');
   const verified=identity.structuredContent??JSON.parse(identity.content[0].text);
   a={...a,phone_key:verified.key,phone_device:verified.device,site:verified.site};
  }
  if(typeof a.phone_key!=='string'||typeof a.phone_device!=='string'||typeof a.site!=='string')throw Error('Existing Android approval identity required');
  if(typeof a.host!=='string'||!/^[a-zA-Z0-9_.-]+$/.test(a.host)||!/^[0-9a-f]{64}$/.test(a.key_hash))throw Error('Expected host and SHA256 credential verifier');
  const prior=await db.prepare('SELECT key_hash,phone_key,phone_device,site,agent,enabled FROM maintenance_hosts WHERE id=?').bind(a.host).first();
  if(prior&&(prior.key_hash!==a.key_hash||prior.phone_key!==a.phone_key||prior.phone_device!==a.phone_device||prior.site!==a.site||(prior.agent??null)!==(a.agent??null)))throw Error('Revoke and use a new host identity for credential replacement');
  if(a.agent==='linux-v1'){
   if(prior&&prior.enabled!==1)throw Error('Revoked maintenance identity cannot be re-enabled; enroll a new identity');
   if(await db.prepare('SELECT id FROM maintenance_hosts WHERE key_hash=? AND id<>?').bind(a.key_hash,a.host).first())throw Error('Device credential already belongs to another host');
  }
  if(a.agent==='linux-v1'){
   await db.prepare('INSERT OR IGNORE INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site,agent) SELECT ?,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM maintenance_hosts WHERE key_hash=? AND id<>?)').bind(a.host,a.key_hash,a.phone_key,a.phone_device,a.site,a.agent,a.key_hash,a.host).run();
   const saved=await db.prepare('SELECT key_hash,agent,enabled FROM maintenance_hosts WHERE id=?').bind(a.host).first();
   if(!saved||saved.key_hash!==a.key_hash||saved.agent!==a.agent||saved.enabled!==1)throw Error('Maintenance identity enrollment conflict');
  }else await db.prepare('INSERT OR IGNORE INTO maintenance_hosts(id,key_hash,phone_key,phone_device,site,agent) VALUES(?,?,?,?,?,?)').bind(a.host,a.key_hash,a.phone_key,a.phone_device,a.site,null).run();
 }else if(op==='maintenance.revoke'){
  await db.prepare('UPDATE maintenance_hosts SET enabled=0 WHERE id=?').bind(a.host).run();
 }else if(op==='maintenance.submit'){
  const host=await db.prepare('SELECT enabled FROM maintenance_hosts WHERE id=?').bind(a.host).first();
  if(!host?.enabled)throw Error('Maintenance host is not enabled');
  if(!Array.isArray(a.argv)||!a.argv.length||a.argv.some(v=>typeof v!=='string'||v.includes('\0'))||!a.argv[0].startsWith('/'))throw Error('Expected absolute executable and string argv');
  const approval={id:'maintenance:'+id,workflow_instance_id:'maintenance:'+id,proposal_revision:1,question:'Allow this one maintenance command on '+a.host+'?\nExecutable and arguments: '+JSON.stringify(a.argv)+'\nWorking directory: '+(a.cwd??'service default'),choices:[{id:'approve',label:'Allow this command once',meaning:'approved'},{id:'reject',label:'Reject',meaning:'rejected'}]};
  const body=JSON.stringify({argv:a.argv,cwd:a.cwd??null,approval}),digest=await hexHash(body),approvalDigest=await hexHash(maintenanceCanonical(approval)),audit=JSON.stringify(a),auditDigest=await hexHash(audit);
  // Admission and its audit copy commit together. The second statement can only
  // insert against the exact winning command; an altered duplicate has no effect.
  await db.batch([
   db.prepare("INSERT OR IGNORE INTO maintenance_jobs(id,host,request,state,created,updated,retention_version,request_digest,request_bytes,approval_digest,execution_digest) VALUES(?,?,?,'queued',?,?,1,?,?,?,?)").bind(id,a.host,body,now,now,digest,new TextEncoder().encode(body).length,approvalDigest,await hexHash(maintenanceCanonical({argv:a.argv,cwd:a.cwd??null}))),
   db.prepare("INSERT OR IGNORE INTO maintenance_audit(id,action,host,body,created,retention_version,body_digest,body_bytes,body_retired) SELECT id,'maintenance.submit',host,CASE WHEN request_retired=1 THEN ? ELSE ? END,?,CASE WHEN retention_version=1 AND request_digest IS NOT NULL THEN 1 ELSE NULL END,?,CASE WHEN request_retired=1 THEN 0 ELSE ? END,request_retired FROM maintenance_jobs WHERE id=? AND host=? AND ((request_digest=? AND retention_version=1) OR (request_digest IS NULL AND request=?))").bind(JSON.stringify({body_retired:true,body_sha256:auditDigest,request_sha256:digest}),audit,now,auditDigest,new TextEncoder().encode(audit).length,id,a.host,digest,body),
  ]);
  const prior=await db.prepare('SELECT host,request,retention_version,request_digest,request_retired FROM maintenance_jobs WHERE id=?').bind(id).first();
  if(prior.host!==a.host||(prior.retention_version===1&&prior.request_digest?prior.request_digest!==digest:prior.request!==body))throw Error('Request ID already belongs to a different command');
 }else throw Error('Unknown maintenance operation');
 if(op!=='maintenance.submit')await db.prepare('INSERT OR IGNORE INTO maintenance_audit(id,action,host,body,created) VALUES(?,?,?,?,?)').bind(id,op,a.host,JSON.stringify(a),now).run();
 if(op==='maintenance.submit'){
  const current=await db.prepare('SELECT state FROM maintenance_jobs WHERE id=?').bind(id).first();
  return {id,accepted:true,state:current.state,...(current.state==='awaiting_confirmation'?{approval:'Signed Android confirmation required for this exact command'}:{})};
 }
 return {id,accepted:true};
}
export async function maintenanceDevice(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 await maintenanceSchema(env.DB);
 const token=/^Bearer ([0-9a-f]{64})$/.exec(req.headers.get('Authorization')||'')?.[1];
 if(!token)return reply({error:'Unauthorized'},401);
 const host=await env.DB.prepare('SELECT id,agent FROM maintenance_hosts WHERE key_hash=? AND enabled=1').bind(await hexHash(token)).first();
 if(!host)return reply({error:'Unauthorized'},401);
 const d=await req.json(),now=Date.now()/1000;
 await env.DB.prepare('UPDATE maintenance_hosts SET seen=? WHERE id=?').bind(now,host.id).run();
 if(d.operation==='heartbeat'){
  if(host.agent!=='linux-v1')return reply({error:'Linux diagnostics host required'},403);
  const snapshot=d.diagnostics,fields=new Set(['version','agent','pid','boot_id','uptime','loadavg','memory','block_devices','interfaces','routes','executor_pid','executor_wait','executor','executor_error']);
  if(!snapshot||typeof snapshot!=='object'||Array.isArray(snapshot)||snapshot.version!==1||snapshot.agent!=='linux-v1'||Object.keys(snapshot).some(k=>!fields.has(k)))return reply({error:'Invalid Linux diagnostics snapshot'},400);
  await env.DB.prepare('UPDATE maintenance_hosts SET diagnostics=? WHERE id=?').bind(JSON.stringify(snapshot),host.id).run();
  return reply({accepted:true,host:host.id});
 }
 if(d.operation==='retention_history'){
  if(!Array.isArray(d.ids)||d.ids.length>128||d.ids.some(id=>typeof id!=='string'||!id)||new Set(d.ids).size!==d.ids.length)return reply({error:'Expected at most 128 distinct command IDs'},400);
  const records=[],blocked=[],budget={remaining:8*1024*1024};
  for(const id of d.ids){try{const evidence=await maintenanceHistoryRead(env,host.id,id,now,budget);records.push(evidence.record)}catch(error){blocked.push({id,reason:error.message})}}
  return reply({host:host.id,records,blocked});
 }
 if(d.operation==='complete'){
  if(typeof d.id!=='string'||!d.id||!['succeeded','failed','uncertain'].includes(d.result?.state))return reply({error:'Invalid result'},400);
  const incoming=maintenanceCanonical(d.result),incomingDigest=await hexHash(incoming);
  await env.DB.prepare("UPDATE maintenance_jobs SET state=?,result=?,updated=?,result_digest=CASE WHEN retention_version=1 THEN ? ELSE result_digest END,result_bytes=CASE WHEN retention_version=1 THEN ? ELSE result_bytes END,completed=CASE WHEN retention_version=1 THEN ? ELSE completed END WHERE id=? AND host=? AND state='claimed'").bind(d.result.state,JSON.stringify(d.result),now,incomingDigest,new TextEncoder().encode(JSON.stringify(d.result)).length,now,d.id,host.id).run();
  // A compacted future result keeps its immutable digest fence. Matching late
  // retries recover the same ACK without rehydrating an expired payload.
  const saved=await env.DB.prepare('SELECT state,result,retention_version,result_digest,result_retired FROM maintenance_jobs WHERE id=? AND host=?').bind(d.id,host.id).first();
  if(!saved)return reply({error:'Unknown maintenance command'},404);
  let storedDigest;
  if(saved.retention_version===1&&saved.result_retired===1)storedDigest=saved.result_digest;
  else if(saved.result)storedDigest=await hexHash(maintenanceCanonical(JSON.parse(saved.result)));
  if(saved.state!==d.result.state||storedDigest!==incomingDigest||(saved.retention_version===1&&saved.result_digest!==storedDigest))return reply({error:'Maintenance completion differs from stored result'},409);
  let retention;
  try{retention=await maintenanceResultRetention(env,host.id,now,true)}catch{retention={pending:true,error:'maintenance_failed'}}
  return reply({accepted:true,id:d.id,host:host.id,result_sha256:storedDigest,...(retention?.error?{retention}:{})});
 }
 if(d.operation!=='poll')return reply({error:'Unknown operation'},400);
 // Cleanup failures cannot turn delivery into an uncertain effect. Polling
 // retries bounded age maintenance; only future completed payloads are eligible.
 let retention;try{retention=await maintenanceResultRetention(env,host.id,now,false)}catch{retention={pending:true,error:'maintenance_failed'}}
 const jobs=(await env.DB.prepare("SELECT id,request,confirmation FROM maintenance_jobs WHERE host=? AND state IN ('queued','claimed') ORDER BY created").bind(host.id).all()).results;
 for(const job of jobs)await env.DB.prepare("UPDATE maintenance_jobs SET state='claimed',updated=? WHERE id=? AND host=? AND state='queued'").bind(now,job.id,host.id).run();
 const authorized=[];
 for(const job of jobs){
  authorized.push({id:job.id,...JSON.parse(job.request),...(job.confirmation?{confirmation:JSON.parse(job.confirmation)}:{})});
 }
 return reply({jobs:authorized,...(retention?.error?{retention}:{})});
}


export async function maintenanceResultRetention(env,host,now=Date.now()/1000,force=false){
 const read=(name,fallback)=>{const raw=env[name];if(raw!==undefined&&typeof raw!=='number'&&!(typeof raw==='string'&&/^(0|[1-9][0-9]*)$/.test(raw)))throw Error('Invalid maintenance retention limit');const value=raw===undefined?fallback:Number(raw);if(!Number.isSafeInteger(value)||value<0)throw Error('Invalid maintenance retention limit');return value};
 const count=read('MAINTENANCE_RESULT_MAX_COUNT',1000),bytes=read('MAINTENANCE_RESULT_MAX_BYTES',4*1024*1024),age=read('MAINTENANCE_RESULT_MAX_AGE_SECONDS',7*86400);
 const requestCount=read('MAINTENANCE_REQUEST_MAX_COUNT',1000),requestBytes=read('MAINTENANCE_REQUEST_MAX_BYTES',4*1024*1024),requestAge=read('MAINTENANCE_REQUEST_MAX_AGE_SECONDS',7*86400);
 if(count>10000||requestCount>10000)throw Error('Maintenance count limit exceeds bounded scan budget');
 if(!Number.isFinite(now))throw Error('Invalid maintenance retention clock');
 const due=await env.DB.prepare('UPDATE maintenance_hosts SET retention_checked=? WHERE id=? AND (?=1 OR retention_checked IS NULL OR retention_checked<=?) RETURNING id').bind(now,host,force?1:0,now-60).first();
 if(!due)return {checked:false};
 // Window only small metadata and return one bounded cleanup batch. Oversized
 // payloads do not consume the remaining byte budget for smaller recent results.
 const rows=(await env.DB.prepare("WITH candidates AS (SELECT id,state,result_digest,result_bytes,completed FROM maintenance_jobs INDEXED BY maintenance_future_results WHERE host=? AND retention_version=1 AND result_retired=0 AND state IN ('succeeded','failed') AND result_digest IS NOT NULL AND completed IS NOT NULL ORDER BY completed DESC,id DESC LIMIT ?), ranked AS (SELECT *,ROW_NUMBER() OVER(ORDER BY completed DESC,id DESC) AS ordinal,SUM(CASE WHEN result_bytes<=? THEN result_bytes ELSE 0 END) OVER(ORDER BY completed DESC,id DESC ROWS UNBOUNDED PRECEDING) AS total FROM candidates) SELECT id,state,result_digest FROM ranked WHERE ordinal>? OR result_bytes>? OR total>? OR completed<? ORDER BY completed,id LIMIT 200").bind(host,count+200,bytes,count,bytes,bytes,now-age).all()).results;
 for(const row of rows){
  const tombstone=JSON.stringify({state:row.state,result_retired:true,result_sha256:row.result_digest});
  await env.DB.prepare("UPDATE maintenance_jobs SET result=?,result_bytes=0,result_retired=1 WHERE id=? AND host=? AND retention_version=1 AND result_retired=0 AND result_digest=? AND state IN ('succeeded','failed')").bind(tombstone,row.id,host,row.result_digest).run();
 }
 // Each candidate joins at most one submit audit by its primary key. The byte
 // budget covers both the command (including approval question) and audit copy.
 const requests=(await env.DB.prepare("WITH candidates AS (SELECT id,host,request_digest,request_bytes,approval_digest,result_digest,completed FROM maintenance_jobs INDEXED BY maintenance_future_requests WHERE host=? AND retention_version=1 AND request_retired=0 AND state IN ('succeeded','failed') AND request_digest IS NOT NULL AND approval_digest IS NOT NULL AND request_bytes IS NOT NULL AND result_digest IS NOT NULL AND completed IS NOT NULL AND confirmation IS NULL ORDER BY completed DESC,id DESC LIMIT ?), payloads AS (SELECT j.*,j.request_bytes+COALESCE(a.body_bytes,0) AS payload_bytes FROM candidates j LEFT JOIN maintenance_audit a ON a.id=j.id AND a.host=j.host AND a.action='maintenance.submit' AND a.retention_version=1 AND a.body_retired=0), ranked AS (SELECT *,ROW_NUMBER() OVER(ORDER BY completed DESC,id DESC) AS ordinal,SUM(CASE WHEN payload_bytes<=? THEN payload_bytes ELSE 0 END) OVER(ORDER BY completed DESC,id DESC ROWS UNBOUNDED PRECEDING) AS total FROM payloads) SELECT id,request_digest,result_digest FROM ranked WHERE ordinal>? OR payload_bytes>? OR total>? OR completed<? ORDER BY completed,id LIMIT 200").bind(host,requestCount+200,requestBytes,requestCount,requestBytes,requestBytes,now-requestAge).all()).results;
 for(const row of requests){
  // D1 batches are atomic: interrupted cleanup retains both full copies or both
  // tombstones, never a lost digest or a half-retired unindexed audit payload.
  await env.DB.batch([
   env.DB.prepare("UPDATE maintenance_jobs SET request=?,request_bytes=0,request_retired=1 WHERE id=? AND host=? AND retention_version=1 AND request_retired=0 AND request_digest=? AND result_digest=? AND approval_digest IS NOT NULL AND completed IS NOT NULL AND confirmation IS NULL AND state IN ('succeeded','failed')").bind(JSON.stringify({request_retired:true,request_sha256:row.request_digest}),row.id,host,row.request_digest,row.result_digest),
   env.DB.prepare("UPDATE maintenance_audit SET body=json_object('body_retired',json('true'),'body_sha256',body_digest,'request_sha256',?),body_bytes=0,body_retired=1 WHERE id=? AND host=? AND action='maintenance.submit' AND retention_version=1 AND body_retired=0 AND body_digest IS NOT NULL AND EXISTS(SELECT 1 FROM maintenance_jobs WHERE id=? AND host=? AND retention_version=1 AND request_retired=1 AND request_digest=? AND result_digest=? AND confirmation IS NULL AND state IN ('succeeded','failed'))").bind(row.request_digest,row.id,host,row.id,host,row.request_digest,row.result_digest),
  ]);
 }
 const pending=rows.length===200||requests.length===200;
 if(pending)await env.DB.prepare('UPDATE maintenance_hosts SET retention_checked=? WHERE id=?').bind(now-60,host).run();
 return {checked:true,retired:rows.length,requests_retired:requests.length,pending};
}

// Historical enrollment is explicit and content-fenced. Ordinary admission and
// polling still cannot silently mark an old payload eligible.
async function maintenanceHistoryRead(env,host,id,now,budget={remaining:8*1024*1024}){
 const size=await env.DB.prepare('SELECT length(CAST(j.request AS BLOB))+COALESCE(length(CAST(j.result AS BLOB)),0)+COALESCE(length(CAST(a.body AS BLOB)),0) AS bytes FROM maintenance_jobs j LEFT JOIN maintenance_audit a ON a.id=j.id WHERE j.host=? AND j.id=?').bind(host,id).first();
 if(!size)throw Error('Unknown maintenance command');
 if(size.bytes>budget.remaining)throw Error('Historical payload byte budget exceeded; qualify a smaller batch');
 budget.remaining-=size.bytes;
 const row=await env.DB.prepare('SELECT * FROM maintenance_jobs WHERE host=? AND id=?').bind(host,id).first();
 if(!row)throw Error('Unknown maintenance command');
 if(!['succeeded','failed'].includes(row.state)||row.confirmation!==null)throw Error('Active uncertain or authority-dependent command');
 const completed=row.completed??row.updated;
 if(!Number.isFinite(row.created)||row.created<0||!Number.isFinite(completed)||completed<row.created||completed>now)throw Error('Original completion time is not trustworthy');
 let request,requestDigest,approvalDigest,executionDigest;
 if(row.request_retired===1){
  if(row.retention_version!==1||!row.request_digest||!row.approval_digest||!row.execution_digest)throw Error('Original execution identity is unavailable');
  requestDigest=row.request_digest;approvalDigest=row.approval_digest;executionDigest=row.execution_digest;
 }else{
  request=JSON.parse(row.request);
  if(!Array.isArray(request.argv)||!request.argv.length||request.argv.some(v=>typeof v!=='string')||request.approval?.id!=='maintenance:'+id||request.approval?.proposal_revision!==1)throw Error('Original request identity is invalid');
  requestDigest=await hexHash(row.request);approvalDigest=await hexHash(maintenanceCanonical(request.approval));executionDigest=await hexHash(maintenanceCanonical({argv:request.argv,cwd:request.cwd??null}));
  if((row.request_digest&&row.request_digest!==requestDigest)||(row.approval_digest&&row.approval_digest!==approvalDigest)||(row.execution_digest&&row.execution_digest!==executionDigest))throw Error('Request digest conflict');
 }
 const result=JSON.parse(row.result);
 let resultDigest;
 if(row.result_retired===1){
  if(row.retention_version!==1||result.result_retired!==true||result.result_sha256!==row.result_digest||result.state!==row.state)throw Error('Result fence is invalid');
  resultDigest=row.result_digest;
 }else{
  if(result.state!==row.state||result.result_retired===true)throw Error('Terminal result is not intact');
  resultDigest=await hexHash(maintenanceCanonical(result));
  if(row.result_digest&&row.result_digest!==resultDigest)throw Error('Result digest conflict');
 }
 const audit=await env.DB.prepare('SELECT * FROM maintenance_audit WHERE id=?').bind(id).first();
 let auditDigest=null;
 if(audit?.action==='maintenance.submit'){
  if(audit.host!==host)throw Error('Submit audit host conflicts');
  if(audit.body_retired===1){if(audit.retention_version!==1||!audit.body_digest)throw Error('Submit audit fence is invalid');auditDigest=audit.body_digest}
  else{
   const body=JSON.parse(audit.body);
   if(body.host!==host||await hexHash(maintenanceCanonical({argv:body.argv,cwd:body.cwd??null}))!==executionDigest)throw Error('Submit audit command conflicts');
   auditDigest=await hexHash(audit.body);
   if(audit.body_digest&&audit.body_digest!==auditDigest)throw Error('Submit audit digest conflict');
  }
 }
 const record={id,host,created:row.created,completed,state:row.state,request_sha256:requestDigest,execution_sha256:executionDigest,approval_sha256:approvalDigest,result_sha256:resultDigest,audit_sha256:auditDigest,preimage_sha256:await hexHash(maintenanceCanonical({row,audit}))};
 return {row,audit,record};
}

export async function maintenanceHistoryPlan(env,{host,after='',limit=64}={},now=Date.now()/1000){
 if(typeof host!=='string'||!host||typeof after!=='string'||!Number.isSafeInteger(limit)||limit<1||limit>128||!Number.isFinite(now))throw Error('Invalid historical retention range');
 // Keyset pagination reads at most 128 identities. Full payloads are read one
 // row at a time, with an explicit 8 MiB aggregate qualification budget.
 const candidates=(await env.DB.prepare('SELECT j.id,length(CAST(j.request AS BLOB))+COALESCE(length(CAST(j.result AS BLOB)),0)+COALESCE(length(CAST(a.body AS BLOB)),0) AS bytes FROM maintenance_jobs j INDEXED BY maintenance_history_host_id LEFT JOIN maintenance_audit a ON a.id=j.id WHERE j.host=? AND j.id>? ORDER BY j.id LIMIT ?').bind(host,after,limit).all()).results;
 const records=[],blocked=[];let used=0,cursor=after;
 for(const candidate of candidates){
  if(candidate.bytes>8*1024*1024){blocked.push({id:candidate.id,reason:'Single payload exceeds migration byte budget'});cursor=candidate.id;continue}
  if(used+candidate.bytes>8*1024*1024)break
  used+=candidate.bytes;cursor=candidate.id;
  try{const entry=await maintenanceHistoryRead(env,host,candidate.id,now);records.push(entry.record)}catch(error){blocked.push({id:candidate.id,reason:error.message})}
 }
 const plan={version:1,host,after,next_cursor:cursor,limit,records,blocked,more:candidates.length===limit||cursor!==(candidates.at(-1)?.id??after)};
 return {plan,plan_sha256:await hexHash(maintenanceCanonical(plan))};
}

export async function maintenanceHistoryApply(env,plan,expected,now=Date.now()/1000){
 if(!plan||plan.version!==1||typeof plan.host!=='string'||!Array.isArray(plan.records)||plan.records.length>128||new Set(plan.records.map(r=>r.id)).size!==plan.records.length||await hexHash(maintenanceCanonical(plan))!==expected)throw Error('Historical retention plan digest differs');
 const enrolled=[],already=[],blocked=[],budget={remaining:8*1024*1024};
 for(const wanted of plan.records){
  let entry;
  try{
   entry=await maintenanceHistoryRead(env,plan.host,wanted.id,now,budget);
   const {preimage_sha256:currentPreimage,...currentIdentity}=entry.record,{preimage_sha256:wantedPreimage,...wantedIdentity}=wanted;
   if(maintenanceCanonical(currentIdentity)!==maintenanceCanonical(wantedIdentity))throw Error('Historical retention identity changed');
   if(currentPreimage!==wantedPreimage){
    if(entry.row.retention_version===1&&entry.row.request_digest===wanted.request_sha256&&entry.row.execution_digest===wanted.execution_sha256&&entry.row.result_digest===wanted.result_sha256&&entry.row.completed===wanted.completed&&(!entry.record.audit_sha256||entry.audit.retention_version===1)){already.push(wanted.id);continue}
    throw Error('Historical retention preimage changed');
   }
  }catch(error){blocked.push({id:wanted.id,reason:error.message});continue}
  const {row,audit,record}=entry;
  const auditGuard=audit?" AND EXISTS(SELECT 1 FROM maintenance_audit WHERE id=? AND action=? AND host IS ? AND body IS ? AND created=? AND retention_version IS ? AND body_digest IS ? AND body_retired=?)":" AND NOT EXISTS(SELECT 1 FROM maintenance_audit WHERE id=?)";
  const auditArgs=audit?[audit.id,audit.action,audit.host,audit.body,audit.created,audit.retention_version,audit.body_digest,audit.body_retired]:[row.id];
  const statements=[env.DB.prepare("UPDATE maintenance_jobs SET retention_version=1,request_digest=?,execution_digest=?,approval_digest=?,request_bytes=?,result_digest=?,result_bytes=?,completed=? WHERE id=? AND host=? AND state=? AND request=? AND result=? AND updated=? AND confirmation IS NULL AND retention_version IS ? AND request_digest IS ? AND result_digest IS ? AND request_retired=? AND result_retired=?"+auditGuard).bind(record.request_sha256,record.execution_sha256,record.approval_sha256,row.request_retired?0:new TextEncoder().encode(row.request).length,record.result_sha256,row.result_retired?0:new TextEncoder().encode(row.result).length,record.completed,row.id,row.host,row.state,row.request,row.result,row.updated,row.retention_version,row.request_digest,row.result_digest,row.request_retired,row.result_retired,...auditArgs)];
  if(audit?.action==='maintenance.submit')statements.push(env.DB.prepare("UPDATE maintenance_audit SET retention_version=1,body_digest=?,body_bytes=? WHERE id=? AND host=? AND action='maintenance.submit' AND body=? AND created=? AND body_retired=? AND EXISTS(SELECT 1 FROM maintenance_jobs WHERE id=? AND host=? AND retention_version=1 AND request_digest=? AND result_digest=? AND completed=? AND confirmation IS NULL)").bind(record.audit_sha256,audit.body_retired?0:new TextEncoder().encode(audit.body).length,row.id,row.host,audit.body,audit.created,audit.body_retired,row.id,row.host,record.request_sha256,record.result_sha256,record.completed));
  const result=await env.DB.batch(statements),changes=result[0].meta?.changes??result[0].changes;
  if(changes===1)enrolled.push(row.id);else blocked.push({id:row.id,reason:'Historical retention preimage changed during commit'});
 }
 // Enrollment does not run destructive sweeps here. Existing bounded windows
 // apply on the next authorized maintenance pass, using original completion age.
 return {enrolled,already_enrolled:already,blocked,retirement_pending:enrolled.length>0};
}
