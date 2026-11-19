import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {namespaceTree,namespaceAt,descendantCount,mappingFor,mappingKey,graphModel,layoutGraph,nodeStates,resourcePools,overviewStats,escapeHTML,createClient} from '../dashboard/model.mjs';
import {diagramMarkup} from '../dashboard/diagram.mjs';
import {dashboardMappings,dashboardDefinition} from '../lib/dashboard-data.mjs';
const origin='https://biomerte-controller.cantikash.chatgpt.site';
function environment(){
 const db=new DatabaseSync(':memory:');
 db.exec("CREATE TABLE biome_owner(singleton INTEGER PRIMARY KEY,user_id TEXT);INSERT INTO biome_owner VALUES(1,'owner');");
 return {BIOME_OWNER_EMAIL:'owner@example.com',DB:{prepare(sql){const statement=db.prepare(sql);let args=[];return {bind(...values){args=values;return this},async first(){return statement.get(...args)||null},async all(){return {results:statement.all(...args)}},async run(){return statement.run(...args)}}}}};
}
const ownerHeaders={'oai-authenticated-user-id':'owner','oai-authenticated-user-email':'owner@example.com',Origin:origin,'Content-Type':'application/json'};
const req=(path,body,headers=ownerHeaders)=>new Request(origin+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});
test('capability namespace tree preserves exact identities and capabilities on parent namespaces',()=>{
 const contracts=[{id:'agent.chatgpt:1'},{id:'agent.chatgpt.delegate:1'},{id:'agent.chatgpt.tools.read:1'},{id:'agent.grok.delegate:1'}];
 const tree=namespaceTree(contracts);
 assert.equal(descendantCount(tree),4);assert.equal(namespaceAt(tree,'agent.chatgpt').contracts[0].id,'agent.chatgpt:1');
 assert.ok(namespaceAt(tree,'agent.chatgpt.tools').children.has('read'));assert.equal(namespaceAt(tree,'missing'),null);
});
test('friendly name metadata never changes the execution identity',()=>{
 const mappings={[mappingKey('capability','host.terminal.run:1')]:{metadata:{name:'Run a command',summary:'Execute a command'}}};
 const result=mappingFor(mappings,'capability','host.terminal.run:1');
 assert.equal(result.identity,'host.terminal.run:1');assert.equal(result.metadata.name,'Run a command');
});
test('native choices, defaults and parallel branches remain connected in diagram model',()=>{
 const graph=graphModel({graph:{start:'start',nodes:{start:{type:'start',next:'choose'},choose:{type:'choose',cases:[{to:'yes',when:{eq:[1,1]}}],default:'no'},yes:{type:'fork',branches:['a','b']},no:{type:'end'},a:{type:'service',next:'end'},b:{type:'service',next:'end'},end:{type:'end'}}}});
 assert.ok(graph.edges.some(e=>e.from==='choose'&&e.to==='yes'));assert.ok(graph.edges.some(e=>e.from==='choose'&&e.to==='no'));assert.ok(graph.edges.some(e=>e.from==='yes'&&e.to==='b'));
 const layout=layoutGraph(graph);assert.equal(layout.nodes.length,7);assert.ok(layout.width>=760);assert.ok(layout.height>=360);
});
test('run diagram highlights every active token without marking unobserved branches completed',()=>{
 const graph=graphModel({nodes:{start:{type:'start',next:'one'},one:{type:'service'},two:{type:'human'},unused:{type:'end'}}});
 const run={state:'running',tokens:[{node:'one',phase:'working'},{node:'two',state:'waiting'}],trace:[{node:'start',state:'completed'}]};
 assert.deepEqual(nodeStates(run),{start:'completed',one:'active',two:'waiting'});
 const diagram=diagramMarkup(graph,{run,labelFor:n=>n.id});
 assert.match(diagram.html,/flow-node active/);assert.match(diagram.html,/flow-node waiting/);
 assert.equal((diagram.html.match(/flow-node completed/g)||[]).length,1);assert.doesNotMatch(diagram.html,/onerror=/);
});
test('resource capacity is counted once across multiple capability offers on the same resource',()=>{
 const pools=resourcePools({capabilities:{offers:[{resource:'pool',contract:'read',capacity:16,state:'ready'},{resource:'pool',contract:'write',capacity:16,state:'ready'}]},leases:[{resource:'pool',state:'bound'},{resource:'pool',state:'queued'}]});
 assert.equal(pools.length,1);assert.equal(pools[0].capacity,16);assert.equal(pools[0].allocated,1);assert.equal(pools[0].queued,1);
});
test('overview statistics use recorded results and do not invent a completion rate for an empty history',()=>{
 assert.equal(overviewStats({}).rate,null);
 assert.deepEqual(overviewStats({process:{runs:[{state:'completed'},{state:'failed'},{state:'running'}]},waits:[{}]}),{active:1,completed:1,attention:2,rate:50});
 assert.equal(escapeHTML('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');
});
test('mapping names persist, stay workspace-specific, and preserve version-specific step metadata',async()=>{
 const env=environment(),body={realm:'site',kind:'workflow',identity:'feature-change',metadata:{name:'Develop a feature',summary:'Build and test a requested change',steps:{'revision-one:build':{name:'Build software',summary:'Compile the source'}}}};
 let response=await dashboardMappings(req('/dashboard/mappings',body),env);assert.equal(response.status,200);
 response=await dashboardMappings(req('/dashboard/mappings?realm=site'),env);const items=(await response.json()).mappings;
 const saved=items.find(x=>x.kind==='workflow'&&x.identity==='feature-change');assert.equal(saved.metadata.name,'Develop a feature');assert.equal(saved.metadata.steps['revision-one:build'].name,'Build software');
 const local=(await (await dashboardMappings(req('/dashboard/mappings?realm=local'),env)).json()).mappings.find(x=>x.kind==='workflow'&&x.identity==='feature-change');
 assert.notEqual(local?.metadata.name,'Develop a feature custom');
 const custom={...body,metadata:{name:'Develop a feature custom',summary:''}};
 await dashboardMappings(req('/dashboard/mappings',custom),env);
 const site=(await (await dashboardMappings(req('/dashboard/mappings?realm=site'),env)).json()).mappings.find(x=>x.identity==='feature-change'&&x.kind==='workflow');
 assert.equal(site.metadata.name,'Develop a feature custom');
 const separate=(await (await dashboardMappings(req('/dashboard/mappings?realm=local'),env)).json()).mappings.find(x=>x.identity==='feature-change'&&x.kind==='workflow');assert.notEqual(separate?.metadata.name,'Develop a feature custom');
});
test('mapping writes and workflow views require owner identity and same origin',async()=>{
 const env=environment(),body={realm:'site',kind:'workflow',identity:'test',metadata:{name:'Tests',summary:''}};
 assert.equal((await dashboardMappings(req('/dashboard/mappings',body,{}),env)).status,401);
 assert.equal((await dashboardMappings(req('/dashboard/mappings',body,{...ownerHeaders,Origin:'https://other.example'}),env)).status,403);
 assert.equal((await dashboardDefinition(req('/dashboard/definition',{realm:'site',mode:'summaries'},{}),env)).status,401);
 assert.equal((await dashboardDefinition(req('/dashboard/definition',{realm:'site',mode:'summaries'},{...ownerHeaders,Origin:'https://other.example'}),env)).status,403);
 assert.equal((await dashboardDefinition(req('/dashboard/definition',{realm:'site',name:'test',revision:'../../secret'}),env)).status,400);
});
test('metadata rejects excess fields and cannot redirect calls or replace an underlying identity',async()=>{
 const env=environment();
 for(const metadata of [{name:'',summary:''},{name:'x'.repeat(81),summary:''},{name:'Name',summary:'',operation:'other'}]){
  assert.equal((await dashboardMappings(req('/dashboard/mappings',{realm:'site',kind:'workflow',identity:'test',metadata}),env)).status,400);
 }
 assert.equal((await dashboardMappings(req('/dashboard/mappings',{realm:'site',kind:'unrecognized',identity:'test',metadata:{name:'Title'}}),env)).status,400);
});
test('controller retry preserves the exact original mutation request ID and body',async()=>{
 const bodies=[];const client=createClient(async(path,options)=>{bodies.push(options.body);return {status:bodies.length===1?504:200,ok:bodies.length>1,json:async()=>bodies.length===1?{error:'pending'}:{result:'done'}}});
 assert.equal(await client('work.create',{title:'Example'}),'done');assert.equal(bodies[0],bodies[1]);
});

import vm from 'node:vm';
import {readFileSync} from 'node:fs';
test('delegated interface actions receive the real click event',async()=>{
 const code=readFileSync(new URL('../dashboard/ui.js',import.meta.url),'utf8');
 const a=code.indexOf('function action(fn)'),b=code.indexOf('function openModal(',a);
 const context={say(){},toast(){}};vm.createContext(context);vm.runInContext(code.slice(a,b),context);
 let received=null,prevented=false;
 const handler=vm.runInContext('action',context)(async event=>{received=event;});
 const event={target:{dataset:{action:'workflow'}},preventDefault(){prevented=true;}};
 await handler(event);assert.equal(received,event);assert.equal(prevented,true);
});
