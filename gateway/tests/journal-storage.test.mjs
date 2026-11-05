import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {storedJournal,journalText,archiveJournalBatch,journalRecord} from '../lib/journal-storage.mjs';
const hash=s=>createHash('sha256').update(s).digest('hex');
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec('CREATE TABLE controller_journal(site TEXT,seq INTEGER,hash TEXT,entry TEXT,PRIMARY KEY(site,seq))');
 const objects=new Map();
 const env={DB:{prepare(q){const stmt=sql.prepare(q);let args=[];return {bind(...v){args=v;return this},async first(){return stmt.get(...args)},async all(){return {results:stmt.all(...args)}},async run(){return stmt.run(...args)}}}},JOURNAL_ARCHIVE:{async put(k,v){objects.set(k,v)},async get(k){return objects.has(k)?{async text(){return objects.get(k)}}:null}}};
 return {env,sql,objects};
}
test('archival preserves exact numeric serialization, replay bytes and indexed latest record',async()=>{
 const {env,sql}=fixture();
 const text='{"seq":1,"prev":"","changes":[["runs","r",{"x":1.0,"big":9007199254740993,"data":"'+'x'.repeat(10000)+'"}]]}';
 sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',1,hash(text),text);
 const result=await archiveJournalBatch(env,'s');assert.equal(result.moved,1);assert.ok(result.bytes_freed>9000);
 const row=sql.prepare('SELECT * FROM controller_journal').get();assert.equal(row.hash,hash(text));assert.equal(await journalText(env,'s',row),text);
 assert.equal((await journalRecord(env,'s','runs','r')).value.x,1);
 assert.equal((await archiveJournalBatch(env,'s')).moved,0);
 sql.close();
});
test('failed or corrupt object verification leaves the SQL source intact',async()=>{
 const {env,sql}=fixture(),text=JSON.stringify({changes:[['runs','r','x'.repeat(10000)]]});
 sql.prepare('INSERT INTO controller_journal VALUES(?,?,?,?)').run('s',1,hash(text),text);
 env.JOURNAL_ARCHIVE.get=async()=>({text:async()=>'corrupt'});
 await assert.rejects(archiveJournalBatch(env,'s'),/verification failed/);
 assert.equal(sql.prepare('SELECT entry FROM controller_journal').get().entry,text);sql.close();
});
test('object-first write tolerates duplicate attempts and refuses missing/corrupt replay',async()=>{
 const {env,objects}=fixture(),text='{"seq":1,"prev":"","changes":[]}';
 const pointer=await storedJournal(env,'s',text,hash(text));
 assert.equal(await storedJournal(env,'s',text,hash(text)),pointer);
 assert.equal(objects.size,1);
 for(const k of objects.keys())objects.set(k,'corrupt');
 await assert.rejects(journalText(env,'s',{entry:pointer,hash:hash(text)}),/integrity/);
 objects.clear();await assert.rejects(journalText(env,'s',{entry:pointer,hash:hash(text)}),/unavailable/);
});
