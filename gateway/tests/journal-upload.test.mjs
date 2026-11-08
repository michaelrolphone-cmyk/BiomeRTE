import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {storedJournal,journalText,journalObjectKey,archiveJournalBatch} from '../lib/journal-storage.mjs';
import {journalUploadSchema,collectJournalUploads,managedJournalKey,managedJournalNamespace} from '../lib/journal-upload.mjs';
import {buildCheckpoint,checkpointGC,compactJournal} from '../lib/journal-checkpoint.mjs';
const hash=s=>createHash('sha256').update(s).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec('CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq))');
 const objects=new Map();let puts=0,lists=0,deleted=[];
 const env={JOURNAL_MANAGED_UPLOADS:'1',DB:{prepare(q){const stmt=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){return stmt.get(...args)},async all(){return {results:stmt.all(...args)}},async run(){return stmt.run(...args)}}}},JOURNAL_ARCHIVE:{
  async put(k,v){puts++;objects.set(k,v)},async get(k){return objects.has(k)?{size:Buffer.byteLength(objects.get(k)),text:async()=>objects.get(k)}:null},
  async delete(keys){for(const key of Array.isArray(keys)?keys:[keys]){deleted.push(key);objects.delete(key)}},
  async list({prefix,limit,cursor}){lists++;const keys=[...objects.keys()].sort().filter(k=>k.startsWith(prefix)&&(!cursor||k>cursor)),selected=keys.slice(0,limit);return {objects:selected.map(key=>({key})),truncated:keys.length>limit,...(keys.length>limit?{cursor:selected.at(-1)}:{})}}
 }};
 const entry=(seq=1,prev='',value=true)=>JSON.stringify({seq,prev,changes:[['k','id',value]]});
 const commit=(text,pointer)=>{const e=JSON.parse(text);sql.prepare('INSERT OR IGNORE INTO controller_journal VALUES(?,?,?,?)').run('s',e.seq,hash(text),pointer)};
 const expire=()=>sql.exec("UPDATE journal_uploads SET expires=0 WHERE state!='committed'");
 return {sql,env,objects,entry,commit,expire,puts:()=>puts,lists:()=>lists,deleted};
}
test('committed uploads survive lost ACK, duplicate loser collection, and dual-format replay',async()=>{
 const f=fixture(),text=f.entry(),pointer=await storedJournal(f.env,'s',text,hash(text));f.commit(text,pointer);
 const retry=await storedJournal(f.env,'s',text,hash(text));f.commit(text,retry); // INSERT ignored, loser stays pending
 assert.notEqual(pointer,retry);f.expire();await collectJournalUploads(f.env,'s');
 assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,1);
 assert.equal(await journalText(f.env,'s',{hash:hash(text),entry:pointer}),text);
 assert.equal(f.objects.has(journalObjectKey('s',{hash:hash(text),entry:retry})),false);
 const old='controller-journal/s/'+hash('historic');f.objects.set(old,'historic');await collectJournalUploads(f.env,'s');assert.equal(f.objects.get(old),'historic');
});
test('late PUT after intent removal is found next cycle and late commit is fenced',async()=>{
 const f=fixture(),text=f.entry();let finish,started;
 const ready=new Promise(r=>{started=r});f.env.JOURNAL_ARCHIVE.put=async(k,v)=>{started();await new Promise(r=>{finish=()=>{f.objects.set(k,v);r()}})};
 const upload=storedJournal(f.env,'s',text,hash(text));await ready;f.expire();await collectJournalUploads(f.env,'s');assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,0);
 finish();const pointer=await upload;assert.throws(()=>f.commit(text,pointer),/lease unavailable/);assert.equal(f.objects.size,1);
 await collectJournalUploads(f.env,'s');assert.equal(f.objects.size,0);assert.equal(f.sql.prepare('SELECT count(*) n FROM controller_journal').get().n,0);
});
test('collector claim fences concurrent commit; active uploads and uncertain deletes are retained',async()=>{
 const f=fixture(),text=f.entry(),pointer=await storedJournal(f.env,'s',text,hash(text));
 await collectJournalUploads(f.env,'s');assert.equal(f.objects.size,1); // active
 f.expire();const remove=f.env.JOURNAL_ARCHIVE.delete;let checked=false;
 f.env.JOURNAL_ARCHIVE.delete=async key=>{assert.throws(()=>f.commit(text,pointer),/lease unavailable/);checked=true;throw Error('uncertain delete')};
 await assert.rejects(collectJournalUploads(f.env,'s'),/uncertain/);assert.ok(checked);assert.equal(f.sql.prepare('SELECT state FROM journal_uploads').get().state,'collecting');
 f.env.JOURNAL_ARCHIVE.delete=remove;await collectJournalUploads(f.env,'s');assert.equal(f.objects.size,0);
});
test('bounded cyclic listing rediscovers late objects behind cursor and never scans legacy prefix',async()=>{
 const f=fixture(),namespace=await managedJournalNamespace(f.env.DB,'s');
 for(let n=0;n<10;n++)f.objects.set(managedJournalKey('s',String(n).padStart(64,'0'),crypto.randomUUID(),namespace),'orphan');
 f.objects.set('controller-journal/s/old','preserve');f.objects.set(managedJournalKey('s','f'.repeat(64),crypto.randomUUID(),crypto.randomUUID()),'unrelated namespace');const first=await collectJournalUploads(f.env,'s',3);assert.equal(first.listed,3);assert.equal(first.cycle_complete,false);
 const late=managedJournalKey('s','0'.repeat(64),crypto.randomUUID(),namespace);f.objects.set(late,'late');
 for(let n=0;n<8;n++){const r=await collectJournalUploads(f.env,'s',3);assert.ok(r.listed<=3)}
 assert.equal(f.objects.size,2);assert.equal(f.objects.get('controller-journal/s/old'),'preserve');assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,0);
});
test('pending-intent backpressure rejects before PUT and committed metadata retires atomically',async()=>{
 const f=fixture();await journalUploadSchema(f.env.DB);
 for(let n=0;n<256;n++)f.sql.prepare("INSERT INTO journal_uploads VALUES(?,?,?,1,'pending',9999999999)").run('s',crypto.randomUUID(),'a'.repeat(64));
 const text=f.entry();await assert.rejects(storedJournal(f.env,'s',text,hash(text)),/backlog full/);assert.equal(f.puts(),0);
 f.sql.exec('DELETE FROM journal_uploads');const pointer=await storedJournal(f.env,'s',text,hash(text));f.commit(text,pointer);
 f.sql.exec("DELETE FROM controller_journal WHERE site='s'");assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,0);
 await collectJournalUploads(f.env,'s');assert.equal(f.objects.size,0);
});
test('managed entries checkpoint, replay and compact while original untracked bytes stay intact',async()=>{
 const f=fixture();let prev='';
 for(let n=1;n<=4;n++){const text=f.entry(n,prev,n),pointer=await storedJournal(f.env,'s',text,hash(text));f.commit(text,pointer);prev=hash(text);assert.equal((await buildCheckpoint(f.env,'s')).advanced,true)}
 f.objects.set('controller-journal/s/untracked','preserve');const result=await compactJournal(f.env,'s',{approvedFromSequence:2});assert.equal(result.removed,1);
 assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,3);assert.equal(f.objects.get('controller-journal/s/untracked'),'preserve');
 const row=f.sql.prepare('SELECT * FROM controller_journal WHERE seq=4').get();assert.equal(JSON.parse(await journalText(f.env,'s',row)).seq,4);
});
test('saturated derived GC yields before creating a checkpoint and resumes after debt drains',async()=>{
 const f=fixture();delete f.env.JOURNAL_MANAGED_UPLOADS;
 const text=f.entry();f.commit(text,text);await buildCheckpoint(f.env,'s');
 for(let n=0;n<300;n++){const digest=hash('orphan'+n);f.sql.prepare('INSERT INTO checkpoint_objects VALUES(?,?,0,0,0)').run('s',digest);f.objects.set('controller-checkpoint-v2/s/'+digest,'orphan'+n)}
 const next=f.entry(2,hash(text),false);f.commit(next,next);let result=await buildCheckpoint(f.env,'s');assert.equal(result.gc_only,true);assert.equal(result.gc_removed,128);assert.equal(result.seq,undefined);
 result=await buildCheckpoint(f.env,'s');assert.equal(result.gc_only,true);
 result=await buildCheckpoint(f.env,'s');assert.equal(result.advanced,true);assert.equal(result.seq,2);const counters=f.sql.prepare('SELECT gc_removed_total,gc_only_rounds FROM checkpoint_progress').get();assert.ok(counters.gc_removed_total>=300);assert.equal(counters.gc_only_rounds,2);
});
test('delayed loser DELETE cannot remove a newer attempt committed at the same sequence',async()=>{
 const f=fixture(),text=f.entry(),old=await storedJournal(f.env,'s',text,hash(text));f.expire();
 const remove=f.env.JOURNAL_ARCHIVE.delete;let release,started;const ready=new Promise(r=>{started=r});
 f.env.JOURNAL_ARCHIVE.delete=async key=>{if(key===journalObjectKey('s',{hash:hash(text),entry:old})){started();await new Promise(r=>{release=r})}return remove(key)};
 const cleaning=collectJournalUploads(f.env,'s');await ready;
 const newer=await storedJournal(f.env,'s',text,hash(text));assert.notEqual(old,newer);f.commit(text,newer);
 release();await cleaning;assert.equal(await journalText(f.env,'s',{hash:hash(text),entry:newer}),text);assert.equal(f.sql.prepare('SELECT state FROM journal_uploads').get().state,'committed');
});
test('crash after verified PUT but before commit leaves a collectable intent, not an untracked blob',async()=>{
 const f=fixture(),text=f.entry();await storedJournal(f.env,'s',text,hash(text));assert.equal(f.objects.size,1);f.expire();
 await collectJournalUploads(f.env,'s');assert.equal(f.objects.size,0);assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,0);
});

test('finite historical inline migration stays legacy-format even with managed writes enabled',async()=>{
 const f=fixture(),text=f.entry(1,'','x'.repeat(10000));f.commit(text,text);
 const result=await archiveJournalBatch(f.env,'s');assert.equal(result.moved,1);
 const row=f.sql.prepare('SELECT * FROM controller_journal WHERE seq=1').get();
 assert.equal(JSON.parse(row.entry).$biome_archive,1);assert.equal(JSON.parse(row.entry).$biome_upload,undefined);
 assert.equal(f.objects.has('controller-journal/s/'+hash(text)),true);assert.equal(await journalText(f.env,'s',row),text);
 await journalUploadSchema(f.env.DB);assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_uploads').get().n,0);
 await collectJournalUploads(f.env,'s');assert.equal(f.objects.size,1);assert.equal((await archiveJournalBatch(f.env,'s')).moved,0);
});
