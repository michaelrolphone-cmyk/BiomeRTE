import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {mergeCapabilityCatalog,federatedCatalog,masterDiscovery,routeCapabilityInvocation} from '../lib/capability-map.mjs';
const one='a'.repeat(64),two='b'.repeat(64),p1='c'.repeat(64),p2='d'.repeat(64);
const contract={id:'terminal:1',inputs:{argv:{type:'list',required:true}},outputs:{stdout:{type:'str',required:true}},synchronous:false};
const off=(id,provider,resource,host)=>({id,provider,resource,contract:'terminal:1',state:'ready',capacity:2,configuration:{execution_host:host}});
function fixture(){
 const sql=new DatabaseSync(':memory:');
 sql.exec("CREATE TABLE controller_config(id INTEGER,site TEXT,heartbeat_seconds INTEGER,enabled INTEGER);INSERT INTO controller_config VALUES(1,'s',30,1);CREATE TABLE controller_nodes(id TEXT,enabled INTEGER,ready INTEGER,seen REAL,host TEXT,load REAL,priority INTEGER)");
 for(const id of ['first','second'])sql.prepare('INSERT INTO controller_nodes VALUES(?,1,1,?, ?,0,1)').run(id,Date.now()/1000,id+'.local');
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'first',CONTROLLER_NODE_MAP:JSON.stringify({[one]:'first',[two]:'second'})};
 let calls=0,known=null;
 const read=async(_env,_tool,args)=>{
  calls++;
  if(args.operation==='cap.status')return {content:[{type:'text',text:JSON.stringify(known&&args.controller===known?{id:args.arguments.operation,state:'succeeded',contract:'terminal:1',provider:p2,resource:'r2',inputs:{argv:['hostname']}}:{state:'missing_unknown'})}]};
  const catalog={contracts:[contract],offers:[args.controller===one?off('o1',p1,'r1','one.local'):off('o2',p2,'r2','two.local')]};
  return {content:[{type:'text',text:JSON.stringify(catalog)}]};
 };
 return {sql,env,read,calls:()=>calls,known:v=>known=v};
}
test('one contract merges multiple implementations without machine-specific identities',()=>{
 const r=mergeCapabilityCatalog([{controller:one,catalog:{contracts:[contract],offers:[off('o1',p1,'r1','one')]}},{controller:two,catalog:{contracts:[contract],offers:[off('o2',p2,'r2','two')]}}]);
 assert.equal(r.contracts.length,1);assert.equal(r.contracts[0].implementations.length,2);assert.deepEqual(r.contracts[0].controllers,[one,two]);
});
test('current provider owner excludes old replicas and contract conflicts stay explicit',()=>{
 const ghost=off('o1',p1,'r1','one');
 const r=mergeCapabilityCatalog([{controller:one,catalog:{contracts:[contract],offers:[ghost]}},{controller:two,catalog:{contracts:[{...contract,inputs:{different:{type:'str'}}}],offers:[ghost]}}],[{provider:p1,controller:one}]);
 assert.equal(r.offers.length,1);assert.equal(r.offers[0].controller,one);assert.deepEqual(r.conflicts,['terminal:1']);
});
test('master discovery returns a system map with complete controller coverage',async()=>{
 const f=fixture();const r=await masterDiscovery(f.env,{section:'contracts'},'site',f.read);
 const v=JSON.parse(r.content[0].text);assert.equal(v.scope,'system');assert.equal(v.items.length,1);assert.equal(v.items[0].implementations.length,2);assert.equal(v.coverage.complete,true);f.sql.close();
});
test('system discovery counts whole rows and preserves the limit across snapshot pages',async()=>{
 const f=fixture();
 try{
  const offers=Array.from({length:61},(_,i)=>({
   ...off(String(i).padStart(2,'0'),p1,'r1','one.local'),description:'x'.repeat(16000)
  }));
  const read=async(_env,_tool,args)=>({structuredContent:{contracts:[contract],offers:args.controller===one?offers:[]}});
  const page=async args=>JSON.parse((await masterDiscovery(f.env,args,'site',read)).content[0].text);
  const first=await page({section:'offers'});
  assert.equal(first.items.length,25);
  const second=await page({...first.next_call.arguments,limit:30});
  const last=await page(second.next_call.arguments);
  assert.equal(second.items.length,30);assert.equal(last.items.length,6);assert.equal(last.complete,true);
  assert.deepEqual([...first.items,...second.items,...last.items].map(x=>x.id),offers.map(x=>x.id));
  const legacy=JSON.parse(atob(first.next_cursor.slice(8)));delete legacy.limit;
  assert.equal((await page({cursor:'master1.'+btoa(JSON.stringify(legacy))})).items.length,25);
  const custom=await page({section:'offers',limit:7});
  assert.equal((await page({cursor:custom.next_cursor})).items.length,7);
 }finally{f.sql.close()}
});
test('an offline source is unknown rather than falsely complete',async()=>{
 const f=fixture();f.sql.exec("UPDATE controller_nodes SET ready=0 WHERE id='second'");
 const r=await federatedCatalog(f.env,'snapshot','',f.read);assert.equal(r.coverage.complete,false);assert.equal(r.contracts[0].implementations.length,1);assert.equal(r.coverage.controllers[1].error,'Controller unavailable');f.sql.close();
});
test('request-level host placement selects its owner and preserves the generic inputs',async()=>{
 const f=fixture();const args={operation:'cap.acquire',arguments:{contract:'terminal:1',task:'session',placement:{host:'TWO.LOCAL.'}},request_id:'host-test'};
 const r=await routeCapabilityInvocation(f.env,args,f.read);assert.equal(r.controller,two);assert.equal(r.arguments.contract,args.arguments.contract);assert.equal(r.arguments.task,args.arguments.task);assert.equal(r.arguments.placement.host,args.arguments.placement.host);assert.equal(r.arguments.placement.provider,p2);assert.equal(r.arguments.resource,'r2');f.sql.close();
});
test('simultaneous retries retain selection when availability or topology changes',async()=>{
 const f=fixture();const args={operation:'cap.acquire',arguments:{contract:'terminal:1',task:'session'},request_id:'same'};
 const [a,b]=await Promise.all([routeCapabilityInvocation(f.env,args,f.read),routeCapabilityInvocation(f.env,args,f.read)]);assert.equal(a.controller,b.controller);
 f.sql.exec("UPDATE controller_nodes SET ready=0 WHERE id='first'");const n=f.calls();
 assert.equal((await routeCapabilityInvocation(f.env,args,f.read)).controller,a.controller);assert.equal(f.calls(),n);f.sql.close();
});
test('unavailable constrained placement fails before a mutation is dispatched',async()=>{
 const f=fixture();await assert.rejects(routeCapabilityInvocation(f.env,{operation:'cap.acquire',arguments:{contract:'terminal:1',task:'s',placement:{host:'missing'}},request_id:'missing'},f.read),/No matching/);f.sql.close();
});
test('existing native command evidence wins over a changed provider location',async()=>{
 const f=fixture();f.known(two);
 const r=await routeCapabilityInvocation(f.env,{operation:'cap.submit',arguments:{contract:'terminal:1',operation:'existing',inputs:{argv:['hostname']}},request_id:'recover'},f.read);
 assert.equal(r.controller,two);f.sql.close();
});
test('missing gateway locator cannot replay while another controller is unreachable',async()=>{
 const f=fixture();f.sql.exec("UPDATE controller_nodes SET ready=0 WHERE id='second'");
 await assert.rejects(routeCapabilityInvocation(f.env,{operation:'cap.submit',arguments:{contract:'terminal:1',operation:'unlocated',inputs:{argv:['hostname']}},request_id:'unknown'},f.read),/Cannot rule out/);f.sql.close();
});
test('explicit controller targeting preserves the existing diagnostic interface',async()=>{
 const f=fixture();const args={operation:'cap.acquire',controller:two,arguments:{contract:'terminal:1'},request_id:'explicit'};assert.equal(await routeCapabilityInvocation(f.env,args,f.read),args);assert.equal(f.calls(),0);f.sql.close();
});

test('a changed request cannot reuse a resolved request id',async()=>{
 const f=fixture();const args={operation:'cap.acquire',arguments:{contract:'terminal:1',task:'one'},request_id:'bound'};
 await routeCapabilityInvocation(f.env,args,f.read);
 await assert.rejects(routeCapabilityInvocation(f.env,{...args,arguments:{...args.arguments,task:'other'}},f.read),/different capability request/);f.sql.close();
});
test('automatic selection uses another controller when the default implementation is full',async()=>{
 const f=fixture();const read=async(env,tool,args)=>args.operation==='cap.leases'?{content:[{type:'text',text:JSON.stringify(args.controller===one?[{resource:'r1',state:'bound'},{resource:'r1',state:'fencing'}]:[])}]}:f.read(env,tool,args);
 const r=await routeCapabilityInvocation(f.env,{operation:'cap.acquire',arguments:{contract:'terminal:1',task:'free'},request_id:'capacity'},read);
 assert.equal(r.controller,two);assert.equal(r.arguments.resource,'r2');f.sql.close();
});

test('legacy replicated offers retain the configured default until topology is registered',()=>{
 const ghost=off('same',p1,'legacy','');
 const actual=off('same',p1,'legacy','legacy-host');
 const r=mergeCapabilityCatalog([{controller:one,default:false,catalog:{contracts:[contract],offers:[ghost]}},{controller:two,default:true,catalog:{contracts:[contract],offers:[actual]}}]);
 assert.equal(r.offers[0].controller,two);assert.equal(r.contracts[0].implementations[0].host,'legacy-host');
 const assigned=mergeCapabilityCatalog([{controller:one,default:false,catalog:{contracts:[contract],offers:[ghost]}},{controller:two,default:true,catalog:{contracts:[contract],offers:[actual]}}],[{provider:p1,controller:one,host:'assigned'}]);
 assert.equal(assigned.offers[0].controller,one);
});
