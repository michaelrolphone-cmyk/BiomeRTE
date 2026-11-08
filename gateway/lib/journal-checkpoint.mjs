import {collectJournalUploads} from './journal-upload.mjs';
// Format 2 checkpoints: bounded manifest, bounded descriptor shards, and 64KiB
// value chunks. No operation materializes the full canonical state.
import {hexHash} from './bridge.mjs';
import {journalText,journalEntryLimit,archiveJournalBatch,journalObjectKey} from './journal-storage.mjs';
const cpInitialized=new WeakSet();
const CP_SHARDS=256,CP_CHUNK=65536,CP_SHARD_MAX=524288,CP_SHARD_TARGET=16384,CP_ENTRY_MAX=4194304;
const cpKey=(site,digest)=>'controller-checkpoint-v2/'+encodeURIComponent(site)+'/'+digest;
export async function checkpointSchema(db){
 if(cpInitialized.has(db))return;
 for(const sql of [
  'CREATE TABLE IF NOT EXISTS checkpoint_progress(site TEXT PRIMARY KEY,max_entry_bytes INTEGER NOT NULL DEFAULT 0,max_entry_seq INTEGER NOT NULL DEFAULT 0,blocked_seq INTEGER,blocked_bytes INTEGER,last_error TEXT,rejected_entries INTEGER NOT NULL DEFAULT 0,last_rejected_bytes INTEGER,last_rejected_at REAL,max_depth INTEGER NOT NULL DEFAULT 2,gc_removed_total INTEGER NOT NULL DEFAULT 0,gc_only_rounds INTEGER NOT NULL DEFAULT 0)',
  'CREATE TABLE IF NOT EXISTS controller_checkpoints(site TEXT PRIMARY KEY,current TEXT,previous TEXT)',
  'CREATE TABLE IF NOT EXISTS checkpoint_objects(site TEXT,digest TEXT,refs INTEGER NOT NULL DEFAULT 0,expires INTEGER NOT NULL,deleting INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(site,digest))',
  'CREATE TABLE IF NOT EXISTS checkpoint_readers(site TEXT,token TEXT,owner TEXT,digest TEXT,seq INTEGER,expires INTEGER,PRIMARY KEY(site,token))',
  'CREATE TABLE IF NOT EXISTS checkpoint_edges(site TEXT,parent TEXT,child TEXT,PRIMARY KEY(site,parent,child))',
  'CREATE INDEX IF NOT EXISTS checkpoint_edges_child ON checkpoint_edges(site,child)',
  'CREATE TABLE IF NOT EXISTS checkpoint_builds(site TEXT PRIMARY KEY,base TEXT,seq INTEGER,hash TEXT,offset INTEGER,shards TEXT,token TEXT,until INTEGER)',
  "CREATE TRIGGER IF NOT EXISTS checkpoint_edge_guard BEFORE INSERT ON checkpoint_edges WHEN NOT EXISTS (SELECT 1 FROM checkpoint_objects WHERE site=NEW.site AND digest=NEW.child AND deleting=0) BEGIN SELECT RAISE(ABORT,'Checkpoint object unavailable'); END",
  "CREATE TRIGGER IF NOT EXISTS checkpoint_edge_pin AFTER INSERT ON checkpoint_edges BEGIN UPDATE checkpoint_objects SET expires=0 WHERE site=NEW.site AND digest=NEW.child; END",
  "CREATE TRIGGER IF NOT EXISTS checkpoint_edge_add AFTER INSERT ON checkpoint_edges BEGIN UPDATE checkpoint_objects SET refs=refs+1 WHERE site=NEW.site AND digest=NEW.child; END",
  "CREATE TRIGGER IF NOT EXISTS checkpoint_edge_remove AFTER DELETE ON checkpoint_edges BEGIN UPDATE checkpoint_objects SET refs=refs-1 WHERE site=OLD.site AND digest=OLD.child; END",
  "CREATE TRIGGER IF NOT EXISTS checkpoint_roots_update AFTER UPDATE OF current ON controller_checkpoints BEGIN DELETE FROM checkpoint_edges WHERE site=NEW.site AND parent IN ('root:current','root:previous'); INSERT OR IGNORE INTO checkpoint_edges SELECT NEW.site,'root:current',json_extract(NEW.current,'$.digest') WHERE NEW.current IS NOT NULL; INSERT OR IGNORE INTO checkpoint_edges SELECT NEW.site,'root:previous',json_extract(NEW.previous,'$.digest') WHERE NEW.previous IS NOT NULL; END"
 ])await db.prepare(sql).run();
 const columns=(await db.prepare('PRAGMA table_info(checkpoint_progress)').all()).results;
 for(const [name,type] of [['rejected_entries','INTEGER NOT NULL DEFAULT 0'],['last_rejected_bytes','INTEGER'],['last_rejected_at','REAL'],['max_depth','INTEGER NOT NULL DEFAULT 2'],['gc_removed_total','INTEGER NOT NULL DEFAULT 0'],['gc_only_rounds','INTEGER NOT NULL DEFAULT 0']])if(!columns.some(c=>c.name===name)){try{await db.prepare('ALTER TABLE checkpoint_progress ADD COLUMN '+name+' '+type).run()}catch(error){if(!String(error.message).includes('duplicate column'))throw error}}
 cpInitialized.add(db);
}
function cpBytes(text){return new TextEncoder().encode(text).length}
async function cpGet(env,site,digest,max){
 if(!/^[0-9a-f]{64}$/.test(digest))throw Error('Invalid checkpoint digest');
 const object=await env.JOURNAL_ARCHIVE.get(cpKey(site,digest));
 if(!object)throw Error('Checkpoint object unavailable');
 if(object.size>max)throw Error('Checkpoint object exceeds bound');
 // R2 reports size; fixtures may not. Reader cancels before unbounded decoding.
 let text;
 if(object.body){
  const reader=object.body.getReader(),chunks=[];let size=0;
  try{for(;;){const r=await reader.read();if(r.done)break;size+=r.value.length;if(size>max)throw Error('Checkpoint object exceeds bound');chunks.push(r.value);}}
  finally{await reader.cancel().catch(()=>{});}
  const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
 }else{text=await object.text();if(cpBytes(text)>max)throw Error('Checkpoint object exceeds bound');}
 if(await hexHash(text)!==digest)throw Error('Checkpoint integrity failure');return text;
}
async function cpPut(env,site,text,children=[]){
 const digest=await hexHash(text);
 // Register before uploading. Pins are durable edges; unreachable objects are
 // reclaimed only after an upload grace period and a successful read-back.
 await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_objects(site,digest,refs,expires) VALUES(?,?,0,unixepoch()+1800)').bind(site,digest).run();
 if(children.length)await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_edges SELECT ?,?,value FROM json_each(?)').bind(site,digest,JSON.stringify([...new Set(children)])).run();
 await env.JOURNAL_ARCHIVE.put(cpKey(site,digest),text,{httpMetadata:{contentType:'application/json'}});
 await cpGet(env,site,digest,Math.max(CP_SHARD_MAX,cpBytes(text)));
 return digest;
}
export async function checkpointGC(env,site,limit=128){
 const expired=(await env.DB.prepare('SELECT token FROM checkpoint_readers WHERE site=? AND expires<=unixepoch() LIMIT 16').bind(site).all()).results;
 for(const reader of expired){const removed=await env.DB.prepare('DELETE FROM checkpoint_readers WHERE site=? AND token=? AND expires<=unixepoch() RETURNING token').bind(site,reader.token).first();if(removed)await env.DB.prepare('DELETE FROM checkpoint_edges WHERE site=? AND parent=?').bind(site,'reader:'+reader.token).run();}
 const rows=(await env.DB.prepare('UPDATE checkpoint_objects SET deleting=1 WHERE site=? AND digest IN (SELECT digest FROM checkpoint_objects WHERE site=? AND refs=0 AND expires<=unixepoch() LIMIT ?) AND refs=0 RETURNING digest').bind(site,site,limit).all()).results;
 if(rows.length){
  // R2 bulk delete is idempotent; failed/uncertain batches remain claimed for
  // retry. Delete edges only after object removal succeeds.
  await env.JOURNAL_ARCHIVE.delete(rows.map(row=>cpKey(site,row.digest)));
  const digests=JSON.stringify(rows.map(row=>row.digest));
  await env.DB.prepare('DELETE FROM checkpoint_edges WHERE site=? AND parent IN (SELECT value FROM json_each(?))').bind(site,digests).run();
  await env.DB.prepare('DELETE FROM checkpoint_objects WHERE site=? AND digest IN (SELECT value FROM json_each(?)) AND refs=0 AND deleting=1').bind(site,digests).run();
 }
 return rows.length;
}
// Source slices preserve native floats and integers exactly; size is bounded
// before parsing. Only framing and keys are decoded, never all values at once.
function cpParts(text){
 const out=[];let start=1,depth=0,string=false,escape=false;
 for(let i=1;i<text.length-1;i++){
  const c=text[i];if(string){if(escape)escape=false;else if(c==='\\')escape=true;else if(c==='"')string=false;continue;}
  if(c==='"')string=true;else if(c==='['||c==='{')depth++;else if(c===']'||c==='}')depth--;else if(c===','&&depth===0){out.push(text.slice(start,i).trim());start=i+1;}
 }
 if(text.slice(start,-1).trim())out.push(text.slice(start,-1).trim());return out;
}
function cpEntry(text){
 const fields=new Map(cpParts(text).map(p=>{const i=p.indexOf(':');return [JSON.parse(p.slice(0,i)),p.slice(i+1).trim()]}));
 return {seq:JSON.parse(fields.get('seq')),prev:JSON.parse(fields.get('prev')),changes:cpParts(fields.get('changes')).map(c=>{const p=cpParts(c);if(p.length!==3)throw Error('Invalid checkpoint change');return [JSON.parse(p[0]),JSON.parse(p[1]),p[2]]})};
}
async function cpObserve(env,site,seq,bytes){
 await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_progress(site) VALUES(?)').bind(site).run();
 const limit=journalEntryLimit(env),blocked=bytes>limit,error=blocked?'Journal entry exceeds checkpoint input limit; reconcile without deletion':null;
 await env.DB.prepare('UPDATE checkpoint_progress SET max_entry_seq=CASE WHEN ?>max_entry_bytes THEN ? ELSE max_entry_seq END,max_entry_bytes=MAX(max_entry_bytes,?) WHERE site=?').bind(bytes,seq,bytes,site).run();
 if(blocked)await env.DB.prepare('UPDATE checkpoint_progress SET blocked_seq=?,blocked_bytes=?,last_error=? WHERE site=?').bind(seq,bytes,error,site).run();
 else await env.DB.prepare('UPDATE checkpoint_progress SET blocked_seq=NULL,blocked_bytes=NULL,last_error=NULL WHERE site=? AND blocked_seq=?').bind(site,seq).run();
 if(blocked)throw Error(error+' (seq '+seq+', bytes '+bytes+', limit '+limit+')');
}
async function cpJournal(env,site,row,observe=null,remaining=Infinity){
 if(row.entry.includes('"$biome_archive"')&&JSON.parse(row.entry).$biome_archive===1){
  const obj=await env.JOURNAL_ARCHIVE.get(journalObjectKey(site,row));
  if(!obj||!Number.isSafeInteger(obj.size))throw Error('Journal archive object missing or size unavailable');
  if(obj.size>journalEntryLimit(env))await cpObserve(env,site,row.seq,obj.size);
  if(obj.size>remaining)return null;
  if(observe)observe(row.seq,obj.size);else await cpObserve(env,site,row.seq,obj.size);
  const text=await obj.text();if(cpBytes(text)!==obj.size||await hexHash(text)!==row.hash)throw Error('Journal integrity failure');return text;
 }
 const bytes=cpBytes(row.entry);
 if(bytes>journalEntryLimit(env))await cpObserve(env,site,row.seq,bytes);
 if(bytes>remaining)return null;
 if(observe)observe(row.seq,bytes);else await cpObserve(env,site,row.seq,bytes);
 if(await hexHash(row.entry)!==row.hash)throw Error('Journal integrity failure');return row.entry;
}
export async function checkpointStatus(db,site){
 await checkpointSchema(db);
 const progress=await db.prepare('SELECT max_entry_bytes,max_entry_seq,blocked_seq,blocked_bytes,last_error,rejected_entries,last_rejected_bytes,last_rejected_at,max_depth,gc_removed_total,gc_only_rounds FROM checkpoint_progress WHERE site=?').bind(site).first();
 const build=await db.prepare('SELECT seq,offset FROM checkpoint_builds WHERE site=?').bind(site).first();
 const root=await db.prepare('SELECT current,previous FROM controller_checkpoints WHERE site=?').bind(site).first();
 return {...(progress??{max_entry_bytes:0,max_entry_seq:0,blocked_seq:null,blocked_bytes:null,last_error:null}),building:build??null,current:root?.current?JSON.parse(root.current):null,previous:root?.previous?JSON.parse(root.previous):null};
}
function cpValidateManifest(state,site,manifest){
 if(state.format!==2||state.site!==site||state.seq!==manifest.seq||state.hash!==manifest.hash||!Array.isArray(state.shards)||state.shards.length!==CP_SHARDS||state.shards.some(h=>h!==null&&!/^[0-9a-f]{64}$/.test(h)))throw Error('Checkpoint identity mismatch');
}
export async function loadCheckpoint(env,site,manifest){
 const encoded=await cpGet(env,site,manifest.digest,32768),state=JSON.parse(encoded);cpValidateManifest(state,site,manifest);return {encoded,digest:manifest.digest,state};
}
export async function readCheckpoint(env,site,maxSeq=Number.MAX_SAFE_INTEGER,owner=null){
 await checkpointSchema(env.DB);const row=await env.DB.prepare('SELECT current,previous FROM controller_checkpoints WHERE site=?').bind(site).first();let failure;
 for(const raw of [row?.current,row?.previous])if(raw){const manifest=JSON.parse(raw);if(manifest.seq<=maxSeq)try{const result=await loadCheckpoint(env,site,manifest);const lease=owner?await pinCheckpoint(env,site,owner,manifest.digest,manifest.seq):null;return {encoded:result.encoded,digest:result.digest,lease,object_page_limit:128}}catch(error){failure=error}}
 if(failure)throw failure;return null;
}
export async function readCheckpointObject(env,site,digest){
 // Only objects reachable from a retained root or in-progress builder are
 // available to authenticated controllers, never arbitrary bucket keys.
 const row=await env.DB.prepare('SELECT refs FROM checkpoint_objects WHERE site=? AND digest=?').bind(site,digest).first();
 if(!row||row.refs<1)throw Error('Checkpoint object not retained');
 return {encoded:await cpGet(env,site,digest,CP_SHARD_MAX),digest};
}
async function cpBucket(kind,id){return parseInt((await hexHash(JSON.stringify([kind,id]))).slice(0,2),16)}
async function cpShard(env,site,digest){
 if(!digest)return [];const node=JSON.parse(await cpGet(env,site,digest,CP_SHARD_MAX));
 if(Array.isArray(node))return node;
 if(node?.format!=='branches'||!Array.isArray(node.children)||node.children.length!==16||node.children.some(h=>h!==null&&!/^[0-9a-f]{64}$/.test(h)))throw Error('Invalid checkpoint shard');
 return node;
}
async function cpLocate(env,site,digest,identity){
 const path=[];let depth=2,node=await cpShard(env,site,digest);
 while(!Array.isArray(node)){
  if(depth>=64)throw Error('Checkpoint trie depth exceeded');
  const index=parseInt(identity[depth++],16);path.push({node,index});node=await cpShard(env,site,node.children[index]);
 }
 return {rows:node,path,depth};
}
async function cpTree(env,site,rows,depth,metrics={depth:2}){
 metrics.depth=Math.max(metrics.depth,depth);
 const encoded=JSON.stringify(rows);
 if(cpBytes(encoded)<=CP_SHARD_TARGET)return cpPut(env,site,encoded,rows.flatMap(r=>r[2]));
 if(depth>=64)throw Error('Checkpoint identity hash collision exceeds shard bound');
 const groups=Array.from({length:16},()=>[]),children=Array(16).fill(null);
 for(const row of rows){const identity=await hexHash(JSON.stringify(row.slice(0,2)));groups[parseInt(identity[depth],16)].push(row);}
 for(let i=0;i<16;i++)if(groups[i].length)children[i]=await cpTree(env,site,groups[i],depth+1,metrics);
 return cpPut(env,site,JSON.stringify({format:'branches',children}),children.filter(Boolean));
}
export async function readCheckpointRecord(env,site,kind,id){
 const found=await readCheckpoint(env,site);if(!found)return null;
 const state=JSON.parse(found.encoded),identity=await hexHash(JSON.stringify([kind,id])),located=await cpLocate(env,site,state.shards[parseInt(identity.slice(0,2),16)],identity),record=located.rows.find(r=>r[0]===kind&&r[1]===id);
 if(!record)return {seq:state.seq,value:null};
 let encoded='';for(const digest of record[2]){encoded+=await cpGet(env,site,digest,CP_CHUNK*4);if(cpBytes(encoded)>CP_ENTRY_MAX)throw Error('Checkpoint record exceeds read bound');}
 return {seq:state.seq,value:JSON.parse(encoded)};
}
async function cpParallel(values,fn,concurrency=4){
 let next=0,failure=null;const output=Array(values.length);
 await Promise.all(Array.from({length:Math.min(concurrency,values.length)},async()=>{
  try{for(;;){if(failure)return;const i=next++;if(i>=values.length)return;output[i]=await fn(values[i],i);}}
  catch(error){failure=failure??error;}
 }));
 // Do not release the builder lease while another in-flight object write is
 // still finishing after a sibling failed.
 if(failure)throw failure;return output;
}
async function cpUpdateTree(env,site,digest,updates,depth=2,metrics={depth:2}){
 metrics.depth=Math.max(metrics.depth,depth);
 const node=await cpShard(env,site,digest);
 if(Array.isArray(node)){
  const records=new Map(node.map(row=>[JSON.stringify(row.slice(0,2)),row]));
  for(const update of updates){if(update.record)records.set(update.key,update.record);else records.delete(update.key);}
  const rows=[...records.values()].sort((a,b)=>JSON.stringify(a.slice(0,2)).localeCompare(JSON.stringify(b.slice(0,2))));
  return cpTree(env,site,rows,depth,metrics);
 }
 if(depth>=64)throw Error('Checkpoint trie depth exceeded');
 const groups=new Map();for(const update of updates){const slot=parseInt(update.identity[depth],16);if(!groups.has(slot))groups.set(slot,[]);groups.get(slot).push(update);}
 for(const [slot,group] of groups)node.children[slot]=await cpUpdateTree(env,site,node.children[slot],group,depth+1,metrics);
 return cpPut(env,site,JSON.stringify(node),node.children.filter(Boolean));
}
// Lookup is only a no-op optimization. Exhaustion falls back to the ordinary
// changed-key budget, never skips unverified state or interrupts progress.
function cpUnchangedLookup(env,site,shards){
 const cache=new Map();let bytes=0,reads=0,hits=0,compared=0,matched=0;
 const UNKNOWN=Symbol('lookup budget exhausted');
 async function node(digest){
  if(!digest)return [];
  if(cache.has(digest)){const hit=cache.get(digest);cache.delete(digest);cache.set(digest,hit);hits++;return hit.value;}
  if(reads>=96)return UNKNOWN;
  reads++;
  const encoded=await cpGet(env,site,digest,CP_SHARD_MAX),size=cpBytes(encoded),value=JSON.parse(encoded);
  if(!Array.isArray(value)&&!(value?.format==='branches'&&Array.isArray(value.children)&&value.children.length===16&&value.children.every(h=>h===null||/^[0-9a-f]{64}$/.test(h))))throw Error('Invalid checkpoint shard');
  while(cache.size&&(cache.size>=256||bytes+size>1048576)){const oldest=cache.keys().next().value;bytes-=cache.get(oldest).size;cache.delete(oldest);}
  cache.set(digest,{value,size});bytes+=size;return value;
 }
 async function same(kind,id,value){
  const identity=await hexHash(JSON.stringify([kind,id]));let digest=shards[parseInt(identity.slice(0,2),16)],depth=2;
  for(;;){
   const found=await node(digest);if(found===UNKNOWN)return false;
   if(Array.isArray(found)){
    const record=found.find(r=>r[0]===kind&&r[1]===id);
    if(value==='null')return !record;
    if(!record)return false;
    let index=0;
    for(let start=0;start<value.length;){let end=Math.min(start+CP_CHUNK,value.length);if(end<value.length&&/[\uD800-\uDBFF]/.test(value[end-1]))end--;
     if(index>=record[2].length||await hexHash(value.slice(start,end))!==record[2][index++])return false;start=end;
    }
    return index===record[2].length;
   }
   if(depth>=64)throw Error('Checkpoint trie depth exceeded');digest=found.children[parseInt(identity[depth++],16)];
  }
 }
 return {stats:()=>({reads,hits,bytes,nodes:cache.size,compared,matched}),async same(kind,id,value){
  // Avoid a read-before-write penalty on ordinary mostly-unique histories.
  if(compared>=8&&matched*4<compared)return false;
  const result=await same(kind,id,value);compared++;if(result)matched++;return result;
 }};
}
export async function buildCheckpoint(env,site,limit=2048){
 if(!env.JOURNAL_ARCHIVE)throw Error('Checkpoint archive binding required');
 if(!Number.isInteger(limit)||limit<1||limit>2048)throw Error('Invalid checkpoint batch limit');
 await checkpointSchema(env.DB);
 await env.DB.prepare('INSERT OR IGNORE INTO controller_checkpoints(site) VALUES(?)').bind(site).run();
 const root=await env.DB.prepare('SELECT current,previous FROM controller_checkpoints WHERE site=?').bind(site).first();
 const prior=root.current?await loadCheckpoint(env,site,JSON.parse(root.current)):null;
 await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_builds VALUES(?,?,?,?,0,?,NULL,0)').bind(site,root.current,prior?.state.seq??0,prior?.state.hash??'',JSON.stringify(prior?.state.shards??Array(CP_SHARDS).fill(null))).run();
 const token=crypto.randomUUID();
 const build=await env.DB.prepare('UPDATE checkpoint_builds SET token=?,until=unixepoch()+600 WHERE site=? AND until<=unixepoch() RETURNING *').bind(token,site).first();
 if(!build)return {advanced:false,busy:true};
 try{
  await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_edges SELECT ?,?,value FROM json_each(?) WHERE value IS NOT NULL').bind(site,'stage',build.shards).run();
  await env.DB.prepare("DELETE FROM checkpoint_edges WHERE site=? AND parent LIKE 'build:%' AND parent!=?").bind(site,'build:'+token).run();
  const uploads=await collectJournalUploads(env,site,8);
  if(uploads.removed)return {advanced:false,pending:true,gc_only:true,upload_gc_removed:uploads.removed};
  const collected=await checkpointGC(env,site);
  if(collected){await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_progress(site) VALUES(?)').bind(site).run();await env.DB.prepare('UPDATE checkpoint_progress SET gc_removed_total=gc_removed_total+?,gc_only_rounds=gc_only_rounds+? WHERE site=?').bind(collected,collected===128?1:0,site).run();}
  if(collected===128)return {advanced:false,pending:true,gc_only:true,gc_removed:collected};
  if(build.base!==root.current){
   await env.DB.prepare('DELETE FROM checkpoint_builds WHERE site=? AND token=?').bind(site,token).run();
   await env.DB.prepare("DELETE FROM checkpoint_edges WHERE site=? AND parent='stage'").bind(site).run();
   return {advanced:false,recovered:true};
  }
  let shards=JSON.parse(build.shards),seq=build.seq,hash=build.hash,offset=build.offset;
  let complete=offset===-1,entriesRead=0,changesRead=0,inputBytes=0,valueBytes=0,maxBytes=0,maxSeq=0;
  const progress=await env.DB.prepare('SELECT max_depth FROM checkpoint_progress WHERE site=?').bind(site).first();
  const metrics={depth:Math.max(prior?.state.max_depth??2,progress?.max_depth??2)};
  const keyBudget=Math.max(1,Math.min(48,Math.floor(500/(5*(metrics.depth-1)+4))));
  const updates=new Map(),unchanged=cpUnchangedLookup(env,site,shards);let skipped=0;
  if(!complete){
   while(entriesRead<96&&changesRead<limit){
    const row=await env.DB.prepare('SELECT seq,hash,entry FROM controller_journal WHERE site=? AND seq=?').bind(site,seq+1).first();
    if(!row){complete=offset===0;break;}
    const text=await cpJournal(env,site,row,(number,bytes)=>{inputBytes+=bytes;if(bytes>maxBytes){maxBytes=bytes;maxSeq=number;}},8388608-inputBytes);
    if(text===null){complete=offset===0;break;}
    entriesRead++;
    const entry=cpEntry(text);if(entry.seq!==seq+1||entry.prev!==hash)throw Error('Checkpoint journal discontinuity');
    const entryOffset=offset,beforeUpdates=new Map(updates),beforeBytes=valueBytes,beforeChanges=changesRead,beforeSkipped=skipped;
    while(offset<entry.changes.length&&changesRead<limit){
     const [kind,id,value]=entry.changes[offset];if(typeof kind!=='string'||typeof id!=='string')throw Error('Invalid checkpoint key');
     const key=JSON.stringify([kind,id]),bytes=cpBytes(value),prior=updates.get(key);
     if(await unchanged.same(kind,id,value)){
      if(prior){updates.delete(key);valueBytes-=prior.bytes;}
      offset++;changesRead++;skipped++;continue;
     }
     if(!prior&&updates.size>=keyBudget)break;
     if(updates.size&&valueBytes-(prior?.bytes??0)+bytes>2097152)break;
     updates.set(key,{key,kind,id,value,bytes});valueBytes+=bytes-(prior?.bytes??0);offset++;changesRead++;
    }
    if(offset<entry.changes.length){
     // Publish at a completed-entry boundary when possible, rather than keep
     // chasing a new partial entry forever while appends continue.
     if(entryOffset===0&&seq>build.seq){updates.clear();for(const [key,value] of beforeUpdates)updates.set(key,value);valueBytes=beforeBytes;changesRead=beforeChanges;skipped=beforeSkipped;offset=0;complete=true;}
     else complete=false;
     break;
    }
    seq=row.seq;hash=row.hash;offset=0;complete=true;
   }
   if(maxBytes){await cpObserve(env,site,maxSeq,maxBytes);await env.DB.prepare('UPDATE checkpoint_progress SET blocked_seq=NULL,blocked_bytes=NULL,last_error=NULL WHERE site=? AND blocked_seq>? AND blocked_seq<=?').bind(site,build.seq,seq+(offset>0?1:0)).run();}
   if(!updates.size&&seq===build.seq&&offset===build.offset)return {advanced:false,seq,entries:entriesRead,changes:changesRead};
   if(complete)offset=-1;
   // Only the final write to each key needs an immutable value object. Parallel
   // I/O is capped at four values/buckets; no whole-state descriptor is loaded.
   const prepared=await cpParallel([...updates.values()],async update=>{
    const identity=await hexHash(update.key),chunks=[];
    if(update.value!=='null'){
     JSON.parse(update.value);
     for(let start=0;start<update.value.length;){let end=Math.min(start+CP_CHUNK,update.value.length);if(end<update.value.length&&/[\uD800-\uDBFF]/.test(update.value[end-1]))end--;chunks.push(await cpPut(env,site,update.value.slice(start,end)));start=end;}
    }
    return {...update,identity,record:update.value==='null'?null:[update.kind,update.id,chunks]};
   });
   const groups=new Map();for(const update of prepared){const bucket=parseInt(update.identity.slice(0,2),16);if(!groups.has(bucket))groups.set(bucket,[]);groups.get(bucket).push(update);}
   await cpParallel([...groups],async([bucket,group])=>{
    const digest=await cpUpdateTree(env,site,shards[bucket],group,2,metrics);
    await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_edges VALUES(?,?,?)').bind(site,'build:'+token,digest).run();shards[bucket]=digest;
   });
  }
  await env.DB.prepare('UPDATE checkpoint_progress SET max_depth=MAX(max_depth,?) WHERE site=?').bind(metrics.depth,site).run();
  const saved=await env.DB.prepare('UPDATE checkpoint_builds SET seq=?,hash=?,offset=?,shards=? WHERE site=? AND token=? AND until>unixepoch() RETURNING site').bind(seq,hash,offset,JSON.stringify(shards),site,token).first();
  if(!saved)throw Error('Checkpoint build lease expired');
  // Persistent stage root replaces temporary pins only after progress is safe.
  await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_edges SELECT ?,?,value FROM json_each(?)').bind(site,'stage',JSON.stringify([...new Set(shards.filter(Boolean))])).run();
  if(!complete)return {advanced:false,pending:true,seq,offset,entries:entriesRead,changes:changesRead,coalesced:updates.size,input_bytes:inputBytes,key_budget:keyBudget,max_depth:metrics.depth,unchanged:skipped,lookup:unchanged.stats()};
  const encoded=JSON.stringify({format:2,site,seq,hash,shards,max_depth:metrics.depth,parent:build.base?JSON.parse(build.base):null}),digest=await cpPut(env,site,encoded,shards.filter(Boolean)),manifest={seq,hash,digest,format:2};
  const changed=await env.DB.prepare('UPDATE controller_checkpoints SET previous=current,current=? WHERE site=? AND current IS ? AND EXISTS (SELECT 1 FROM checkpoint_builds WHERE site=? AND token=? AND until>unixepoch()) RETURNING site').bind(JSON.stringify(manifest),site,build.base,site,token).first();
  if(!changed)throw Error('Checkpoint publication conflict');
  await env.DB.prepare('DELETE FROM checkpoint_builds WHERE site=? AND token=?').bind(site,token).run();
  await env.DB.prepare("DELETE FROM checkpoint_edges WHERE site=? AND parent='stage'").bind(site).run();
  return {advanced:true,seq,manifest,entries:entriesRead,changes:changesRead,coalesced:updates.size,input_bytes:inputBytes,key_budget:keyBudget,max_depth:metrics.depth,unchanged:skipped,lookup:unchanged.stats()};
 }finally{
  await env.DB.prepare('UPDATE checkpoint_builds SET until=0 WHERE site=? AND token=?').bind(site,token).run();
  await env.DB.prepare('DELETE FROM checkpoint_edges WHERE site=? AND parent=?').bind(site,'build:'+token).run();
 }
}
export async function compactJournal(env,site,{approvedFromSequence,limit=128}={}){
 if(!Number.isSafeInteger(approvedFromSequence)||approvedFromSequence<1)throw Error('Explicit approved retention boundary required');
 if(!Number.isInteger(limit)||limit<1||limit>128)throw Error('Invalid compaction limit');
 await checkpointSchema(env.DB);const root=await env.DB.prepare('SELECT current,previous FROM controller_checkpoints WHERE site=?').bind(site).first();
 if(!root?.current||!root.previous)return {removed:0};
 const current=await loadCheckpoint(env,site,JSON.parse(root.current)),previous=await loadCheckpoint(env,site,JSON.parse(root.previous));
 if(current.state.seq<=previous.state.seq)throw Error('Checkpoint recovery order invalid');
 const parent=current.state.parent;
 if(parent&&(parent.seq!==previous.state.seq||parent.hash!==previous.state.hash||parent.digest!==previous.digest))throw Error('Checkpoint recovery ancestry mismatch');
 if(!parent&&current.state.seq!==previous.state.seq+1)throw Error('Checkpoint recovery ancestry unavailable');
 const first=await env.DB.prepare('SELECT seq,hash,entry FROM controller_journal WHERE site=? AND seq=?').bind(site,previous.state.seq+1).first();
 const last=await env.DB.prepare('SELECT seq,hash,entry FROM controller_journal WHERE site=? AND seq=?').bind(site,current.state.seq).first();
 if(!first||!last)throw Error('Checkpoint recovery tail missing');
 const firstEntry=cpEntry(await cpJournal(env,site,first)),lastEntry=first.seq===last.seq?firstEntry:cpEntry(await cpJournal(env,site,last));
 if(firstEntry.prev!==previous.state.hash||lastEntry.seq!==current.state.seq||last.hash!==current.state.hash)throw Error('Checkpoint recovery tail corrupt');
 // Parent manifest binds the entire hash-verified ingestion interval. Immutable
 // intermediate journal bytes were verified before this root was published.
 // All new DAG objects were read-back verified before atomic publication;
 // unchanged immutable objects inherit that verification from the prior root.
 const readers=await env.DB.prepare('SELECT min(seq) floor FROM checkpoint_readers WHERE site=? AND expires>unixepoch()').bind(site).first();
 const cutoff=Math.min(previous.state.seq,readers.floor??previous.state.seq);
 const rows=(await env.DB.prepare('SELECT seq,hash,entry FROM controller_journal WHERE site=? AND seq>=? AND seq<? ORDER BY seq LIMIT ?').bind(site,approvedFromSequence,cutoff,limit).all()).results;
 if(rows.length){await env.JOURNAL_ARCHIVE.delete(rows.map(row=>journalObjectKey(site,row)));await env.DB.prepare('DELETE FROM controller_journal WHERE site=? AND seq IN (SELECT value FROM json_each(?)) AND seq<?').bind(site,JSON.stringify(rows.map(row=>row.seq)),cutoff).run();}
 return {removed:rows.length,recovery:previous.state.seq};
}
export async function checkpointMaintenance(env,site){
 if(env.JOURNAL_RETENTION_ENABLED!=='1')return {enabled:false};const from=Number(env.JOURNAL_RETENTION_FROM_SEQUENCE);
 if(!Number.isSafeInteger(from)||from<1)throw Error('Approved retention boundary not configured');
 await archiveJournalBatch(env,site);
 const checkpoint=await buildCheckpoint(env,site);const compacted=await compactJournal(env,site,{approvedFromSequence:from});return {enabled:true,checkpoint,compacted};
}

export async function readCheckpointObjects(env,site,digests,lease=null,owner=null){
 if(owner){const valid=await env.DB.prepare('UPDATE checkpoint_readers SET expires=unixepoch()+600 WHERE site=? AND token=? AND owner=? AND expires>unixepoch() RETURNING token').bind(site,lease,owner).first();if(!valid)throw Error('Checkpoint read lease expired');}
 if(!Array.isArray(digests)||digests.length<1||digests.length>128)throw Error('Invalid checkpoint object page');
 const objects=[];let bytes=0;
 // Fixed waves bound speculative reads and decoded objects to four. Settle
 // every read before returning or throwing; never leave background R2 work.
 // Consume in request order, including errors, so a corrupt look-ahead object
 // beyond this page's byte boundary does not invalidate an otherwise valid page.
 for(let start=0;start<digests.length;start+=4){
  const wave=await Promise.all(digests.slice(start,start+4).map(async digest=>{
   try{return {object:await readCheckpointObject(env,site,digest)}}catch(error){return {error}}
  }));
  for(const result of wave){
   if(result.error)throw result.error;
   const object=result.object,size=cpBytes(object.encoded);
   if(objects.length&&bytes+size>2097152)return {objects,next:objects.length};
   objects.push(object);bytes+=size;
  }
 }
 return {objects,next:objects.length<digests.length?objects.length:null};
}

async function pinCheckpoint(env,site,owner,digest,seq){
 const count=await env.DB.prepare('SELECT count(*) n FROM checkpoint_readers WHERE site=? AND owner=? AND expires>unixepoch()').bind(site,owner).first();
 if(count.n>=2)throw Error('Checkpoint restore already active');
 const token=crypto.randomUUID();
 const inserted=await env.DB.prepare('INSERT INTO checkpoint_readers SELECT ?,?,?,?,?,unixepoch()+600 WHERE (SELECT count(*) FROM checkpoint_readers WHERE site=? AND owner=? AND expires>unixepoch())<2 RETURNING token').bind(site,token,owner,digest,seq,site,owner).first();
 if(!inserted)throw Error('Checkpoint restore already active');
 try{await env.DB.prepare('INSERT INTO checkpoint_edges VALUES(?,?,?)').bind(site,'reader:'+token,digest).run();}
 catch(error){await env.DB.prepare('DELETE FROM checkpoint_readers WHERE site=? AND token=?').bind(site,token).run();throw error;}
 return token;
}
export async function releaseCheckpoint(env,site,token,owner=null){
 const row=await env.DB.prepare('SELECT owner FROM checkpoint_readers WHERE site=? AND token=?').bind(site,token).first();
 if(!row||(owner&&row.owner!==owner))return {released:false};
 await env.DB.prepare('DELETE FROM checkpoint_edges WHERE site=? AND parent=?').bind(site,'reader:'+token).run();
 await env.DB.prepare('DELETE FROM checkpoint_readers WHERE site=? AND token=?').bind(site,token).run();return {released:true};
}

export async function renewCheckpoint(env,site,token,owner){
 const renewed=await env.DB.prepare('UPDATE checkpoint_readers SET expires=unixepoch()+600 WHERE site=? AND token=? AND owner=? AND expires>unixepoch() RETURNING token').bind(site,token,owner).first();
 if(!renewed)throw Error('Checkpoint read lease expired');return {renewed:true};
}

export async function recordJournalRejection(env,site,bytes){
 await checkpointSchema(env.DB);
 await env.DB.prepare('INSERT OR IGNORE INTO checkpoint_progress(site) VALUES(?)').bind(site).run();
 await env.DB.prepare('UPDATE checkpoint_progress SET rejected_entries=rejected_entries+1,last_rejected_bytes=?,last_rejected_at=unixepoch() WHERE site=?').bind(bytes,site).run();
}
