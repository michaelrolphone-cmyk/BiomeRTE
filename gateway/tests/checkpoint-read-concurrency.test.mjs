import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readCheckpointObjects} from '../lib/journal-checkpoint.mjs';
const hash=s=>createHash('sha256').update(s).digest('hex');
function fixture(values,{invalidLease=false,corrupt=-1,missing=-1,oversized=-1}={}){
 const digests=values.map(hash);let active=0,maximum=0,gets=0,checks=0,renewals=0;
 const env={DB:{prepare(q){let args;return {bind(...a){args=a;return this},async first(){
  if(q.startsWith('UPDATE checkpoint_readers')){renewals++;assert.deepEqual(args,['s','pin','owner']);return invalidLease?null:{token:'pin'};}
  checks++;return args[1]===digests[missing]?null:{refs:1};
 }}}},JOURNAL_ARCHIVE:{async get(key){
  const i=digests.indexOf(key.split('/').pop());gets++;active++;maximum=Math.max(maximum,active);
  return {size:i===oversized?524289:Buffer.byteLength(values[i]),async text(){
   try{await new Promise(r=>setTimeout(r,8+(i%4===0?12:0)));return i===corrupt?'corrupt':values[i];}
   finally{active--;}
  }};
 }}};
 return {env,digests,stats:()=>({active,maximum,gets,checks,renewals})};
}
test('four bounded reads preserve exact input order, duplicates, and hashes',async()=>{
 const f=fixture(Array.from({length:17},(_,i)=>JSON.stringify({i})));
 const ds=[...f.digests,f.digests[0]];
 const r=await readCheckpointObjects(f.env,'s',ds,'pin','owner');
 assert.deepEqual(r.objects.map(o=>o.digest),ds);assert.equal(r.next,null);
 assert.deepEqual(f.stats(),{active:0,maximum:4,gets:18,checks:18,renewals:1});
});
test('byte pagination remains a prefix with at most three speculative reads',async()=>{
 const f=fixture(Array.from({length:12},(_,i)=>String(i)+'x'.repeat(524287)));
 const r=await readCheckpointObjects(f.env,'s',f.digests);
 assert.equal(r.objects.length,4);assert.equal(r.next,4);assert.equal(f.stats().gets,8);
 assert.equal(f.stats().active,0);
 assert.deepEqual(r.objects.map(o=>o.digest),f.digests.slice(0,4));
});
test('lookahead corruption after byte boundary does not poison valid earlier page',async()=>{
 const f=fixture(Array.from({length:12},(_,i)=>String(i)+'x'.repeat(524287)),{corrupt:6});
 const r=await readCheckpointObjects(f.env,'s',f.digests);
 assert.equal(r.next,4);assert.equal(f.stats().active,0);
 await assert.rejects(readCheckpointObjects(f.env,'s',f.digests.slice(4)),/integrity/);
 assert.equal(f.stats().active,0);
});
test('requested corruption and unretained objects fail closed after wave settles',async()=>{
 for(const opts of [{corrupt:1},{missing:2}]){
  const f=fixture(['a','b','c','d','e'],opts);
  await assert.rejects(readCheckpointObjects(f.env,'s',f.digests),/integrity|retained/);
  assert.equal(f.stats().active,0);assert.ok(f.stats().gets<=4);
 }
});
test('invalid lease or page starts no object reads',async()=>{
 const f=fixture(['a'],{invalidLease:true});
 await assert.rejects(readCheckpointObjects(f.env,'s',f.digests,'pin','owner'),/lease/);
 assert.equal(f.stats().gets,0);
 for(const input of [[],Array(129).fill(f.digests[0]),null])
  await assert.rejects(readCheckpointObjects(f.env,'s',input),/page/);
 assert.equal(f.stats().gets,0);
});
