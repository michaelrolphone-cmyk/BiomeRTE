import {test} from 'node:test';
import assert from 'node:assert/strict';
import {consoleMetrics} from '../lib/console-metrics.mjs';
test('host observation uses discovered placement and an immutable read-only command; pending reads preserve their operation',async()=>{
 const calls=[];let phase='queued';const deps={hexHash:async()=> 'a'.repeat(64),discoverPage:async()=>({structuredContent:{items:[{contract:'host.terminal.run:1',state:'ready',resource:'discovered-terminal',controller:'discovered-controller',provider:'provider'}]}}),runTool:async(env,name,args)=>{calls.push(args);return {structuredContent:args.operation==='cap.submit'?{state:'queued'}:{state:phase,result:{exit_code:0,stdout:JSON.stringify({host:'observed-host',observed_at:123,volumes:[],network:[],memory_total_bytes:1024,cpu_count:4,load_average:[1]})}}};}};
 let result=await consoleMetrics({}, {owner:'owner',realm:'site'},deps);assert.equal(result.hosts[0].refreshing,true);assert.equal(result.hosts[0].metrics,null);
 const submit=calls.find(c=>c.operation==='cap.submit');assert.equal(submit.controller,'discovered-controller');assert.equal(submit.arguments.resource,'discovered-terminal');assert.equal(submit.arguments.contract,'host.terminal.run:1');assert.match(submit.arguments.inputs.argv[2],/statvfs/);assert.match(submit.arguments.inputs.argv[2],/received_bytes/);assert.ok(submit.arguments.inputs.argv[2].length<4096);assert.doesNotMatch(submit.arguments.inputs.argv[2],/open\([^\n]*['"]w['"]/);
 phase='succeeded';result=await consoleMetrics({}, {owner:'owner',realm:'site'},deps);assert.equal(result.hosts[0].metrics.host,'observed-host');assert.equal(calls.filter(c=>c.operation==='cap.submit').length,1);assert.equal(result.hosts[0].operation,submit.arguments.operation);
});
