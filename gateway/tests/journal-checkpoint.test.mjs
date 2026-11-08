import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {buildCheckpoint,readCheckpoint,compactJournal,checkpointMaintenance,readCheckpointObjects,checkpointGC} from '../lib/journal-checkpoint.mjs';
import {journalRecord,storedJournal} from '../lib/journal-storage.mjs';
const hash=s=>createHash('sha256').update(s).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec('CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq))');
 const objects=new Map();let largestRead=0;
 const env={DB:{prepare(q){const stmt=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){return stmt.get(...args)},async all(){return {results:stmt.all(...args)}},async run(){return stmt.run(...args)}}}},JOURNAL_ARCHIVE:{async put(k,v){objects.set(k,v)},async get(k){return objects.has(k)?{size:Buffer.byteLength(objects.get(k)),async text(){largestRead=Math.max(largestRead,Buffer.byteLength(objects.get(k)));return objects.get(k)}}:null},async delete(k){for(const key of Array.isArray(k)?k:[k])objects.delete(key)}}};
 let seq=0,prev='';
 async function append(changes,existing=false){const text=`{"changes":${changes},"prev":"${prev}","seq":${++seq}}`;prev=hash(text);sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',seq,prev,await storedJournal(env,'s',text,prev,existing));}
 return {env,sql,objects,append,largestRead:()=>largestRead};
}
async function advance(env){for(let n=0;n<100;n++){const r=await buildCheckpoint(env,'s');if(r.advanced)return r;if(!r.pending)return r;}throw Error('No bounded progress');}
test('sharded checkpoint preserves exact numeric bytes, records, receipts and deletion',async()=>{
 const {env,append}=fixture();await append('[["runs","r",{"x":1.0,"big":9007199254740993}],["_receipt_stream","0",{"sequence":8}],["old","x",true]]');await advance(env);
 await append('[["old","x",null]]');await advance(env);
 const manifest=JSON.parse((await readCheckpoint(env,'s')).encoded);assert.equal(manifest.format,2);assert.equal(manifest.shards.length,256);
 const shard=JSON.parse((await readCheckpointObjects(env,'s',[manifest.shards.find(Boolean)])).objects[0].encoded);
 assert.ok(shard.length);assert.equal((await journalRecord(env,'s','old','x')).value,null);
 assert.equal((await journalRecord(env,'s','_receipt_stream','0')).value.sequence,8);
 const runDigest=hash(JSON.stringify(['runs','r'])),bucket=parseInt(runDigest.slice(0,2),16);
 const records=JSON.parse((await readCheckpointObjects(env,'s',[manifest.shards[bucket]])).objects[0].encoded);
 const raw=(await readCheckpointObjects(env,'s',records.find(r=>r[0]==='runs')[2])).objects.map(o=>o.encoded).join('');assert.match(raw,/9007199254740993/);assert.match(raw,/1\.0/);
});
test('partial journal entry resumes durably and does not publish partial state',async()=>{
 const {env,append}=fixture();await append(JSON.stringify(Array.from({length:11},(_,i)=>['k','id'+i,{i}])));
 assert.equal((await buildCheckpoint(env,'s',2)).pending,true);assert.equal(await readCheckpoint(env,'s'),null);
 await advance(env);assert.equal((await journalRecord(env,'s','k','id10')).value.i,10);
});
test('future-only compaction cannot resurrect a deleted peer from retained legacy history',async()=>{
 const {env,append,sql}=fixture();await append('[["peers","p",{"enabled":true}]]');await advance(env);
 await append('[["peers","p",null]]');await advance(env);await append('[]');await advance(env);await append('[]');await advance(env);
 await assert.rejects(compactJournal(env,'s'),/approved/);assert.equal((await compactJournal(env,'s',{approvedFromSequence:2})).removed,1);
 assert.ok(sql.prepare('SELECT seq FROM controller_journal WHERE seq=1').get());assert.equal((await journalRecord(env,'s','peers','p')).value,null);
});
test('corrupt read-back publishes nothing and failed retries can recover',async()=>{
 const {env,append}=fixture();await append('[["k","x",true]]');const get=env.JOURNAL_ARCHIVE.get;
 env.JOURNAL_ARCHIVE.get=async k=>k.startsWith('controller-checkpoint-v2/')?{size:2,text:async()=>'{}'}:get(k);
 await assert.rejects(advance(env),/integrity/);assert.equal(await readCheckpoint(env,'s'),null);env.JOURNAL_ARCHIVE.get=get;assert.equal((await advance(env)).advanced,true);
});
test('current manifest corruption falls back to prior, and prevents purge',async()=>{
 const {env,append,objects}=fixture();await append('[]');await advance(env);await append('[]');const latest=await advance(env);
 objects.set('controller-checkpoint-v2/s/'+latest.manifest.digest,'corrupt');assert.equal(JSON.parse((await readCheckpoint(env,'s')).encoded).seq,1);
 await assert.rejects(compactJournal(env,'s',{approvedFromSequence:1}),/integrity/);
});
test('input larger than explicit memory limit is rejected before archive text read',async()=>{
 const {env,append,largestRead}=fixture();await append('[["k","big","'+'x'.repeat(4194304)+'"]]',true);
 const before=largestRead();await assert.rejects(advance(env),/checkpoint input limit/);assert.equal(largestRead(),before);assert.equal(await readCheckpoint(env,'s'),null);
});
test('bounded shards restore aggregate state much larger than each loaded object',async()=>{
 const {env,append,largestRead}=fixture();const value='x'.repeat(350000);
 for(let i=0;i<80;i++){await append(JSON.stringify([['artifacts','a'+i,{data:value,id:i}]]));await advance(env);}
 const state=JSON.parse((await readCheckpoint(env,'s')).encoded);assert.ok(JSON.stringify(state).length<32768);
 assert.equal((await journalRecord(env,'s','artifacts','a79')).value.data.length,350000);
 assert.ok(largestRead()<524288); // 28MB total, every decode <512KiB
});
test('rolling graph GC bounds unshared checkpoint objects after grace and preserves current/prior',async()=>{
 const {env,append,sql,objects}=fixture();await append('[["authority","a",{"active":true}]]');
 assert.equal((await checkpointMaintenance(env,'s')).enabled,false);env.JOURNAL_RETENTION_ENABLED='1';await assert.rejects(checkpointMaintenance(env,'s'),/boundary/);env.JOURNAL_RETENTION_FROM_SEQUENCE='2';
 await checkpointMaintenance(env,'s');
 for(let i=0;i<30;i++){await append('[["value","x",'+i+']]');await checkpointMaintenance(env,'s');sql.exec('UPDATE checkpoint_objects SET expires=0');}
 for(let i=0;i<6;i++)await checkpointGC(env,'s');
 assert.ok(sql.prepare('SELECT count(*) n FROM controller_journal').get().n<=3);
 assert.ok([...objects.keys()].filter(k=>k.startsWith('controller-checkpoint-v2/')).length<=10);
 assert.equal((await journalRecord(env,'s','authority','a')).value.active,true);assert.equal((await journalRecord(env,'s','value','x')).value,29);
});
test('descriptor shards split adaptively rather than impose a finite record ceiling',async()=>{
 const {env,append}=fixture(),rows=[];
 for(let i=0;rows.length<1100;i++){const id='p'.repeat(440)+i;if(hash(JSON.stringify(['k',id])).startsWith('00'))rows.push(['k',id,i]);}
 await append(JSON.stringify(rows));let result;
 for(let i=0;i<100;i++){result=await buildCheckpoint(env,'s',16);if(result.advanced)break;}
 assert.equal(result.advanced,true);
 const root=JSON.parse((await readCheckpoint(env,'s')).encoded),node=JSON.parse((await readCheckpointObjects(env,'s',[root.shards[0]])).objects[0].encoded);
 assert.equal(node.format,'branches');assert.equal((await journalRecord(env,'s','k',rows.at(-1)[1])).value,rows.at(-1)[2]);
});
test('reader pins protect a long restore while current/prior rotate and GC runs',async()=>{
 const {releaseCheckpoint}=await import('../lib/journal-checkpoint.mjs');
 const {env,append,sql}=fixture();await append('[["k","x",1]]');await advance(env);
 const pinned=await readCheckpoint(env,'s',100,'node');const root=JSON.parse(pinned.encoded),shard=root.shards.find(Boolean);
 for(let i=2;i<6;i++){await append('[["k","x",'+i+']]');await advance(env);}
 sql.exec('UPDATE checkpoint_objects SET expires=0');for(let i=0;i<6;i++)await checkpointGC(env,'s');
 assert.equal((await readCheckpointObjects(env,'s',[shard],pinned.lease,'node')).objects.length,1);
 await assert.rejects(readCheckpointObjects(env,'s',[shard],pinned.lease,'other'),/lease/);
 assert.equal((await compactJournal(env,'s',{approvedFromSequence:1})).removed,0);
 await releaseCheckpoint(env,'s',pinned.lease,'node');
 assert.ok((await compactJournal(env,'s',{approvedFromSequence:1})).removed>0);for(let i=0;i<6;i++)await checkpointGC(env,'s');
 await assert.rejects(readCheckpointObjects(env,'s',[shard]),/retained/);
});
test('publication interruption resumes completed staged entry without another journal write',async()=>{
 const {env,append}=fixture();await append('[["k","x",1]]');const prepare=env.DB.prepare;let fail=true;
 env.DB.prepare=function(q){if(fail&&q.startsWith('UPDATE controller_checkpoints SET previous')){fail=false;throw Error('injected publication interruption')}return prepare(q)};
 await assert.rejects(advance(env),/injected/);assert.equal(await readCheckpoint(env,'s'),null);
 assert.equal((await advance(env)).advanced,true);assert.equal((await journalRecord(env,'s','k','x')).value,1);
});
test('append admission returns413 before archive/commit and exposes durable rejection metric',async()=>{
 const {controllerDevice}=await import('../lib/ha.mjs');const {checkpointStatus}=await import('../lib/journal-checkpoint.mjs');
 const {env,sql,objects}=fixture();env.JOURNAL_MAX_ENTRY_BYTES='4096';sql.exec("CREATE TABLE controller_config(id INTEGER PRIMARY KEY,site TEXT); INSERT INTO controller_config VALUES(1,'s')");
 const entry=JSON.stringify({seq:1,prev:'',changes:[['k','x','x'.repeat(5000)]]});
 const response=await controllerDevice(new Request('https://example.test/bridge/ha/append',{method:'POST',body:JSON.stringify({expected:0,entry})}),env,'/bridge/ha/append',{id:'node'},false);
 assert.equal(response.status,413);assert.match((await response.json()).error,/maximum4096|maximum 4096/);
 assert.equal(sql.prepare('SELECT count(*) n FROM controller_journal').get().n,0);assert.equal(objects.size,0);
 const status=await checkpointStatus(env.DB,'s');assert.equal(status.rejected_entries,1);assert.equal(status.last_rejected_bytes,Buffer.byteLength(entry));
 await assert.rejects(storedJournal(env,'s',entry,hash(entry)),e=>e.status===413);assert.equal(objects.size,0);
});
test('historical oversized entry reports exact blocked sequence and size without deletion',async()=>{
 const {checkpointStatus}=await import('../lib/journal-checkpoint.mjs');const {env,append,sql}=fixture();env.JOURNAL_MAX_ENTRY_BYTES='4096';
 await append('[["k","x","'+'x'.repeat(5000)+'"]]',true);await assert.rejects(advance(env),/seq 1, bytes/);
 const status=await checkpointStatus(env.DB,'s');assert.equal(status.blocked_seq,1);assert.ok(status.blocked_bytes>5000);assert.equal(status.max_entry_seq,1);assert.equal(status.max_entry_bytes,status.blocked_bytes);
 assert.equal(sql.prepare('SELECT count(*) n FROM controller_journal').get().n,1);
});
test('one invocation ingests many entries and coalesces repeated values and descriptor updates',async()=>{
 const {env,append}=fixture();for(let i=0;i<90;i++)await append('[["status","node",{"seq":'+i+'}],["receipt_stream","0",{"sequence":'+i+'}]]');
 const result=await buildCheckpoint(env,'s');assert.equal(result.advanced,true);assert.equal(result.seq,90);assert.equal(result.changes,180);assert.equal(result.coalesced,2);
 assert.equal((await journalRecord(env,'s','status','node')).value.seq,89);
 await append('[]');await advance(env);assert.ok((await compactJournal(env,'s',{approvedFromSequence:1})).removed>0);
});
test('unique-key budget publishes a completed boundary and resumes until backlog drains',async()=>{
 const {env,append}=fixture();for(let i=0;i<120;i++)await append(JSON.stringify([['status','node',{i}],['_receipt','r'+i,{i}]]));
 const first=await buildCheckpoint(env,'s');assert.equal(first.advanced,true);assert.ok(first.seq>20&&first.seq<120);assert.ok(first.coalesced<=64);
 let last=first;for(let i=0;i<6&&last.seq<120;i++)last=await buildCheckpoint(env,'s');assert.equal(last.seq,120);
 assert.equal((await journalRecord(env,'s','_receipt','r119')).value.i,119);
});
test('bulk object pages stop at byte budget and provide an exact continuation',async()=>{
 const {env,append}=fixture();await append(JSON.stringify([['k','big','x'.repeat(100000)]]));await advance(env);
 const manifest=JSON.parse((await readCheckpoint(env,'s')).encoded),leaf=JSON.parse((await readCheckpointObjects(env,'s',[manifest.shards.find(Boolean)])).objects[0].encoded),digest=leaf[0][2][0];
 const page=await readCheckpointObjects(env,'s',Array(128).fill(digest));assert.equal(page.objects.length,32);assert.equal(page.next,32);assert.equal(page.objects.reduce((n,o)=>n+Buffer.byteLength(o.encoded),0),2097152);
});
test('progress schema upgrades preserve earlier checkpoint telemetry',async()=>{
 const {checkpointStatus}=await import('../lib/journal-checkpoint.mjs');const {env,sql}=fixture();
 sql.exec("CREATE TABLE checkpoint_progress(site TEXT PRIMARY KEY,max_entry_bytes INTEGER NOT NULL DEFAULT 0,max_entry_seq INTEGER NOT NULL DEFAULT 0,blocked_seq INTEGER,blocked_bytes INTEGER,last_error TEXT);INSERT INTO checkpoint_progress(site,max_entry_bytes,max_entry_seq) VALUES('s',1234,7)");
 const status=await checkpointStatus(env.DB,'s');assert.equal(status.max_entry_bytes,1234);assert.equal(status.max_entry_seq,7);assert.equal(status.rejected_entries,0);assert.equal(status.max_depth,2);
});
