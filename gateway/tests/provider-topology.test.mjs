import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {nearestController,routeProviderInvocation,rememberProviderInvocation,validateProviderContext} from '../lib/provider-topology.mjs';
const a='a'.repeat(64),b='b'.repeat(64),p='c'.repeat(64),now=Date.now()/1000;
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec("CREATE TABLE controller_config(id INTEGER,site TEXT,heartbeat_seconds INTEGER);INSERT INTO controller_config VALUES(1,'s',30);CREATE TABLE controller_nodes(id TEXT,enabled INTEGER,ready INTEGER,seen REAL,host TEXT,load REAL,priority INTEGER)");
 for(const row of [['near',1,1,now,'host-one.local',.7,5],['far',1,1,now,'host-two',.1,1]])sql.prepare('INSERT INTO controller_nodes VALUES(?,?,?,?,?,?,?)').run(...row);
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'far',CONTROLLER_NODE_MAP:JSON.stringify({[a]:'near',[b]:'far'})};
 const context={provider:p,host:'host-one'};
 const call=(operation,arguments_={},provider_context=context)=>({operation,arguments:arguments_,request_id:'test',...(provider_context?{provider_context}:{})});
 return {sql,env,context,call};
}
test('gateway chooses co-located controller over configured default, priority and load',async()=>{
 const f=fixture();const r=await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p,contract:'x:1',resource:'device'}));assert.equal(r.args.controller,a);assert.equal(r.placement.basis,'same_host');assert.equal('provider_context' in r.args,false);f.sql.close();
});
test('registration chooses available fallback when co-located controller is offline',async()=>{
 const f=fixture();f.sql.exec("UPDATE controller_nodes SET ready=0 WHERE id='near'");const r=await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p}));assert.equal(r.args.controller,b);assert.equal(r.placement.basis,'available_controller_unknown_distance');f.sql.close();
});
test('stale heartbeat cannot win placement and no ready controller gives explicit error',async()=>{
 const f=fixture();f.sql.exec("UPDATE controller_nodes SET seen=0 WHERE id='near'");assert.equal((await routeProviderInvocation(f.env,f.call('provider.poll',{provider:p}))).args.controller,b);f.sql.exec('UPDATE controller_nodes SET ready=0');await assert.rejects(routeProviderInvocation(f.env,f.call('cap.offer',{provider:p})),e=>e.status===503);f.sql.close();
});
test('provider context cannot pin a controller or change provider identity',async()=>{
 const f=fixture();await assert.rejects(routeProviderInvocation(f.env,{...f.call('provider.poll',{provider:p}),controller:b}),e=>e.status===400);await assert.rejects(routeProviderInvocation(f.env,f.call('provider.poll',{provider:a})),e=>e.status===403);assert.throws(()=>validateProviderContext({provider:p,host:'host',controller:a}));f.sql.close();
});
test('caller acquisition, lease and action continuations follow server-assigned provider owner',async()=>{
 const f=fixture();const offer=f.call('cap.offer',{provider:p,contract:'x:1',resource:'device'});const reg=await routeProviderInvocation(f.env,offer);await rememberProviderInvocation(f.env,offer,reg,{content:[{type:'text',text:'{}'}]});
 const acquire=f.call('cap.acquire',{contract:'x:1',resource:'device'},null);const routed=await routeProviderInvocation(f.env,acquire);assert.equal(routed.args.controller,a);
 await rememberProviderInvocation(f.env,acquire,routed,{content:[{type:'text',text:'{"id":"lease"}'}]});
 const invoke=f.call('cap.invoke',{lease:'lease',operation:'effect'},null);const owned=await routeProviderInvocation(f.env,invoke);assert.equal(owned.args.controller,a);await rememberProviderInvocation(f.env,invoke,owned,{content:[{type:'text',text:'{"id":"effect","state":"queued"}'}]});assert.equal((await routeProviderInvocation(f.env,f.call('cap.status',{operation:'effect'},null))).args.controller,a);f.sql.close();
});
test('new attachment restores only current registration after assigned controller becomes unavailable',async()=>{
 const f=fixture();const offer=f.call('cap.offer',{provider:p,contract:'x:1',resource:'device'});let routed=await routeProviderInvocation(f.env,offer);await rememberProviderInvocation(f.env,offer,routed,{content:[{type:'text',text:'{}'}]});f.sql.exec("UPDATE controller_nodes SET ready=0 WHERE id='near'");const replay=[];routed=await routeProviderInvocation(f.env,f.call('provider.poll',{provider:p}),async(...a)=>replay.push(a));assert.equal(routed.args.controller,b);assert.equal(routed.placement.epoch,2);assert.equal(replay.length,1);assert.deepEqual(replay[0][1],offer.arguments);f.sql.close();
});

test('legacy provider-ID poll and fence calls follow gateway assignment without a client selector',async()=>{
 const f=fixture();await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p,contract:'x:1',resource:'device'}));
 for(const op of ['provider.poll','provider.claim','provider.result','cap.fenced','agent.progress']){
  const r=await routeProviderInvocation(f.env,f.call(op,{provider:p},null));assert.equal(r.args.controller,a);assert.equal('provider_context' in r.args,false);
 }
 f.sql.close();
});

test('completion and recovery stay at dispatch owner after provider placement changes',async()=>{
 const f=fixture();try{
  await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p}));
  const invoke=f.call('cap.invoke',{lease:'old-lease',operation:'old-effect'},null);
  await rememberProviderInvocation(f.env,invoke,{args:{...invoke,controller:a},site:'s'},{structuredContent:{id:'old-effect'}});
  f.sql.prepare('UPDATE provider_placements SET controller=?').run(b);
  for(const op of ['provider.result','provider.claim','provider.custody','provider.reconcile','cap.status','cap.heartbeat','cap.command.state','agent.progress']){
   const result=await routeProviderInvocation(f.env,f.call(op,{provider:p,operation:'old-effect'}));
   assert.equal(result.args.controller,a,op);
  }
  assert.equal((await routeProviderInvocation(f.env,f.call('provider.poll',{provider:p}))).args.controller,b);
  await assert.rejects(routeProviderInvocation(f.env,{...f.call('provider.result',{provider:p,operation:'old-effect'},null),controller:b}),e=>e.status===409);
 }finally{f.sql.close()}
});

test('poll results establish immutable expiring action and fence locators, including explicit controller calls',async()=>{
 const f=fixture();try{
  const original={...f.call('provider.poll',{provider:p},null),controller:a};
  const routed=await routeProviderInvocation(f.env,original);
  await rememberProviderInvocation(f.env,original,routed,{structuredContent:{actions:[{id:'dispatched',lease:'held'}],fences:[{id:'fence'}]}});
  const rows=f.sql.prepare('SELECT * FROM provider_dispatch_routes ORDER BY kind,id').all();
  assert.equal(rows.length,3);
  const expires=rows.map(x=>x.expires);
  await rememberProviderInvocation(f.env,original,routed,{structuredContent:{actions:[{id:'dispatched',lease:'held'}],fences:[{id:'fence'}]}});
  assert.deepEqual(f.sql.prepare('SELECT expires FROM provider_dispatch_routes ORDER BY kind,id').all().map(x=>x.expires),expires);
  await assert.rejects(rememberProviderInvocation(f.env,original,{...routed,args:{...original,controller:b}},{structuredContent:{actions:[{id:'dispatched',lease:'held'}]}}),e=>e.status===409);
  assert.equal((await routeProviderInvocation(f.env,f.call('provider.result',{provider:p,operation:'dispatched'},null))).args.controller,a);
 }finally{f.sql.close()}
});

test('a missing recovery read cannot invent dispatch ownership',async()=>{
 const f=fixture();try{
  const call={...f.call('cap.status',{operation:'expired',recovery:true},null),controller:b};
  const routed=await routeProviderInvocation(f.env,call);
  await rememberProviderInvocation(f.env,call,routed,{structuredContent:{id:'expired',state:'missing_unknown',acknowledged:false}});
  assert.equal(f.sql.prepare("SELECT count(*) n FROM provider_dispatch_routes WHERE kind='operation' AND id='expired'").get().n,0);
 }finally{f.sql.close()}
});

test('same-owner registration refreshes changed and removed IPs without changing epoch or pinned dispatch routes',async()=>{
 const f=fixture();try{
  const first={provider:p,host:'host-one',addresses:['192.168.1.20']};
  await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p,contract:'x:1',resource:'device'},first));
  const invoke=f.call('cap.invoke',{lease:'held',operation:'effect'},null);
  await rememberProviderInvocation(f.env,invoke,{args:{...invoke,controller:a},site:'s'},{structuredContent:{id:'effect'}});
  f.sql.prepare('UPDATE provider_placements SET updated=1').run();
  const changed={provider:p,host:'host-one.local',addresses:['192.168.1.30']};
  const routed=await routeProviderInvocation(f.env,f.call('provider.poll',{provider:p},changed));
  let row=f.sql.prepare('SELECT * FROM provider_placements').get();
  assert.equal(routed.args.controller,a);assert.equal(row.controller,a);assert.equal(row.epoch,1);
  assert.equal(row.host,'host-one.local');assert.deepEqual(JSON.parse(row.addresses),['192.168.1.30']);assert.ok(row.updated>1);
  const observed=row.updated;
  await routeProviderInvocation(f.env,f.call('provider.poll',{provider:p},null));
  assert.equal(f.sql.prepare('SELECT updated FROM provider_placements').get().updated,observed);
  await routeProviderInvocation(f.env,f.call('provider.result',{provider:p,operation:'effect'},{...changed,addresses:[]}));
  row=f.sql.prepare('SELECT * FROM provider_placements').get();
  assert.deepEqual(JSON.parse(row.addresses),[]);assert.equal(row.epoch,1);
  assert.equal(f.sql.prepare("SELECT controller FROM provider_dispatch_routes WHERE kind='operation' AND id='effect'").get().controller,a);
  await assert.rejects(routeProviderInvocation(f.env,{...f.call('provider.poll',{provider:p},changed),controller:b}),error=>error.status===400);
  await assert.rejects(routeProviderInvocation(f.env,f.call('provider.poll',{provider:b},changed)),error=>error.status===403);
 }finally{f.sql.close();}
});

test('rejected placement or dispatch selection cannot refresh provider network metadata',async()=>{
 const f=fixture();try{
  await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p},{provider:p,host:'host-one',addresses:['192.168.1.20']}));
  const invoke=f.call('cap.invoke',{lease:'held',operation:'effect'},null);
  await rememberProviderInvocation(f.env,invoke,{args:{...invoke,controller:a},site:'s'},{structuredContent:{id:'effect'}});
  const before=f.sql.prepare('SELECT * FROM provider_placements').get();
  const changed={provider:p,host:'host-two',addresses:['192.168.1.30']};
  await assert.rejects(routeProviderInvocation(f.env,{...f.call('provider.poll',{provider:p},changed),controller:b}),error=>error.status===400);
  await assert.rejects(routeProviderInvocation(f.env,{...f.call('provider.result',{provider:p,operation:'effect'},changed),controller:b}),error=>error.status===409);
  await assert.rejects(routeProviderInvocation(f.env,f.call('provider.poll',{provider:b},changed)),error=>error.status===403);
  assert.deepEqual(f.sql.prepare('SELECT * FROM provider_placements').get(),before);
 }finally{f.sql.close();}
});

test('an older same-owner observation cannot replace newer registered network metadata',async()=>{
 const f=fixture();try{
  await routeProviderInvocation(f.env,f.call('cap.offer',{provider:p},{provider:p,host:'host-one',addresses:['192.168.1.30']}));
  f.sql.prepare('UPDATE provider_placements SET updated=?').run(Date.now()/1000+60);
  const before=f.sql.prepare('SELECT * FROM provider_placements').get();
  const result=await routeProviderInvocation(f.env,f.call('provider.poll',{provider:p},
   {provider:p,host:'host-one.local',addresses:['192.168.1.20']}));
  assert.equal(result.args.controller,a);
  assert.deepEqual(f.sql.prepare('SELECT * FROM provider_placements').get(),before);
 }finally{f.sql.close();}
});
