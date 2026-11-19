// Temporary coordination only. Durable application state and signed packages live
// on controllers. No completed-session ledger or permanent shared snapshot.
const sessionError=(message,status=409)=>Object.assign(new Error(message),{status});
const sessionKey=x=>typeof x==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(x);
const sessionDigest=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
const sessionBytes=x=>new TextEncoder().encode(x).length;
async function sessionHash(x){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(x)))].map(n=>n.toString(16).padStart(2,'0')).join('');}
export async function sessionSchema(db){
 for(const q of [
 'CREATE TABLE IF NOT EXISTS coordination_sessions(site TEXT,session TEXT,owner TEXT,closer TEXT,closer_epoch INTEGER DEFAULT 0,closer_until REAL DEFAULT 0,claim_token TEXT,closing_token TEXT,definition TEXT,state TEXT,expires REAL,max_entries INTEGER,max_bytes INTEGER,seq INTEGER DEFAULT 0,hash TEXT DEFAULT \'\',bytes INTEGER DEFAULT 0,PRIMARY KEY(site,session))',
 'CREATE TABLE IF NOT EXISTS coordination_scope(site TEXT,kind TEXT,id TEXT,session TEXT,PRIMARY KEY(site,kind,id))',
 'CREATE TABLE IF NOT EXISTS coordination_entries(site TEXT,session TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,session,seq))',
 'CREATE TABLE IF NOT EXISTS coordination_exports(site TEXT,session TEXT,node TEXT,package_digest TEXT,PRIMARY KEY(site,session,node))',
 'CREATE TABLE IF NOT EXISTS coordination_acks(site TEXT,session TEXT,node TEXT,package_digest TEXT,PRIMARY KEY(site,session,node))',
 'CREATE TABLE IF NOT EXISTS coordination_ttl(site TEXT,session TEXT,node TEXT,key TEXT,value TEXT,expires REAL,PRIMARY KEY(site,session,node,key))',
 "CREATE TRIGGER IF NOT EXISTS coordination_export_proof BEFORE INSERT ON coordination_exports WHEN EXISTS(SELECT 1 FROM coordination_acks WHERE site=NEW.site AND session=NEW.session AND node=NEW.node AND package_digest!=NEW.package_digest) BEGIN SELECT RAISE(ABORT,'Conflicting import proof'); END",
 "CREATE TRIGGER IF NOT EXISTS coordination_import_proof BEFORE INSERT ON coordination_acks WHEN EXISTS(SELECT 1 FROM coordination_exports WHERE site=NEW.site AND session=NEW.session AND node=NEW.node AND package_digest!=NEW.package_digest) BEGIN SELECT RAISE(ABORT,'Conflicting outbox proof'); END"
 ])await db.prepare(q).run();
 const columns=(await db.prepare('PRAGMA table_info(coordination_sessions)').all()).results.map(c=>c.name);
 if(['closer','closer_epoch','closer_until','claim_token','closing_token'].some(name=>!columns.includes(name)))throw sessionError('Incompatible pre-release session schema; recover unresolved sessions before upgrading',503);
}
export async function coordinationSession(db,site,node,operation,d,now=Date.now()/1000,{verifiedParticipants=false,maxActive=8,maxReservedBytes=67108864,maxSeconds=86400,closerLeaseSeconds=60}={}){
 await sessionSchema(db);
 if(!sessionKey(d.session))throw sessionError('Invalid session identity',400);
 const args=[site,d.session];
 if(!Number.isInteger(closerLeaseSeconds)||closerLeaseSeconds<1||closerLeaseSeconds>600)throw sessionError('Invalid configured closer lease',503);
 if(operation==='open'){
  if(!Array.isArray(d.participants)||!d.participants.includes(node)||d.participants.length<2||d.participants.length>32||new Set(d.participants).size!==d.participants.length||!d.participants.every(sessionKey)||!Array.isArray(d.scope)||!d.scope.length||d.scope.length>256||!d.scope.every(r=>Array.isArray(r)&&r.length===2&&['work','revisions','documents','definitions','process_versions','grants'].includes(r[0])&&typeof r[1]==='string'&&r[1].length>0&&r[1].length<=512))throw sessionError('Explicit participants and scope required',400);
  if(!Array.isArray(d.authorized_closers)||!d.authorized_closers.includes(node)||new Set(d.authorized_closers).size!==d.authorized_closers.length||!d.authorized_closers.every(n=>d.participants.includes(n))||d.successor_after!==d.expires)throw sessionError('Explicit authorized closers and expiry successor bound required',400);
  if(!Number.isInteger(maxActive)||maxActive<1||maxActive>64||!Number.isSafeInteger(maxReservedBytes)||maxReservedBytes<1||maxReservedBytes>1073741824||!Number.isInteger(maxSeconds)||maxSeconds<1||maxSeconds>604800)throw sessionError('Invalid configured session admission limits',503);
  if(d.expires-now>maxSeconds||d.max_bytes>maxReservedBytes)throw sessionError('Session exceeds configured site admission limits',413);
  // Caller-selected finite bounds, validated against supported transport limits.
  if(!Number.isFinite(d.expires)||d.expires<=now||!Number.isSafeInteger(d.max_entries)||d.max_entries<1||d.max_entries>100000||!Number.isSafeInteger(d.max_bytes)||d.max_bytes<1||d.max_bytes>1073741824)throw sessionError('Invalid session bounds',400);
  for(const participant of d.participants){if(!verifiedParticipants&&!await db.prepare('SELECT id FROM controller_nodes WHERE id=? AND enabled=1').bind(participant).first())throw sessionError('Unregistered participant',403);}
  const definition=JSON.stringify({participants:[...d.participants].sort(),authorized_closers:[...d.authorized_closers].sort(),successor_after:d.successor_after,scope:[...new Map(d.scope.map(r=>[JSON.stringify(r),r])).values()].sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)))});
  const present=await db.prepare('SELECT session FROM coordination_sessions WHERE site=? AND session=?').bind(...args).first();
  if(!present){
   try{await db.batch([
    db.prepare("INSERT INTO coordination_sessions(site,session,owner,definition,state,expires,max_entries,max_bytes) SELECT ?,?,?,?,'open',?,?,? WHERE (SELECT count(*) FROM coordination_sessions WHERE site=?)<? AND COALESCE((SELECT sum(max_bytes) FROM coordination_sessions WHERE site=?),0)+?<=?").bind(...args,node,definition,d.expires,d.max_entries,d.max_bytes,site,maxActive,site,d.max_bytes,maxReservedBytes),
    ...JSON.parse(definition).scope.map(([kind,id])=>db.prepare('INSERT INTO coordination_scope SELECT site,?,?,session FROM coordination_sessions WHERE site=? AND session=?').bind(kind,id,site,d.session))
   ]);}catch(error){
    if(!await db.prepare('SELECT session FROM coordination_sessions WHERE site=? AND session=?').bind(...args).first())throw sessionError('Scope overlaps an unresolved session');
   }
  }
  const row=await db.prepare('SELECT * FROM coordination_sessions WHERE site=? AND session=?').bind(...args).first();
  if(!row)throw sessionError('Site session admission budget exhausted',413);
  if(row.owner!==node||row.definition!==definition||row.expires!==d.expires||row.max_entries!==d.max_entries||row.max_bytes!==d.max_bytes)throw sessionError('Session identity already in use');
  return row;
 }
 let row=await db.prepare('SELECT * FROM coordination_sessions WHERE site=? AND session=?').bind(...args).first();
 if(!row){if(operation==='close')return {closed:true};throw sessionError('Session unavailable',410);}
 const definition=JSON.parse(row.definition);
 if(!Array.isArray(definition.authorized_closers)||definition.successor_after!==row.expires)throw sessionError('Incompatible pre-release session invitation; recover before upgrading',503);
 if(!definition.participants.includes(node))throw sessionError('Not a session participant',403);
 if(operation==='status')return {...row,participants:definition.participants,scope:definition.scope};
 if(operation==='read'){
  if(!Number.isSafeInteger(d.after)||d.after<0)throw sessionError('Invalid session cursor',400);
  // One bounded entry, not a query materializing many large bodies.
  const entry=await db.prepare('SELECT seq,hash,entry FROM coordination_entries WHERE site=? AND session=? AND seq>? ORDER BY seq LIMIT 1').bind(...args,d.after).first();
  return {state:row.state,closer:row.closer,closer_epoch:row.closer_epoch,closer_until:row.closer_until,final_seq:row.seq,final_hash:row.hash,record:entry??null};
 }
 if(operation==='append'){
  if(typeof d.entry!=='string'||sessionBytes(d.entry)>1048576||sessionBytes(d.entry)+sessionBytes(site)+sessionBytes(d.session)+512>1500000)throw sessionError('Session entry exceeds the 1 MiB inline admission or row metadata budget',413);
  let entry;try{entry=JSON.parse(d.entry)}catch{throw sessionError('Invalid entry JSON',400)}
  if(!Number.isSafeInteger(d.expected)||d.expected<0||entry.seq!==d.expected+1||typeof entry.prev!=='string'||!Array.isArray(entry.changes)||!entry.changes.length||entry.changes.length>4096)throw sessionError('Invalid entry envelope',400);
  const seen=new Set();
  for(const change of entry.changes){
   if(!Array.isArray(change)||change.length!==3||!definition.scope.some(([kind,id])=>kind===change[0]&&id===change[1])||typeof change[1]!=='string'||!change[1]||change[1].length>512||change[2]!==null&&typeof change[2]!=='string')throw sessionError('Invalid scoped opaque change',400);
   const key=JSON.stringify(change.slice(0,2));if(seen.has(key))throw sessionError('Duplicate change',400);seen.add(key);
  }
  const hash=await sessionHash(d.entry),bytes=sessionBytes(d.entry);
  const existing=await db.prepare('SELECT hash FROM coordination_entries WHERE site=? AND session=? AND seq=?').bind(...args,entry.seq).first();
  if(existing?.hash===hash)return {seq:entry.seq,hash};
  if(row.state!=='open'||row.expires<=now)throw sessionError('Session admission closed',410);
  if(row.seq>=row.max_entries||row.bytes+bytes>row.max_bytes)throw sessionError('Session bound reached; close and export',413);
  const results=await db.batch([
   db.prepare("UPDATE coordination_sessions SET seq=?,hash=?,bytes=bytes+? WHERE site=? AND session=? AND seq=? AND hash=? AND state='open' AND expires>? AND seq<max_entries AND bytes+?<=max_bytes").bind(entry.seq,hash,bytes,...args,d.expected,entry.prev,now,bytes),
   db.prepare('INSERT OR IGNORE INTO coordination_entries SELECT site,session,seq,hash,? FROM coordination_sessions WHERE site=? AND session=? AND seq=? AND hash=?').bind(d.entry,...args,entry.seq,hash)
  ]);
  if((results[0].meta?.changes??results[0].changes)!==1)throw sessionError('Session head changed');
  return {seq:entry.seq,hash};
 }
 if(operation==='freeze'){
  if(!definition.authorized_closers.includes(node))throw sessionError('Caller has no finite closing authority',403);
  if(node!==row.owner&&now<definition.successor_after)throw sessionError('Successor closing bound has not arrived');
  if(d.final_seq!==row.seq||d.final_hash!==row.hash)throw sessionError('Freeze head mismatch');
  if(row.state==='frozen'&&row.closer===node&&row.closer_until>now){
   await db.prepare("UPDATE coordination_sessions SET closer_until=? WHERE site=? AND session=? AND state='frozen' AND closer=? AND closer_epoch=? AND closer_until>?").bind(now+closerLeaseSeconds,...args,node,row.closer_epoch,now).run();
  }else{
   if(!Number.isSafeInteger(row.closer_epoch)||row.closer_epoch>=Number.MAX_SAFE_INTEGER)throw sessionError('Closer epoch unavailable');
   const token=crypto.randomUUID(),next=row.closer_epoch+1;
   await db.batch([
    db.prepare("UPDATE coordination_sessions SET state='frozen',closer=?,closer_epoch=?,closer_until=?,claim_token=? WHERE site=? AND session=? AND seq=? AND hash=? AND closer_epoch=? AND (state='open' OR (state='frozen' AND closer_until<=?))").bind(node,next,now+closerLeaseSeconds,token,...args,d.final_seq,d.final_hash,row.closer_epoch,now),
    ...['coordination_acks','coordination_exports'].map(table=>db.prepare(`DELETE FROM ${table} WHERE site=? AND session=? AND EXISTS(SELECT 1 FROM coordination_sessions WHERE site=? AND session=? AND claim_token=?)`).bind(...args,...args,token))
   ]);
  }
  row=await db.prepare('SELECT state,closer,closer_epoch,closer_until,seq,hash FROM coordination_sessions WHERE site=? AND session=?').bind(...args).first();
  if(!row||row.state!=='frozen'||row.closer!==node||row.closer_until<=now||row.seq!==d.final_seq||row.hash!==d.final_hash)throw sessionError('Another closer or head won; reconcile session status');
  return {state:'frozen',closer:row.closer,closer_epoch:row.closer_epoch,closer_until:row.closer_until,final_seq:row.seq,final_hash:row.hash};
 }
 if(operation==='outbox'){
  if(!definition.authorized_closers.includes(node))throw sessionError('Caller has no finite closing authority',403);
  if(node!==row.closer)throw sessionError('Closer claim belongs to another authorized participant');
  if(d.closer_epoch!==row.closer_epoch||row.closer_until<=now)throw sessionError('Closer claim expired or changed');
  if(row.state!=='frozen'||d.final_seq!==row.seq||d.final_hash!==row.hash||d.durable!==true||!Array.isArray(d.packages)||d.packages.length!==definition.participants.length||new Set(d.packages.map(p=>p?.to)).size!==definition.participants.length||!d.packages.every(p=>definition.participants.includes(p?.to)&&sessionDigest(p.package_digest)))throw sessionError('Complete durable signed-package outbox attestation required',400);
  const acks=(await db.prepare('SELECT node,package_digest FROM coordination_acks WHERE site=? AND session=?').bind(...args).all()).results;
  const prior=(await db.prepare('SELECT node,package_digest FROM coordination_exports WHERE site=? AND session=?').bind(...args).all()).results;
  if([...acks,...prior].some(p=>!d.packages.some(q=>q.to===p.node&&q.package_digest===p.package_digest)))throw sessionError('Package proof conflicts');
  const closingToken=crypto.randomUUID();
  const gate=db.prepare("UPDATE coordination_sessions SET state='closing',closing_token=? WHERE site=? AND session=? AND state='frozen' AND closer=? AND closer_epoch=? AND closer_until>? AND seq=? AND hash=?").bind(closingToken,...args,node,d.closer_epoch,now,d.final_seq,d.final_hash);
  const writes=d.packages.map(p=>db.prepare("INSERT OR IGNORE INTO coordination_exports SELECT site,session,?,? FROM coordination_sessions WHERE site=? AND session=? AND state='closing' AND closing_token=?").bind(p.to,p.package_digest,...args,closingToken));
  // Successful attestation and deletion are one transaction: a crashed closer
  // cannot strand a frozen shared log after its durable local outbox is ready.
  const outcome=await db.batch([gate,...writes,...sessionDeleteStatements(db,args,closingToken)]);
  if((outcome[0].meta?.changes??outcome[0].changes)!==1)throw sessionError('Closer claim changed before outbox acceptance');
  const imported=definition.participants.filter(p=>acks.some(a=>a.node===p));
  return {closed:true,durable_outbox_attested:true,exported_by:node,imported,pending_delivery:definition.participants.filter(p=>!imported.includes(p))};

 }
 if(operation==='ack'){
  if(row.state!=='frozen'||d.closer_epoch!==row.closer_epoch||d.final_seq!==row.seq||d.final_hash!==row.hash||!sessionDigest(d.package_digest))throw sessionError('Invalid frozen import proof');
  const exported=await db.prepare('SELECT package_digest FROM coordination_exports WHERE site=? AND session=? AND node=?').bind(...args,node).first();
  if(exported&&exported.package_digest!==d.package_digest)throw sessionError('Package proof conflicts');
  // Authenticated recipient attests signature/baseline verification and durable
  // local import. Gateway cannot itself verify the receiver's local database.
  await db.prepare("INSERT OR IGNORE INTO coordination_acks SELECT site,session,?,? FROM coordination_sessions WHERE site=? AND session=? AND state='frozen' AND closer_epoch=? AND seq=? AND hash=?").bind(node,d.package_digest,...args,d.closer_epoch,d.final_seq,d.final_hash).run();
  const prior=await db.prepare('SELECT package_digest FROM coordination_acks WHERE site=? AND session=? AND node=?').bind(...args,node).first();
  if(!prior)throw sessionError('Session unavailable',410);
  if(prior.package_digest!==d.package_digest)throw sessionError('Conflicting import proof');
  return {acknowledged:true};
 }
 if(operation==='close'){
  if(node!==row.closer)throw sessionError('Only the selected closer may delete this session',403);
  if(d.closer_epoch!==row.closer_epoch||row.closer_until<=now)throw sessionError('Closer claim expired or changed');
  const acks=(await db.prepare('SELECT node,package_digest FROM coordination_acks WHERE site=? AND session=?').bind(...args).all()).results;
  const exports=(await db.prepare('SELECT node,package_digest FROM coordination_exports WHERE site=? AND session=?').bind(...args).all()).results;
  const imported=definition.participants.filter(p=>acks.some(a=>a.node===p));
  const pending_delivery=definition.participants.filter(p=>!imported.includes(p));
  if(row.state!=='frozen'||pending_delivery.length&&!definition.participants.every(p=>exports.some(e=>e.node===p)))throw sessionError('Durable signed local outbox or verified distribution required');
  if(acks.some(a=>exports.some(e=>e.node===a.node&&e.package_digest!==a.package_digest)))throw sessionError('Package proof conflicts');
  // Frozen membership/head and immutable acknowledgments make this safe across
  // retries. All session payloads are inline SQL, so deletion has no R2 tail.
  const closingToken=crypto.randomUUID();
  const gate=db.prepare("UPDATE coordination_sessions SET state='closing',closing_token=? WHERE site=? AND session=? AND state='frozen' AND closer=? AND closer_epoch=? AND closer_until>?").bind(closingToken,...args,node,d.closer_epoch,now);
  const outcome=await db.batch([gate,...sessionDeleteStatements(db,args,closingToken)]);
  if((outcome[0].meta?.changes??outcome[0].changes)!==1)throw sessionError('Closer claim changed before deletion');
  return {closed:true,imported,pending_delivery,exported_by:exports.length?row.closer:null};
 }
 if(operation==='ttl/read'){
  if(!sessionKey(d.key)||!definition.participants.includes(d.owner??node))throw sessionError('Invalid TTL read',400);
  const value=await db.prepare('SELECT value,expires FROM coordination_ttl WHERE site=? AND session=? AND node=? AND key=? AND expires>?').bind(...args,d.owner??node,d.key,now).first();
  return {record:value??null};
 }
 if(operation==='ttl'){
  if(row.state!=='open'||row.expires<=now)throw sessionError('Session admission closed',410);
  if(!sessionKey(d.key)||typeof d.value!=='string'||sessionBytes(d.value)>4096||!Number.isFinite(d.expires)||d.expires<=now||d.expires>row.expires)throw sessionError('Invalid TTL value',400);
  await db.prepare('DELETE FROM coordination_ttl WHERE site=? AND expires<=?').bind(site,now).run();
  const result=await db.prepare("INSERT INTO coordination_ttl SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM coordination_sessions WHERE site=? AND session=? AND state='open' AND expires>?) AND ((SELECT count(*) FROM coordination_ttl WHERE site=? AND session=? AND node=?)<128 OR EXISTS(SELECT 1 FROM coordination_ttl WHERE site=? AND session=? AND node=? AND key=?)) ON CONFLICT(site,session,node,key) DO UPDATE SET value=excluded.value,expires=excluded.expires").bind(...args,node,d.key,d.value,d.expires,...args,now,...args,node,...args,node,d.key).run();
  if(!(result.meta?.changes??result.changes))throw sessionError('TTL admission unavailable',413);
  return {stored:true};
 }
 throw sessionError('Unknown session operation',404);
}

function sessionDeleteStatements(db,args,token){
 return [...['coordination_entries','coordination_ttl','coordination_acks','coordination_exports','coordination_scope'].map(table=>db.prepare(`DELETE FROM ${table} WHERE site=? AND session=? AND EXISTS(SELECT 1 FROM coordination_sessions WHERE site=? AND session=? AND closing_token=?)`).bind(...args,...args,token)),db.prepare('DELETE FROM coordination_sessions WHERE site=? AND session=? AND closing_token=?').bind(...args,token)];
}
