// Disk-backed 77k-entry backlog plus new appends every build invocation.
// Run: node --max-old-space-size=64 tests/checkpoint-throughput-benchmark.mjs
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {buildCheckpoint,readCheckpointRecord,compactJournal} from '../lib/journal-checkpoint.mjs';
const directory=mkdtempSync(join(tmpdir(),'checkpoint-throughput-')),sql=new DatabaseSync(join(directory,'db.sqlite'));
sql.exec('PRAGMA journal_mode=WAL;PRAGMA synchronous=OFF;CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq));CREATE TABLE objects(key TEXT PRIMARY KEY,value TEXT)');
const hash=s=>createHash('sha256').update(s).digest('hex');let calls=0,maxCalls=0,head=0,previous='',puts=0,deletes=0;
const env={DB:{prepare(q){const stmt=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){calls++;return stmt.get(...args)},async all(){calls++;return {results:stmt.all(...args)}},async run(){calls++;return stmt.run(...args)}}}},JOURNAL_ARCHIVE:{async put(k,v){calls++;puts++;sql.prepare('INSERT OR REPLACE INTO objects VALUES(?,?)').run(k,v)},async get(k){calls++;const row=sql.prepare('SELECT value FROM objects WHERE key=?').get(k);return row?{size:Buffer.byteLength(row.value),async text(){return row.value}}:null},async delete(keys){calls++;for(const k of Array.isArray(keys)?keys:[keys]){deletes++;sql.prepare('DELETE FROM objects WHERE key=?').run(k)}}}};
function append(legacy){
 const seq=++head,changes=[['status','controller',{seq,ready:true}],legacy?['_receipt','legacy-'+seq,{signature:'request-'+seq,result:{ok:true,seq}}]:['_receipt_stream',String(seq%16),{sequence:seq,result:{ok:true,seq}}]];
 const text=JSON.stringify({seq,prev:previous,changes});previous=hash(text);
 sql.prepare('INSERT INTO objects VALUES(?,?)').run('controller-journal/s/'+previous,text);
 sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',seq,previous,JSON.stringify({$biome_archive:1,changes:changes.map(c=>c.slice(0,2))}));
}
try{
 const backlog=Number(process.env.BACKLOG??77000);sql.exec('BEGIN');for(let i=0;i<backlog;i++)append(true);sql.exec('COMMIT');
 const started=performance.now();let rounds=0,position=0,peakLag=head,advancedEntries=0,maximumAdvance=0;
 while(position<head&&rounds<3000){
  for(let i=0;i<16;i++)append(false);calls=0;
  const result=await buildCheckpoint(env,'s');await compactJournal(env,'s',{approvedFromSequence:backlog+1});rounds++;maxCalls=Math.max(maxCalls,calls);
  assert.ok(calls<1000,'bounded service-call budget exceeded: '+calls);
  if(result.advanced){maximumAdvance=Math.max(maximumAdvance,result.seq-position);advancedEntries+=result.seq-position;position=result.seq;}
  peakLag=Math.max(peakLag,head-position);
  if(rounds%200===0)console.error(JSON.stringify({rounds,head,position,lag:head-position,elapsed_seconds:(performance.now()-started)/1000}));
 }
 assert.equal(position,head,'backlog did not drain while appends continued');
 for(let i=0;i<20;i++){
  for(let j=0;j<16;j++)append(false);calls=0;const result=await buildCheckpoint(env,'s');await compactJournal(env,'s',{approvedFromSequence:backlog+1});maxCalls=Math.max(maxCalls,calls);assert.equal(result.seq,head);position=result.seq;
 }
 assert.equal((await readCheckpointRecord(env,'s','_receipt','legacy-'+backlog)).value.result.seq,backlog);
 assert.equal((await readCheckpointRecord(env,'s','status','controller')).value.seq,head);
 // Future-only compaction can remove multiple-entry ranges without replaying
 // the interval again; its manifest ancestry binds verified input history.
 await compactJournal(env,'s',{approvedFromSequence:backlog+1});const futureRows=sql.prepare('SELECT count(*) n FROM controller_journal WHERE seq>=?').get(backlog+1).n;assert.ok(futureRows<=64);assert.equal(sql.prepare('SELECT count(*) n FROM controller_journal WHERE seq<=?').get(backlog).n,backlog);
 console.log(JSON.stringify({initial_backlog:backlog,new_entries_per_build:16,build_invocations:rounds,final_head:head,final_checkpoint:position,elapsed_seconds:(performance.now()-started)/1000,maximum_entries_advanced_per_build:maximumAdvance,max_service_calls_per_build:maxCalls,archive_puts:puts,archive_deletes:deletes,steady_state_rounds:20,legacy_receipt_preserved:true,retained_future_journal_rows:futureRows}));
}finally{sql.close();rmSync(directory,{recursive:true,force:true});}
