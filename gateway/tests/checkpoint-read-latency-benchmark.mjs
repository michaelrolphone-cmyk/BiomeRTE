import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readCheckpointObjects,readCheckpointObject} from '../lib/journal-checkpoint.mjs';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const values=Array.from({length:128},(_,i)=>JSON.stringify({i,data:'x'.repeat(1024)}));
const digests=values.map(v=>createHash('sha256').update(v).digest('hex'));
async function run(parallel){
 let active=0,maxActive=0,db=0,r2=0;
 const env={DB:{prepare(){return {bind(){return this},async first(){db++;await pause(3);return {refs:1}}}}},
 JOURNAL_ARCHIVE:{async get(key){r2++;active++;maxActive=Math.max(maxActive,active);await pause(8);
 const i=digests.indexOf(key.split('/').pop());return {size:values[i].length,async text(){await pause(4);active--;return values[i]}}}}};
 const start=performance.now();
 let result;
 if(parallel) result=await readCheckpointObjects(env,'s',digests);
 else{const objects=[];for(const d of digests)objects.push(await readCheckpointObject(env,'s',d));result={objects,next:null};}
 assert.deepEqual(result.objects.map(o=>o.digest),digests);assert.equal(active,0);
 return {elapsed_ms:Math.round(performance.now()-start),db_calls:db,r2_calls:r2,max_active_r2:maxActive,result};
}
const before=await run(false),after=await run(true);
assert.deepEqual(before.result,after.result);
delete before.result;delete after.result;
console.log(JSON.stringify({fixture:'128 x 1KiB; synthetic D1 3ms + R2 get 8ms + body 4ms',before,after,speedup:before.elapsed_ms/after.elapsed_ms}));
