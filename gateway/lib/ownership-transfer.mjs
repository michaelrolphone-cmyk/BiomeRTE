// Current authority/routing metadata and finite, incomplete transfers only.
// Controllers verify signatures, snapshot closure and local durable state. The
// registered transport attests those checks; this registry grants no scopes.
const failure=(message,status=409)=>Object.assign(new Error(message),{status});
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const key=x=>typeof x==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(x);
const digest=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
const compare=(a,b)=>{const aa=Array.from(a),bb=Array.from(b);for(let i=0;i<Math.min(aa.length,bb.length);i++){const d=aa[i].codePointAt(0)-bb[i].codePointAt(0);if(d)return d;}return aa.length-bb.length;};
const bytes=x=>new TextEncoder().encode(x).length;
const changes=result=>Number(result.meta?.changes??result.changes??0);
const bindingFields=['unit','epoch','owner','target','transfer','authority_digest','scope','aliases','snapshot_digest','authorization_digest','expires'];
const bounds={maxUnits:4096,maxActive:8,maxAliases:128,maxScope:256,maxBytes:65536,maxSeconds:86400};

// One-time cutover adoption gate, not ongoing device placement. Deployment
// removes this manifest after all exact adoptions have been reconciled. Neither
// an incoming approval boolean nor the mere presence of a provider key opens it.
export async function ownershipBootstrapDigest(value){
 const canonical=x=>Array.isArray(x)?x.map(canonical):object(x)?Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
 const raw=JSON.stringify(canonical(value)).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
 return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(raw))),b=>b.toString(16).padStart(2,'0')).join('');
}
export async function ownershipBootstrapAuthorized(env,data){
 if(env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS===undefined)return false;
 let authorized;
 try{authorized=JSON.parse(env.OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS)}catch{throw failure('Invalid temporary ownership adoption manifest',503)}
 if(!object(authorized)||Object.keys(authorized).length>4096||Object.entries(authorized).some(([unit,hash])=>!key(unit)||!digest(hash)))throw failure('Invalid temporary ownership adoption manifest',503);
 return object(data)&&Object.hasOwn(authorized,data.unit)&&authorized[data.unit]===await ownershipBootstrapDigest(data);
}

function limits(options){
 const out={...bounds,...options};
 for(const [name,max] of Object.entries({maxUnits:65536,maxActive:64,maxAliases:256,maxScope:256,maxBytes:131072,maxSeconds:604800}))
  if(!Number.isSafeInteger(out[name])||out[name]<1||out[name]>max)throw failure('Invalid configured ownership admission limits',503);
 return out;
}
function fields(data,allowed){
 if(!object(data)||Object.keys(data).some(name=>!allowed.includes(name)))throw failure('Invalid ownership request fields',400);
}
function aliases(value,limit){
 if(!Array.isArray(value)||value.length>limit||value.some(x=>typeof x!=='string'||!x||bytes(x)>512||/[\u0000-\u001f\u007f]/.test(x))||new Set(value).size!==value.length)
  throw failure('Invalid bounded route aliases',400);
 return [...value].sort(compare);
}
function scope(value,limit){
 if(!Array.isArray(value)||value.length<1||value.length>limit||value.some(x=>!Array.isArray(x)||x.length!==2||!key(x[0])||typeof x[1]!=='string'||!x[1]||bytes(x[1])>512||/[\u0000-\u001f\u007f]/.test(x[1])))
  throw failure('Invalid bounded transfer scope',400);
 const encoded=value.map(x=>JSON.stringify(x));
 if(new Set(encoded).size!==encoded.length)throw failure('Duplicate transfer scope',400);
 return value.map(x=>[...x]).sort((a,b)=>compare(a[0],b[0])||compare(a[1],b[1]));
}
function binding(data,limit){
 if(!key(data.unit)||!key(data.transfer)||!Number.isSafeInteger(data.epoch)||data.epoch<1||data.epoch>=Number.MAX_SAFE_INTEGER||!digest(data.owner)||!digest(data.target)||data.owner===data.target||!digest(data.authority_digest)||!digest(data.snapshot_digest)||!digest(data.authorization_digest)||!Number.isFinite(data.expires)||data.expires<=0)
  throw failure('Invalid ownership transfer binding',400);
 const out={unit:data.unit,epoch:data.epoch,owner:data.owner,target:data.target,transfer:data.transfer,authority_digest:data.authority_digest,scope:scope(data.scope,limit.maxScope),aliases:aliases(data.aliases,limit.maxAliases),snapshot_digest:data.snapshot_digest,authorization_digest:data.authorization_digest,expires:data.expires};
 if(bytes(JSON.stringify(out))>limit.maxBytes)throw failure('Ownership metadata byte limit exceeded',413);
 return out;
}
async function identity(env,node){
 // node comes only from the existing authenticated registered-controller path.
 if(!node||!key(node.id))throw failure('Authenticated registered controller required',403);
 let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{throw failure('Invalid controller routing configuration',503)}
 if(!object(mapping)||Object.entries(mapping).some(([app,transport])=>!digest(app)||!key(transport))||new Set(Object.values(mapping)).size!==Object.keys(mapping).length)
  throw failure('Controller mapping must be one-to-one',503);
 const app=Object.keys(mapping).find(app=>mapping[app]===node.id);
 if(!app||!await env.DB.prepare('SELECT id FROM controller_nodes WHERE id=? AND enabled=1').bind(node.id).first())throw failure('Transport has no unique registered application identity',403);
 return {app,mapping};
}
async function recipient(env,mapping,app){
 const transport=mapping[app];
 if(!transport||!await env.DB.prepare('SELECT id FROM controller_nodes WHERE id=? AND enabled=1').bind(transport).first())throw failure('Target application is not registered',403);
}

export async function ownershipSchema(db){
 for(const query of [
  'CREATE TABLE IF NOT EXISTS ownership_current(site TEXT,unit TEXT,owner TEXT,epoch INTEGER,authority_digest TEXT,PRIMARY KEY(site,unit))',
  'CREATE TABLE IF NOT EXISTS ownership_routes(site TEXT,alias TEXT,unit TEXT,PRIMARY KEY(site,alias))',
  'CREATE INDEX IF NOT EXISTS ownership_routes_unit ON ownership_routes(site,unit)',
  'CREATE TABLE IF NOT EXISTS ownership_transfers(site TEXT,unit TEXT,transfer TEXT,binding TEXT,expires REAL,bytes INTEGER,state TEXT,source_ack INTEGER DEFAULT 0,target_ack INTEGER DEFAULT 0,PRIMARY KEY(site,unit),UNIQUE(site,transfer))',
  'CREATE TABLE IF NOT EXISTS ownership_transfer_scope(site TEXT,kind TEXT,id TEXT,unit TEXT,transfer TEXT,PRIMARY KEY(site,kind,id))',
  "CREATE TRIGGER IF NOT EXISTS ownership_scope_no_overlap BEFORE INSERT ON ownership_transfer_scope WHEN EXISTS(SELECT 1 FROM ownership_transfer_scope WHERE site=NEW.site AND kind=NEW.kind AND id=NEW.id AND (unit!=NEW.unit OR transfer!=NEW.transfer)) BEGIN SELECT RAISE(ABORT,'Unresolved ownership scopes overlap'); END",
 ])await db.prepare(query).run();
 // An old prototype must never silently reinterpret unresolved authority.
 for(const [table,expected] of Object.entries({ownership_current:['site','unit','owner','epoch','authority_digest'],ownership_routes:['site','alias','unit'],ownership_transfers:['site','unit','transfer','binding','expires','bytes','state','source_ack','target_ack'],ownership_transfer_scope:['site','kind','id','unit','transfer']})){
  const columns=(await db.prepare(`PRAGMA table_info(${table})`).all()).results.map(x=>x.name);
  if(columns.length!==expected.length||expected.some(x=>!columns.includes(x)))throw failure('Incompatible ownership schema; reconcile before upgrading',503);
 }
}
async function current(db,site,unit){
 const row=await db.prepare('SELECT unit,owner,epoch,authority_digest FROM ownership_current WHERE site=? AND unit=?').bind(site,unit).first();
 if(!row)return null;
 const rows=(await db.prepare('SELECT alias FROM ownership_routes WHERE site=? AND unit=? ORDER BY alias').bind(site,unit).all()).results;
 return {...row,aliases:rows.map(x=>x.alias).sort(compare)};
}
function receipt(row,owner){
 return {...JSON.parse(row.binding),state:row.state,current_owner:owner.owner,current_epoch:owner.epoch,source_ack:!!row.source_ack,target_ack:!!row.target_ack};
}
function sameCurrent(row,proof){
 return row.owner===proof.owner&&row.epoch===proof.epoch&&row.authority_digest===proof.authority_digest&&JSON.stringify(row.aliases)===JSON.stringify(proof.aliases);
}
function validCurrent(row,proof,state){
 return sameCurrent(row,state==='committed'?{...proof,owner:proof.target,epoch:proof.epoch+1}:state==='aborted'?{...proof,epoch:proof.epoch+1}:proof);
}
function admissionTime(proof,now,limit){
 if(proof.expires<=now)throw failure('Transfer expired; ownership remains fenced',410);
 if(proof.expires-now>limit.maxSeconds)throw failure('Transfer exceeds configured duration bound',413);
}

// Routing returns only present authority. Expiry never removes the source fence.
// A committed transfer routes to its recipient even while final ACKs are pending.
export async function resolveOwnershipRoute(db,site,alias){
 await ownershipSchema(db);
 const row=await db.prepare("SELECT c.unit,c.owner,c.epoch,c.authority_digest,CASE WHEN t.state IN ('prepared','ready') THEN 1 ELSE 0 END AS frozen FROM ownership_routes r JOIN ownership_current c ON c.site=r.site AND c.unit=r.unit LEFT JOIN ownership_transfers t ON t.site=c.site AND t.unit=c.unit WHERE r.site=? AND r.alias=?").bind(site,alias).first();
 return row?{...row,frozen:!!row.frozen}:null;
}

export async function ownershipTransfer(env,site,node,operation,data,now=Date.now()/1000,options={}){
 const limit=limits(options),db=env.DB;
 if(!key(site)||!Number.isFinite(now)||now<0)throw failure('Invalid ownership site or clock',400);
 const actor=await identity(env,node);
 const extra={bootstrap:['unit','expected_epoch','owner','authority_digest','aliases','snapshot_digest','local_authority','durable'],status:['unit'],prepare:[...bindingFields,'source_frozen'],ready:[...bindingFields,'verified','staged'],commit:bindingFields,cancel:bindingFields,ack:[...bindingFields,'durable']}[operation];
 if(!extra)throw failure('Unknown ownership operation',404);
 fields(data,extra);
 if(!key(data.unit))throw failure('Invalid ownership unit',400);
 if(bytes(JSON.stringify(data))>limit.maxBytes)throw failure('Ownership metadata byte limit exceeded',413);
 await ownershipSchema(db);
 const args=[site,data.unit];

 if(operation==='bootstrap'){
  // This flag is a trusted calling-context gate, never a submitted field. No
  // discovery, fingerprint, physical attachment or expiry enables adoption.
  if(options.bootstrapAuthorized!==true)throw failure('Explicit bootstrap authorization required',403);
  if(data.expected_epoch!==0||data.owner!==actor.app||!digest(data.authority_digest)||!digest(data.snapshot_digest)||data.local_authority!==true||data.durable!==true)throw failure('Durable current-authority adoption attestation required',400);
  const routes=aliases(data.aliases,limit.maxAliases),previous=await current(db,...args);
  if(previous){
   if(!sameCurrent(previous,{...data,epoch:1,aliases:routes})||await db.prepare('SELECT unit FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first())throw failure('Current ownership already differs from bootstrap');
   // Reconcile current authority only: no discarded bootstrap snapshot proof
   // is represented as a persistent, exactly replayable receipt.
   return {...previous,frozen:false,transfer:null};
  }
  try{await db.batch([
   // An existing unit must hit the UNIQUE constraint even at the admission
   // limit, so concurrent different bootstrap aliases cannot form a union.
   db.prepare('INSERT INTO ownership_current SELECT ?,?,?,1,? WHERE (SELECT count(*) FROM ownership_current WHERE site=?)<? OR EXISTS(SELECT 1 FROM ownership_current WHERE site=? AND unit=?)').bind(...args,actor.app,data.authority_digest,site,limit.maxUnits,...args),
   db.prepare('INSERT INTO ownership_routes SELECT c.site,r.value,c.unit FROM ownership_current c,json_each(?) r WHERE c.site=? AND c.unit=? AND c.owner=? AND c.epoch=1 AND c.authority_digest=?').bind(JSON.stringify(routes),...args,actor.app,data.authority_digest),
  ]);}catch(error){
   // Re-read after a concurrent exact retry, otherwise preserve the conflict.
   const winner=await current(db,...args);
   if(!winner||!sameCurrent(winner,{...data,epoch:1,aliases:routes}))throw failure('Route alias or ownership adoption conflict');
  }
  const adopted=await current(db,...args);
  if(!adopted)throw failure('Current ownership admission budget exhausted',413);
  if(!sameCurrent(adopted,{...data,epoch:1,aliases:routes}))throw failure('Another current owner won bootstrap');
  const pending=await db.prepare('SELECT unit FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
  if(pending)throw failure('Ownership changed during bootstrap; reconcile status');
  return {...adopted,frozen:false,transfer:null};
 }

 if(operation==='status'){
  // One database snapshot: separate current/transfer reads could straddle CAS
  // and incorrectly present the old owner with a committed transfer receipt.
  const row=await db.prepare("SELECT c.unit,c.owner,c.epoch,c.authority_digest,(SELECT json_group_array(alias) FROM (SELECT alias FROM ownership_routes WHERE site=c.site AND unit=c.unit ORDER BY alias)) AS aliases,t.binding,t.state,t.source_ack,t.target_ack FROM ownership_current c LEFT JOIN ownership_transfers t ON t.site=c.site AND t.unit=c.unit WHERE c.site=? AND c.unit=?").bind(...args).first();
  if(!row)throw failure('Ownership unit unavailable',410);
  const owner={unit:row.unit,owner:row.owner,epoch:row.epoch,authority_digest:row.authority_digest,aliases:JSON.parse(row.aliases).sort(compare)};
  return {...owner,frozen:!!row.binding&&['prepared','ready'].includes(row.state),transfer:row.binding?receipt(row,owner):null};
 }
 let owner=await current(db,...args);
 if(!owner)throw failure('Ownership unit unavailable',410);
 let pending=await db.prepare('SELECT * FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
 const proof=binding(data,limit),encoded=JSON.stringify(proof);
 if(actor.app!==proof.owner&&actor.app!==proof.target)throw failure('Not an ownership transfer participant',403);
 if(operation==='prepare'||operation==='cancel'&&!pending){
  if(actor.app!==proof.owner)throw failure('Only the existing owner may attest source freeze',403);
  if(operation==='prepare'&&data.source_frozen!==true)throw failure('Durable source-freeze attestation required',400);
  if(operation==='prepare'){admissionTime(proof,now,limit);await recipient(env,actor.mapping,proof.target);}
  if(pending){
   if(pending.binding!==encoded||!validCurrent(owner,proof,pending.state))throw failure('Another ownership transfer or current epoch is present');
   return receipt(pending,owner);
  }
  if(!sameCurrent(owner,proof))throw failure('Ownership epoch, authority or route aliases changed');
  try{await db.batch([
   db.prepare("INSERT OR IGNORE INTO ownership_transfers(site,unit,transfer,binding,expires,bytes,state) SELECT site,unit,?,?,?,?,'prepared' FROM ownership_current WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=? AND (SELECT count(*) FROM ownership_transfers WHERE site=?)<?").bind(proof.transfer,encoded,proof.expires,bytes(encoded),...args,proof.owner,proof.epoch,proof.authority_digest,site,limit.maxActive),
   // Expand the already bounded stored scope once. Repeating a near-limit
   // binding in hundreds of statements would inflate the D1 request body.
   db.prepare("INSERT OR IGNORE INTO ownership_transfer_scope SELECT t.site,json_extract(s.value,'$[0]'),json_extract(s.value,'$[1]'),t.unit,t.transfer FROM ownership_transfers t,json_each(t.binding,'$.scope') s WHERE t.site=? AND t.unit=? AND t.binding=?").bind(...args,encoded),
  ]);}catch(error){throw failure('Ownership transfer scope or identity overlaps an unresolved transfer')}
  pending=await db.prepare('SELECT * FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
  if(!pending){
   const count=await db.prepare('SELECT count(*) AS n FROM ownership_transfers WHERE site=?').bind(site).first();
   throw failure(count.n>=limit.maxActive?'Incomplete transfer admission budget exhausted':'Ownership or transfer identity changed',count.n>=limit.maxActive?413:409);
  }
  owner=await current(db,...args);
  if(pending.binding!==encoded||!validCurrent(owner,proof,pending.state))throw failure('Ownership transfer raced with another binding');
  if(operation==='prepare')return receipt(pending,owner);
 }
 if(!pending)throw failure('Transfer proof unavailable; reconcile current ownership',410);
 if(pending.binding!==encoded||!validCurrent(owner,proof,pending.state))throw failure('Transfer binding or current ownership does not match');

 if(operation==='ready'){
  if(actor.app!==proof.target)throw failure('Only the recipient may attest verified durable staging',403);
  if(data.verified!==true||data.staged!==true)throw failure('Verified durable snapshot staging required',400);
  admissionTime(proof,now,limit);
  if(pending.state==='committed')throw failure('Transfer already committed; reconcile current ownership');
  await db.prepare("UPDATE ownership_transfers SET state='ready' WHERE site=? AND unit=? AND binding=? AND state='prepared' AND expires>? AND EXISTS(SELECT 1 FROM ownership_current WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=?)").bind(...args,encoded,now,...args,proof.owner,proof.epoch,proof.authority_digest).run();
  pending=await db.prepare('SELECT * FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
  owner=await current(db,...args);
  if(!pending||pending.binding!==encoded||pending.state!=='ready'||!sameCurrent(owner,proof))throw failure('Transfer changed before staging acceptance');
  return receipt(pending,owner);
 }
 if(operation==='commit'){
  // An exact read-only retry can report an already executed commit. It cannot
  // execute a new commit after expiry, and no TTL can authorize a takeover.
  if(pending.state==='committed')return receipt(pending,owner);
  admissionTime(proof,now,limit);
  if(pending.state!=='ready')throw failure('Source freeze and recipient staging must both be attested');
  await recipient(env,actor.mapping,proof.target);
  const results=await db.batch([
   db.prepare("UPDATE ownership_current SET owner=?,epoch=epoch+1 WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=? AND EXISTS(SELECT 1 FROM ownership_transfers WHERE site=? AND unit=? AND binding=? AND state='ready' AND expires>?)").bind(proof.target,...args,proof.owner,proof.epoch,proof.authority_digest,...args,encoded,now),
   db.prepare("UPDATE ownership_transfers SET state='committed' WHERE site=? AND unit=? AND binding=? AND state='ready' AND expires>? AND EXISTS(SELECT 1 FROM ownership_current WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=?)").bind(...args,encoded,now,...args,proof.target,proof.epoch+1,proof.authority_digest),
  ]);
  pending=await db.prepare('SELECT * FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
  owner=await current(db,...args);
  if(!pending||pending.binding!==encoded||pending.state!=='committed'||!validCurrent(owner,proof,'committed'))throw failure('Ownership compare-and-swap lost; reconcile status');
  if(changes(results[0])>1||changes(results[1])>1)throw failure('Invalid ownership compare-and-swap result',503);
  return receipt(pending,owner);
 }
 if(operation==='cancel'){
  // An explicit source cancellation CAS cannot race successfully with commit.
  // Expiry alone does nothing; cancel remains available after its admission
  // deadline so a prepared snapshot is never stranded by that deadline.
  if(actor.app!==proof.owner)throw failure('Only the source may cancel its handoff',403);
  if(pending.state==='committed')throw failure('Committed ownership cannot be cancelled');
  if(pending.state==='aborted')return receipt(pending,owner);
  await db.batch([
   db.prepare("UPDATE ownership_current SET epoch=epoch+1 WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=? AND EXISTS(SELECT 1 FROM ownership_transfers WHERE site=? AND unit=? AND binding=? AND state IN ('prepared','ready'))").bind(...args,proof.owner,proof.epoch,proof.authority_digest,...args,encoded),
   db.prepare("UPDATE ownership_transfers SET state='aborted' WHERE site=? AND unit=? AND binding=? AND state IN ('prepared','ready') AND EXISTS(SELECT 1 FROM ownership_current WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=?)").bind(...args,encoded,...args,proof.owner,proof.epoch+1,proof.authority_digest),
  ]);
  pending=await db.prepare('SELECT * FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
  owner=await current(db,...args);
  if(!pending||pending.binding!==encoded||pending.state!=='aborted'||!validCurrent(owner,proof,'aborted'))throw failure('Cancellation lost the ownership CAS; reconcile status');
  return receipt(pending,owner);
 }
 if(operation==='ack'){
  // ACK means this participant durably reconciled its local freeze/activation
  // with the committed epoch. Cleanup is allowed after expiry, never takeover.
  if(data.durable!==true||!['committed','aborted'].includes(pending.state))throw failure('Settled durable reconciliation acknowledgment required');
  const finalState=pending.state,finalOwner=owner.owner,finalEpoch=owner.epoch;
  const column=actor.app===proof.owner?'source_ack':'target_ack';
  const result=await db.batch([
   db.prepare(`UPDATE ownership_transfers SET ${column}=1 WHERE site=? AND unit=? AND binding=? AND state=? AND EXISTS(SELECT 1 FROM ownership_current WHERE site=? AND unit=? AND owner=? AND epoch=? AND authority_digest=?)`).bind(...args,encoded,finalState,...args,finalOwner,finalEpoch,proof.authority_digest),
   db.prepare("DELETE FROM ownership_transfer_scope WHERE site=? AND unit=? AND transfer=? AND EXISTS(SELECT 1 FROM ownership_transfers WHERE site=? AND unit=? AND binding=? AND state IN ('committed','aborted') AND source_ack=1 AND target_ack=1)").bind(...args,proof.transfer,...args,encoded),
   db.prepare("DELETE FROM ownership_transfers WHERE site=? AND unit=? AND binding=? AND state IN ('committed','aborted') AND source_ack=1 AND target_ack=1").bind(...args,encoded),
  ]);
  if(changes(result[0])!==1)throw failure('Transfer acknowledgment raced with cleanup; reconcile current ownership',410);
  if(changes(result[2])===1)return {...receipt({...pending,source_ack:1,target_ack:1},owner),closed:true};
  pending=await db.prepare('SELECT * FROM ownership_transfers WHERE site=? AND unit=?').bind(...args).first();
  // Another exact participant ACK can finish cleanup between transaction and
  // read. This call knows its own durable ACK succeeded, but claims no ledger.
  return pending&&pending.binding===encoded?receipt(pending,owner):{...receipt({binding:encoded,state:finalState,source_ack:1,target_ack:1},owner),closed:true};
 }
}
