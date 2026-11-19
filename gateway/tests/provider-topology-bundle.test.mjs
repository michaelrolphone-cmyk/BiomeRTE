import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as built from '../dist/server/index.js';
test('deployable worker includes topology module and links it into the real MCP handler',async()=>{
 assert.equal(typeof built.routeProviderInvocation,'function');
 assert.equal(typeof built.nearestController,'function');
 const result=await built.runTool({CONTROLLER_COORDINATION_MODE:'local'},'biome_invoke',{operation:'provider.poll',arguments:{provider:'a'.repeat(64)},request_id:'bundle-check',provider_context:{provider:'a'.repeat(64),host:'invalid host'}});
 assert.equal(result.isError,true);assert.match(result.content[0].text,/Invalid provider topology/);
});
