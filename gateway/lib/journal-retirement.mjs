import {collectJournalUploads} from './journal-upload.mjs';
// One unresolved, explicitly enabled retirement. No completed-history marker.
const retirementError=(message,status=409)=>Object.assign(new Error(message),{status});
const retirementTables=['checkpoint_readers','checkpoint_builds','checkpoint_edges','controller_checkpoints','controller_journal','journal_uploads','journal_upload_sweeps','checkpoint_objects','checkpoint_progress'];
const retirementPrefixes=site=>['controller-journal/','controller-journal-managed-v1/','controller-checkpoint-v2/'].map(p=>p+encodeURIComponent(site)+'/');
const retirementHash=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
async function retirementSchema(db){
 for(const statement of [
  "CREATE TABLE IF NOT EXISTS journal_retirement(site TEXT PRIMARY KEY,source_seq INTEGER,source_hash TEXT,controllers TEXT,mapping TEXT,database_started INTEGER DEFAULT 0,phase TEXT DEFAULT 'objects',position INTEGER DEFAULT 0)",
  'CREATE TABLE IF NOT EXISTS journal_retirement_adoptions(site TEXT,node TEXT,manifest_digest TEXT,PRIMARY KEY(site,node))',
  "CREATE TRIGGER IF NOT EXISTS journal_retirement_insert_guard BEFORE INSERT ON controller_journal WHEN EXISTS(SELECT 1 FROM journal_retirement WHERE site=NEW.site) BEGIN SELECT RAISE(ABORT,'Legacy journal retirement is in progress'); END",
  "CREATE TRIGGER IF NOT EXISTS journal_retirement_update_guard BEFORE UPDATE ON controller_journal WHEN EXISTS(SELECT 1 FROM journal_retirement WHERE site=NEW.site) BEGIN SELECT RAISE(ABORT,'Legacy journal retirement is in progress'); END"
 ])await db.prepare(statement).run();
}
async function retirementHasTable(db,table){return !!await db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").bind(table).first();}
async function retirementQuiet(db,site){
 const checks=[['checkpoint_readers','expires>unixepoch()'],['checkpoint_builds','until>unixepoch()'],['journal_uploads',"state IN ('pending','collecting')"],['journal_upload_sweeps','until>unixepoch()']];
 for(const [table,condition] of checks)if(await retirementHasTable(db,table)&&await db.prepare(`SELECT 1 AS present FROM ${table} WHERE site=? AND ${condition} LIMIT 1`).bind(site).first())throw retirementError('Legacy activity still unresolved: '+table);
}
async function retirementHead(db,site){return await db.prepare('SELECT seq,hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1').bind(site).first()??{seq:0,hash:''};}
function retirementObjectValid(key,prefix,index){
 if(typeof key!=='string'||!key.startsWith(prefix))return false;
 const suffix=key.slice(prefix.length),uuid='[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}';
 return index===1?new RegExp('^'+uuid+'/[a-f0-9]{64}/'+uuid+'$').test(suffix):/^[a-f0-9]{64}$/.test(suffix);
}
async function retirementPage(archive,prefix,index,limit){
 const page=await archive.list({prefix,limit,include:[]});
 if(!Array.isArray(page.objects)||page.objects.length>limit||typeof page.truncated!=='boolean'||page.objects.some(o=>!retirementObjectValid(o.key,prefix,index)))throw retirementError('Unrecognized object or invalid retirement listing; no objects from this page deleted');
 if(page.truncated&&!page.objects.length)throw retirementError('Retirement listing cannot progress');
 return page;
}
export async function retireLegacyJournal(env,site,node,operation,d){
 if(env.CONTROLLER_COORDINATION_MODE!=='local'||env.LEGACY_RETIREMENT_ENABLED!=='1')throw retirementError('Explicit local-mode retirement enablement required',403);
 if(!env.JOURNAL_ARCHIVE||typeof env.JOURNAL_ARCHIVE.list!=='function'||typeof env.JOURNAL_ARCHIVE.delete!=='function')throw retirementError('Bounded archive listing and deletion binding required',503);
 const db=env.DB;
 let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{throw retirementError('Invalid controller mapping',503)}
 const controllers=Object.keys(mapping??{}).sort();
 if(controllers.length<2||!controllers.every(retirementHash)||new Set(Object.values(mapping)).size!==controllers.length)throw retirementError('At least two uniquely mapped application controllers required');
 for(const identity of controllers)if(!await db.prepare('SELECT id FROM controller_nodes WHERE id=? AND enabled=1').bind(mapping[identity]).first())throw retirementError('Mapped controller is not enabled');
 const identities=controllers.filter(identity=>mapping[identity]===node.id);
 if(identities.length!==1)throw retirementError('Retirement caller is not a mapped controller',403);
 const identity=identities[0],mappingProof=JSON.stringify(Object.fromEntries(controllers.map(id=>[id,mapping[id]])));
 await retirementSchema(db);
 // Collect expired uncommitted uploads through the existing fenced collector.
 // This does not relax begin/adoption/deletion's quiet checks or restore writes.
 if(operation==='drain'){
  const limit=d.limit??32;
  if(!Number.isInteger(limit)||limit<1||limit>32)throw retirementError('Invalid retirement page limit',400);
  const uploads=await collectJournalUploads(env,site,limit);
  const pending=await retirementHasTable(db,'journal_uploads')?(await db.prepare("SELECT count(*) AS n FROM journal_uploads WHERE site=? AND state IN ('pending','collecting')").bind(site).first()).n:0;
  return {uploads,pending};
 }
 if(operation==='begin'){
  if(!Number.isSafeInteger(d.source_seq)||d.source_seq<0||!(d.source_seq===0&&d.source_hash===''||retirementHash(d.source_hash)))throw retirementError('Exact source head required',400);
  await retirementQuiet(db,site);
  const head=await retirementHead(db,site);if(head.seq!==d.source_seq||head.hash!==d.source_hash)throw retirementError('Source head changed');
  await db.prepare("INSERT OR IGNORE INTO journal_retirement(site,source_seq,source_hash,controllers,mapping) SELECT ?,?,?,?,? WHERE COALESCE((SELECT MAX(seq) FROM controller_journal WHERE site=?),0)=? AND COALESCE((SELECT hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1),'')=?").bind(site,d.source_seq,d.source_hash,JSON.stringify(controllers),mappingProof,site,d.source_seq,site,d.source_hash).run();
 }
 const row=await db.prepare('SELECT * FROM journal_retirement WHERE site=?').bind(site).first();
 if(!row){if(operation==='status'||operation==='step')return {active:false};throw retirementError('No unresolved retirement');}
 if(row.controllers!==JSON.stringify(controllers)||row.mapping!==mappingProof)throw retirementError('Controller mapping changed during retirement');
 if(operation==='begin'&&(row.source_seq!==d.source_seq||row.source_hash!==d.source_hash))throw retirementError('Retirement identity differs');
 if(operation==='adopt'){
  if(d.source_seq!==row.source_seq||d.source_hash!==row.source_hash||!retirementHash(d.manifest_digest)||d.standalone_verified!==true||d.old_activity_drained!==true)throw retirementError('Verified standalone adoption and drained old activity attestation required',400);
  await retirementQuiet(db,site);
  await db.prepare('INSERT OR IGNORE INTO journal_retirement_adoptions SELECT site,?,? FROM journal_retirement WHERE site=? AND source_seq=? AND source_hash=?').bind(identity,d.manifest_digest,site,row.source_seq,row.source_hash).run();
  const saved=await db.prepare('SELECT manifest_digest FROM journal_retirement_adoptions WHERE site=? AND node=?').bind(site,identity).first();
  if(!saved||saved.manifest_digest!==d.manifest_digest)throw retirementError('Conflicting adoption proof');
 }
 const adopted=(await db.prepare('SELECT node,manifest_digest FROM journal_retirement_adoptions WHERE site=? ORDER BY node').bind(site).all()).results;
 if(['begin','status','adopt'].includes(operation))return {active:true,...row,adopted};
 if(operation!=='step')throw retirementError('Unknown retirement operation',404);
 if(!controllers.every(node=>adopted.some(a=>a.node===node)))throw retirementError('Every mapped controller must verify standalone adoption');
 await retirementQuiet(db,site);
 const prefixes=retirementPrefixes(site),limit=d.limit??32;
 if(!Number.isInteger(limit)||limit<1||limit>1000)throw retirementError('Invalid retirement page limit',400);
 if(row.phase==='objects'){
  const head=await retirementHead(db,site);if(!row.database_started&&(head.seq!==row.source_seq||head.hash!==row.source_hash)||row.database_started&&head.seq!==0)throw retirementError('Frozen source head changed');
  const page=await retirementPage(env.JOURNAL_ARCHIVE,prefixes[row.position],row.position,limit);
  if(page.objects.length)await env.JOURNAL_ARCHIVE.delete(page.objects.map(o=>o.key));
  if(!page.truncated)await db.prepare("UPDATE journal_retirement SET phase=?,position=?,database_started=CASE WHEN ?=2 THEN 1 ELSE database_started END WHERE site=? AND phase='objects' AND position=?").bind(row.position===2?'database':'objects',row.position===2?0:row.position+1,row.position,site,row.position).run();
  return {active:true,removed_objects:page.objects.length};
 }
 if(row.phase==='database'&&row.position<retirementTables.length){
  const table=retirementTables[row.position];let removed=0;
  if(await retirementHasTable(db,table)){
   const result=await db.prepare(`DELETE FROM ${table} WHERE site=? AND rowid IN (SELECT rowid FROM ${table} WHERE site=? LIMIT ?)`).bind(site,site,limit).run();removed=result.meta?.changes??result.changes;
  }
  if(!removed)await db.prepare("UPDATE journal_retirement SET position=position+1 WHERE site=? AND phase='database' AND position=?").bind(site,row.position).run();
  return {active:true,table,removed_rows:removed};
 }
 // Fresh emptiness checks catch late objects; no completed marker is retained.
 for(let index=0;index<prefixes.length;index++)if((await retirementPage(env.JOURNAL_ARCHIVE,prefixes[index],index,limit)).objects.length){await db.prepare("UPDATE journal_retirement SET phase='objects',position=? WHERE site=?").bind(index,site).run();throw retirementError('Late archive object detected; drain old activity before resuming');}
 for(const table of retirementTables)if(await retirementHasTable(db,table)&&await db.prepare(`SELECT 1 AS present FROM ${table} WHERE site=? LIMIT 1`).bind(site).first())throw retirementError('Retirement database verification failed: '+table);
 await db.batch([db.prepare('DELETE FROM journal_retirement_adoptions WHERE site=?').bind(site),db.prepare('DELETE FROM journal_retirement WHERE site=?').bind(site)]);
 return {active:false,completed:true};
}
