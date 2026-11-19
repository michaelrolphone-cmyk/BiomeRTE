import test from 'node:test';
import assert from 'node:assert/strict';
import {liveAt} from '../lib/recovery.mjs';
test('liveness retains future reads and error-path values but drops overwritten copies',()=>{
 const nodes={before:{type:'service',args:{frame:{$:'frame'}},save:'prepared',next:'map',on_error:'fault'},map:{type:'set',values:{frame:{$:'prepared.frame'}},next:'end'},end:{type:'end',result:{$:'frame'}},fault:{type:'end',result:{error:{$:'error'},previous:{$:'step'}}}};
 assert.deepEqual([...liveAt(nodes).before].sort(),['error','frame','step']);
 assert.equal(liveAt(nodes).before.has('prepared'),false);
});
test('loops preserve a value read before its next overwrite',()=>{
 const nodes={read:{type:'service',args:{prior:{$:'step.value'}},save:'step',next:'choose'},choose:{type:'choose',cases:[{when:{$:'frame.again'},to:'read'}],default:'end'},end:{type:'end',result:{$:'step'}}};
 assert.equal(liveAt(nodes).read.has('step'),true);
 assert.equal(liveAt(nodes).read.has('frame'),true);
});
