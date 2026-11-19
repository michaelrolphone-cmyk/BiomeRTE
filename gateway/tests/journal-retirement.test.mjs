import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {journalUploadSchema,managedJournalNamespace,managedJournalKey} from '../lib/journal-upload.mjs';
import {retireLegacyJournal} from '../lib/journal-retirement.mjs';
async function fixture(){
 const sql=new DatabaseSync(':memory:'),DB=sqliteD1(sql),a='a'.repeat(64),b='b'.repeat(64),hash='c'.repeat(64),site='site';
 sql.exec("CREATE TABLE controller_nodes(id TEXT,enabled INTEGER);INSERT INTO controller_nodes VALUES('pi',1),('mac',1);CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq));CREATE TABLE bridge_jobs(id TEXT);INSERT INTO bridge_jobs VALUES('keep');CREATE TABLE auth(id TEXT);INSERT INTO auth VALUES('keep');CREATE TABLE coordination_sessions(site TEXT,session TEXT);INSERT INTO coordination_sessions VALUES('site','keep');CREATE TABLE checkpoint_readers(site TEXT,expires INTEGER);CREATE TABLE checkpoint_builds(site TEXT,until INTEGER);CREATE TABLE journal_uploads(site TEXT,state TEXT)");
 sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run(site,1,hash,'{}');sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('other',1,hash,'{}');
 const objects=new Map([['controller-journal/site/'+hash,'body'],['controller-checkpoint-v2/site/'+hash,'body'],['controller-journal/other/'+hash,'keep'],['other/site/'+hash,'keep']]);
 const archive={async list({prefix,limit}){const keys=[...objects.keys()].filter(k=>k.startsWith(prefix)).sort();return {objects:keys.slice(0,limit).map(key=>({key})),truncated:keys.length>limit}},async delete(keys){for(const key of keys)objects.delete(key)}};
 const env={DB,JOURNAL_ARCHIVE:archive,CONTROLLER_COORDINATION_MODE:'local',LEGACY_RETIREMENT_ENABLED:'1',CONTROLLER_NODE_MAP:JSON.stringify({[a]:'pi',[b]:'mac'})};
 const call=(op,d={},node='pi')=>retireLegacyJournal(env,site,{id:node},op,d);
 const begin=()=>call('begin',{source_seq:1,source_hash:hash});
 const adopt=async()=>{for(const node of ['pi','mac'])await call('adopt',{source_seq:1,source_hash:hash,manifest_digest:'d'.repeat(64),standalone_verified:true,old_activity_drained:true},node)};
 return {sql,env,objects,archive,call,begin,adopt,hash,a,b};
}
async function finish(f){let result;for(let i=0;i<80;i++){result=await f.call('step',{limit:1});if(result.completed)return result;}throw Error('Retirement did not finish');}
test('retirement requires explicit local enablement and both exact adoption proofs',async()=>{const f=await fixture();f.env.LEGACY_RETIREMENT_ENABLED='0';await assert.rejects(f.begin(),e=>e.status===403);f.env.LEGACY_RETIREMENT_ENABLED='1';await f.begin();await assert.rejects(f.call('step'));await assert.rejects(f.call('adopt',{source_seq:1,source_hash:f.hash,manifest_digest:'d'.repeat(64),standalone_verified:true}));await f.adopt();assert.throws(()=>f.sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('site',2,'e'.repeat(64),'{}'));f.sql.close();});
test('bounded retirement deletes exact-site history without auth, queue, session or foreign object loss',async()=>{const f=await fixture();await f.begin();await f.adopt();assert.equal((await finish(f)).completed,true);assert.deepEqual(await f.call('status'),{active:false});assert.deepEqual(await f.call('step'),{active:false});assert.equal(f.sql.prepare("SELECT count(*) n FROM controller_journal WHERE site='site'").get().n,0);assert.equal(f.sql.prepare("SELECT count(*) n FROM controller_journal WHERE site='other'").get().n,1);for(const table of ['bridge_jobs','auth','coordination_sessions'])assert.equal(f.sql.prepare('SELECT count(*) n FROM '+table).get().n,1);for(const table of ['journal_retirement','journal_retirement_adoptions'])assert.equal(f.sql.prepare('SELECT count(*) n FROM '+table).get().n,0);assert.equal(f.objects.size,2);f.sql.close();});
test('changed mapping, live readers and pending uploads prevent deletion',async()=>{const f=await fixture();await f.begin();await f.adopt();f.env.CONTROLLER_NODE_MAP=JSON.stringify({[f.a]:'mac',[f.b]:'pi'});await assert.rejects(f.call('step'));f.env.CONTROLLER_NODE_MAP=JSON.stringify({[f.a]:'pi',[f.b]:'mac'});f.sql.exec("INSERT INTO checkpoint_readers VALUES('site',unixepoch()+60)");await assert.rejects(f.call('step'));f.sql.exec('DELETE FROM checkpoint_readers');f.sql.exec("INSERT INTO journal_uploads VALUES('site','pending')");await assert.rejects(f.call('step'));assert.equal(f.objects.size,4);f.sql.close();});
test('interrupted object deletion resumes from remaining keys without a history ledger',async()=>{const f=await fixture();await f.begin();await f.adopt();const original=f.archive.delete;f.archive.delete=async keys=>{await original(keys);f.archive.delete=original;throw Error('Lost deletion reply')};await assert.rejects(f.call('step'));await finish(f);assert.equal(f.objects.size,2);assert.equal(f.sql.prepare('SELECT count(*) n FROM journal_retirement').get().n,0);f.sql.close();});
test('unrecognized keys inside scoped prefix are protected and block completion',async()=>{const f=await fixture();f.objects.set('controller-journal/site/not-a-journal-object','keep');await f.begin();await f.adopt();await assert.rejects(f.call('step'),e=>e.status===409);assert.equal(f.objects.size,5);f.sql.close();});
test('late object after SQL cleanup prevents completion and remains resumable',async()=>{const f=await fixture();await f.begin();await f.adopt();for(let i=0;i<60;i++){const state=await f.call('status');if(state.phase==='database'&&state.position===9)break;await f.call('step',{limit:1});}
 f.objects.set('controller-journal/site/'+'e'.repeat(64),'late');await assert.rejects(f.call('step'),e=>e.status===409);await finish(f);assert.equal(f.objects.size,2);f.sql.close();});

test('retirement drain removes expired intents while preserving fresh and committed uploads',async()=>{
 const f=await fixture();f.sql.exec('DROP TABLE journal_uploads');await journalUploadSchema(f.env.DB);
 f.env.JOURNAL_MANAGED_UPLOADS='1';const namespace=await managedJournalNamespace(f.env.DB,'site');
 const insert=f.sql.prepare('INSERT INTO journal_uploads VALUES(?,?,?,?,?,?)');
 const tokens=['11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','33333333-3333-3333-3333-333333333333'];
 for(let i=0;i<3;i++){insert.run('site',tokens[i],f.hash,2+i,i===2?'committed':'pending',i===1?Math.floor(Date.now()/1000)+600:0);f.objects.set(managedJournalKey('site',f.hash,tokens[i],namespace),'body');}
 const originalDelete=f.archive.delete;f.archive.delete=keys=>originalDelete(Array.isArray(keys)?keys:[keys]);
 f.env.LEGACY_RETIREMENT_ENABLED='0';await assert.rejects(f.call('drain'),e=>e.status===403);
 f.env.LEGACY_RETIREMENT_ENABLED='1';f.env.CONTROLLER_COORDINATION_MODE='shared';await assert.rejects(f.call('drain'),e=>e.status===403);
 f.env.CONTROLLER_COORDINATION_MODE='local';await assert.rejects(f.call('drain',{limit:33}),e=>e.status===400);
 const result=await f.call('drain');assert.equal(result.pending,1);assert.equal(result.uploads.removed,1);
 assert.equal(f.objects.has(managedJournalKey('site',f.hash,tokens[0],namespace)),false);
 for(const token of tokens.slice(1))assert.equal(f.objects.has(managedJournalKey('site',f.hash,token,namespace)),true);
 await assert.rejects(f.begin(),e=>e.status===409);assert.equal(f.sql.prepare('SELECT count(*) n FROM controller_journal').get().n,2);f.sql.close();
});

test('larger bounded retirement pages drain more than 32 objects in one delete batch',async()=>{
 const f=await fixture();for(let i=0;i<1000;i++)f.objects.set('controller-journal/site/'+i.toString(16).padStart(64,'0'),'body');
 const list=f.archive.list;f.archive.list=async options=>{assert.deepEqual(options.include,[]);return list(options)};await f.begin();await f.adopt();const result=await f.call('step',{limit:1000});assert.equal(result.removed_objects,1000);
 await assert.rejects(f.call('step',{limit:1001}),e=>e.status===400);await finish(f);assert.equal(f.objects.size,2);f.sql.close();
});
