// Run: node --max-old-space-size=64 tests/checkpoint-memory-benchmark.mjs
// Disk-backed fake R2 avoids making the test fixture itself hold the dataset.
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,writeFileSync,readFileSync,statSync,unlinkSync,existsSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {storedJournal} from '../lib/journal-storage.mjs';
import {buildCheckpoint,readCheckpoint,readCheckpointObjects,readCheckpointRecord} from '../lib/journal-checkpoint.mjs';
const directory=mkdtempSync(join(tmpdir(),'checkpoint-bench-')),sql=new DatabaseSync(join(directory,'db.sqlite'));
const digest=s=>createHash('sha256').update(s).digest('hex');
const path=k=>join(directory,digest(k));let maxRead=0,maxCheckpointRead=0,maxHeap=0,previous='';
sql.exec('CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq))');
const env={DB:{prepare(q){const stmt=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){return stmt.get(...args)},async all(){return {results:stmt.all(...args)}},async run(){return stmt.run(...args)}}}},JOURNAL_ARCHIVE:{async put(k,v){writeFileSync(path(k),v)},async get(k){const file=path(k);return existsSync(file)?{size:statSync(file).size,async text(){const text=readFileSync(file,'utf8');maxRead=Math.max(maxRead,Buffer.byteLength(text));if(k.startsWith('controller-checkpoint-v2/'))maxCheckpointRead=Math.max(maxCheckpointRead,Buffer.byteLength(text));maxHeap=Math.max(maxHeap,process.memoryUsage().heapUsed);return text}}:null},async delete(k){for(const key of Array.isArray(k)?k:[k])try{unlinkSync(path(key))}catch(e){if(e.code!=='ENOENT')throw e}}}};
try{
 const count=600,bytes=350000;
 for(let i=1;i<=count;i++){
  const text=JSON.stringify({seq:i,prev:previous,changes:[['artifacts','a'+i,{data:'x'.repeat(bytes),id:i}]]});previous=digest(text);
  await env.JOURNAL_ARCHIVE.put('controller-journal/s/'+previous,text);
  sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',i,previous,'{"$biome_archive":1,"changes":[["artifacts","a'+i+'"]]}');
  assert.equal((await buildCheckpoint(env,'s')).advanced,true);
 }
 const root=await readCheckpoint(env,'s'),manifest=JSON.parse(root.encoded);let restored=0,total=0;
 for(const hash of manifest.shards.filter(Boolean)){
  const shard=JSON.parse((await readCheckpointObjects(env,'s',[hash])).objects[0].encoded);
  for(const record of shard){let raw='';for(let i=0;i<record[2].length;i+=8)for(const object of (await readCheckpointObjects(env,'s',record[2].slice(i,i+8))).objects)raw+=object.encoded;
   const value=JSON.parse(raw);assert.equal(value.data.length,bytes);restored++;total+=value.data.length;
  }
 }
 assert.equal(restored,count);assert.ok(total>200000000);assert.ok(maxRead<524288);
 const near=JSON.stringify({seq:601,prev:previous,changes:Array.from({length:11},(_,i)=>['runs','near'+i,{state:'completed',result:'y'.repeat(350000)}])});
 const nearHash=digest(near),stored=await storedJournal(env,'s',near,nearHash);sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',601,nearHash,stored);
 let advanced;for(let i=0;i<5;i++){advanced=await buildCheckpoint(env,'s');if(advanced.advanced)break;}
 assert.equal(advanced.advanced,true);assert.equal((await readCheckpointRecord(env,'s','runs','near10')).value.result.length,350000);assert.ok(Buffer.byteLength(near)>3800000&&Buffer.byteLength(near)<4194304);assert.ok(maxCheckpointRead<524288);
 console.log(JSON.stringify({records:restored,payload_bytes:total,max_object_bytes:maxRead,max_checkpoint_object_bytes:maxCheckpointRead,near_capacity_entry_bytes:Buffer.byteLength(near),max_observed_heap_bytes:maxHeap,heap_limit_flag:'64MiB',root_bytes:Buffer.byteLength(root.encoded)}));
}finally{sql.close();rmSync(directory,{recursive:true,force:true});}
