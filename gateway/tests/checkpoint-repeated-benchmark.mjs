// Repeated ~600KB transactions, one changed record or UNIQUE=1 all changed.
// DELAY_MS adds latency to every D1/R2 operation. BASELINE may name a module URL.
// Run: DELAY_MS=10 node --max-old-space-size=64 tests/checkpoint-repeated-benchmark.mjs
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const {buildCheckpoint,readCheckpointRecord}=await import(process.env.BASELINE||'../lib/journal-checkpoint.mjs');
const directory=mkdtempSync(join(tmpdir(),'checkpoint-throughput-')),sql=new DatabaseSync(join(directory,'db.sqlite'));
sql.exec('PRAGMA journal_mode=WAL;PRAGMA synchronous=OFF;CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq));CREATE TABLE objects(key TEXT PRIMARY KEY,value TEXT)');
const hash=s=>createHash('sha256').update(s).digest('hex');const delay=()=>new Promise(r=>setTimeout(r,Number(process.env.DELAY_MS??0)));let calls=0,maxCalls=0,head=0,previous='',puts=0,deletes=0;
const env={DB:{prepare(q){const stmt=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){calls++;await delay();return stmt.get(...args)},async all(){calls++;await delay();return {results:stmt.all(...args)}},async run(){calls++;await delay();return stmt.run(...args)}}}},JOURNAL_ARCHIVE:{async put(k,v){calls++;await delay();puts++;sql.prepare('INSERT OR REPLACE INTO objects VALUES(?,?)').run(k,v)},async get(k){calls++;await delay();const row=sql.prepare('SELECT value FROM objects WHERE key=?').get(k);return row?{size:Buffer.byteLength(row.value),async text(){return row.value}}:null},async delete(keys){calls++;await delay();for(const k of Array.isArray(keys)?keys:[keys]){deletes++;sql.prepare('DELETE FROM objects WHERE key=?').run(k)}}}};

function append(changes){const seq=++head,text=JSON.stringify({seq,prev:previous,changes});previous=hash(text);sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',seq,previous,text);}
const rows=Array.from({length:600},(_,i)=>['records','id'+i,{id:i,payload:'x'.repeat(950),version:0}]);
try{
 append(rows);let pages=0;while((await buildCheckpoint(env,'s')).seq<1){if(++pages>100)throw Error('seed stalled');}
 calls=0;puts=0;const started=performance.now();let measuredPages=0,maxPageCalls=0,skipped=0,reads=0;
 for(let n=1;n<=3;n++){
  if(process.env.UNIQUE)for(const row of rows)row[2].version=n;else rows[n][2].version=n;append(rows);let result;
  do{const before=calls;result=await buildCheckpoint(env,'s');measuredPages++;maxPageCalls=Math.max(maxPageCalls,calls-before);assert.ok(calls-before<1000,'service call bound exceeded');skipped+=result.unchanged??0;reads+=result.lookup?.reads??0;assert.ok((result.lookup?.bytes??0)<=1048576);assert.ok((result.lookup?.reads??0)<=96);if(measuredPages>200)throw Error('stalled');}while(result.seq<head);
 }
 for(let n=1;n<=3;n++)assert.equal((await readCheckpointRecord(env,'s','records','id'+n)).value.version,process.env.UNIQUE?3:n);
 console.log(JSON.stringify({baseline:!!process.env.BASELINE,unique:!!process.env.UNIQUE,entries:3,records_per_entry:600,entry_bytes:Buffer.byteLength(JSON.stringify({changes:rows})),pages:measuredPages,service_calls:calls,max_page_calls:maxPageCalls,puts,skipped,lookup_reads:reads,latency_ms_per_call:Number(process.env.DELAY_MS??0),elapsed_seconds:(performance.now()-started)/1000}));
}finally{sql.close();rmSync(directory,{recursive:true,force:true});}
