import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import gateway from '../worker/index.js';
import {controllerCatalog} from '../lib/discovery.mjs';
import {controllerNetworkSchema,controllerNetworkUpdate,controllerNetworkRegistration} from '../lib/controller-network.mjs';
const origin='https://biome.test', a='a'.repeat(64), b='b'.repeat(64);
const hash=value=>createHash('sha256').update(value).digest('hex');
const address=(ip='192.168.1.55',extra={})=>({address:ip,family:'ipv4',interface:'en1',scope:'private',prefix_length:24,...extra});
const report=rows=>({version:1,addresses:rows});
function fixture(){
 const sql=new DatabaseSync(':memory:');
 for(const migration of ['0000_biome_gateway','0001_controller_ha','0002_bridge_result_chunks','0003_bridge_live_claims'])
  sql.exec(readFileSync('drizzle/'+migration+'.sql','utf8'));
 sql.prepare('INSERT INTO controller_config(id,site,enabled,heartbeat_seconds) VALUES(1,?,1,12)').run('test-site');
 const keys={mac:'1'.repeat(64),pi:'2'.repeat(64)};
 for(const [index,id] of ['mac','pi'].entries())
  sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity) VALUES(?,?,?,4)').run(id,hash(keys[id]),index);
 const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',
  CONTROLLER_NODE_MAP:JSON.stringify({[a]:'mac',[b]:'pi'}),BIOME_OAUTH_ORIGIN:origin,BIOME_OWNER_EMAIL:'owner@test.invalid'};
 const send=(body={},node='mac',headers={},path='/bridge/ha/heartbeat')=>gateway.fetch(new Request(origin+path,{
  method:'POST',headers:{'Content-Type':'application/json',...(node?{Authorization:'Bearer '+keys[node]}:{}),...headers},
  body:JSON.stringify(path==='/bridge/ha/heartbeat'?{position:0,ready:true,load:0,host:node??'unknown',...body}:body)}),env);
 const get=async(node='mac')=>(await (await send({},node,{},'/bridge/ha/status')).json()).nodes.find(value=>value.id===node);
 return {sql,env,send,get};
}
test('address changes replace the authenticated registration and keep stable public route references',async()=>{
 const f=fixture();try{
  let response=await f.send({network:report([address()])},'mac',{'CF-Connecting-IP':'8.8.4.4'});
  assert.equal(response.status,200);
  let node=await f.get();
  assert.deepEqual(node.network.self_report.addresses,[{...address(),network:'192.168.1.0/24'}]);
  assert.equal(node.network.self_report.current,true);
  assert.equal(node.network.gateway_observed.address,'8.8.4.4');
  assert.equal(node.network.gateway_observed.scope,'public');
  assert.equal(node.network.gateway_observed.purpose,'egress_observation');
  assert.equal(node.network.gateway_observed.inbound_reachable,null);
  assert.deepEqual(node.network.route,{controller:a,transport:'outbound_gateway',ready:true,native_call:origin+'/v1/call'});
  await f.send({network:report([address('192.168.1.77')])});
  node=await f.get();assert.deepEqual(node.network.self_report.addresses.map(row=>row.address),['192.168.1.77']);
  assert.equal((await f.get('pi')).network.self_report.status,'unreported');
  const catalog=await controllerCatalog(f.env);
  assert.equal(catalog.find(row=>row.controller===a).network.self_report.addresses[0].address,'192.168.1.77');
 }finally{f.sql.close();}
});
test('fresh readiness cannot refresh omitted, failed or expired interface reports',async()=>{
 const f=fixture();try{
  await f.send({network:report([address()])},'mac',{'CF-Connecting-IP':'8.8.4.4'});
  const old=Date.now()/1000-60;
  f.sql.prepare("UPDATE controller_nodes SET network=json_set(network,'$.self_report.observed_at',?,'$.gateway_observed.observed_at',?) WHERE id='mac'").run(old,old);
  await f.send({},'mac',{'CF-Connecting-IP':'9.9.9.9','X-Forwarded-For':'1.2.3.4'});
  let node=await f.get();
  assert.equal(node.ready,1);assert.equal(node.network.self_report.current,false);
  assert.equal(node.network.self_report.status,'stale');assert.equal(node.network.self_report.observed_at,old);
  assert.equal(node.network.gateway_observed.current,true);assert.equal(node.network.gateway_observed.address,'9.9.9.9');
  await f.send({network:null});node=await f.get();
  assert.equal(node.network.self_report.status,'unavailable');assert.equal(node.network.self_report.observed_at,old);
  assert.equal(node.network.self_report.addresses[0].address,'192.168.1.55');
  await f.send({network:report([])});node=await f.get();
  assert.equal(node.network.self_report.current,true);assert.deepEqual(node.network.self_report.addresses,[]);
 }finally{f.sql.close();}
});
test('a locally unhealthy controller can still report its current network',async()=>{
 const f=fixture();try{
  await f.send({ready:false,network:report([address()])});
  const node=await f.get(),catalog=(await controllerCatalog(f.env)).find(row=>row.controller===a);
  assert.equal(node.ready,0);assert.equal(node.network.self_report.current,true);assert.equal(node.network.route.ready,false);
  assert.equal(catalog.ready,false);assert.equal(catalog.network.self_report.current,true);
 }finally{f.sql.close();}
});
test('network scope and subnet are derived on the server and IPv6 retains its interface zone',async()=>{
 const f=fixture();try{
  const rows=[address('10.1.2.3',{scope:'public',network:'8.8.8.0/24'}),
   address('8.8.8.8',{scope:'private',prefix_length:32}),
   address('100.64.1.2',{scope:'public'}),
   {address:'fe80::B:ff02:b7d4:7051%en1',family:'ipv6',interface:'en1',scope:'link_local',prefix_length:64,network:'fe80::/64',scope_id:14},
   {address:'fd12:3456::9',family:'ipv6',interface:'en1',scope:'public',prefix_length:64},
   {address:'2606:4700:4700::1111',family:'ipv6',interface:'en1',scope:'public',prefix_length:64}];
  assert.equal((await f.send({network:report(rows)})).status,200);
  const byAddress=new Map((await f.get()).network.self_report.addresses.map(row=>[row.address,row]));
  assert.equal(byAddress.get('10.1.2.3').scope,'private');assert.equal(byAddress.get('10.1.2.3').network,'10.1.2.0/24');
  assert.equal(byAddress.get('8.8.8.8').scope,'public');assert.equal(byAddress.get('100.64.1.2').scope,'shared');
  assert.equal(byAddress.get('fe80::b:ff02:b7d4:7051%en1').scope_id,14);
  assert.equal(byAddress.get('fd12:3456::9').scope,'private');assert.equal(byAddress.get('2606:4700:4700::1111').scope,'public');
 }finally{f.sql.close();}
});
test('invalid, non-host and over-limit reports do not mutate node state',async()=>{
 const f=fixture();try{
  for(const rows of [[address('0.0.0.0')],[address('127.0.0.1')],[address('224.0.0.1')],[address('255.255.255.255')],
    [address('192.168.1.55/24')],[address('1.2.3.999')],[address('01.2.3.4')],
    [address('192.168.1.55',{family:'ipv6'})],[address('192.168.1.55',{prefix_length:33})],
    [address('192.168.1.55',{controller:b})],
    [{address:'fe80::1%en2',family:'ipv6',interface:'en1',scope:'link_local'}],
    [{address:'::ffff:127.0.0.1',family:'ipv6',interface:'en1',scope:'public'}],
    Array.from({length:129},()=>address())]){
   assert.equal((await f.send({network:report(rows)})).status,400,JSON.stringify(rows[0]));
  }
  assert.equal(f.sql.prepare("SELECT seen FROM controller_nodes WHERE id='mac'").get().seen,0);
  assert.equal((await f.send({network:{version:2,addresses:[]}})).status,400);
  assert.equal((await f.send({network:{version:1,addresses:[],controller:b}})).status,400);
 }finally{f.sql.close();}
});
test('authenticated ownership cannot be selected by report and anonymous views reveal no private addresses',async()=>{
 const f=fixture();try{
  await f.send({network:report([address()]),controller:'pi'});
  assert.equal((await f.get()).network.self_report.addresses.length,1);
  assert.equal((await f.get('pi')).network.self_report.status,'unreported');
  const anonymous=await f.send({},null,{},'/bridge/ha/status');
  assert.equal(anonymous.status,401);assert.equal((await anonymous.text()).includes('192.168.1.55'),false);
  const rpc={jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'biome_discover',arguments:{section:'controllers'}}};
  const response=await f.send(rpc,null,{},'/api/mcp');
  assert.equal(response.status,401);assert.equal((await response.text()).includes('192.168.1.55'),false);
 }finally{f.sql.close();}
});
test('schema installation is idempotent and network metadata never changes token/owner',async()=>{
 const f=fixture();try{
  await Promise.all([controllerNetworkSchema(f.env.DB),controllerNetworkSchema(f.env.DB),controllerNetworkSchema(f.env.DB)]);
  assert.equal(f.sql.prepare('PRAGMA table_info(controller_nodes)').all().filter(row=>row.name==='network').length,1);
  const before=f.sql.prepare('SELECT id,token_hash,priority,capacity FROM controller_nodes ORDER BY id').all();
  await f.send({network:report([address()])});
  assert.deepEqual(f.sql.prepare('SELECT id,token_hash,priority,capacity FROM controller_nodes ORDER BY id').all(),before);
 }finally{f.sql.close();}
});
test('older overlapping heartbeat cannot replace a newer network observation',async()=>{
 const f=fixture();try{
  await f.send({network:report([address('192.168.1.77')])});
  const later=Date.now()/1000+20;f.sql.prepare("UPDATE controller_nodes SET seen=? WHERE id='mac'").run(later);
  await f.send({network:report([address('192.168.1.55')])});
  const node=await f.get();assert.equal(node.seen,later);assert.equal(node.network.self_report.addresses[0].address,'192.168.1.77');
 }finally{f.sql.close();}
});
test('untrusted source headers and stale gateway observations cannot become a current public IP',async()=>{
 const f=fixture();try{
  await f.send({},'mac',{'X-Forwarded-For':'8.8.4.4','X-Real-IP':'9.9.9.9'});
  assert.equal((await f.get()).network.gateway_observed,null);
  await f.send({},'mac',{'CF-Connecting-IP':'8.8.4.4'});
  const old=Date.now()/1000-60;f.sql.prepare("UPDATE controller_nodes SET network=json_set(network,'$.gateway_observed.observed_at',?) WHERE id='mac'").run(old);
  await f.send({network:report([address()])});
  const value=(await f.get()).network;
  assert.equal(value.self_report.current,true);assert.equal(value.gateway_observed.current,false);assert.equal(value.gateway_observed.observed_at,old);
 }finally{f.sql.close();}
});
test('a disabled registry row and invalid route mapping never yield a current route',()=>{
 const stored=JSON.stringify({self_report:{available:true,observed_at:100,addresses:[address()]}});
 const value=controllerNetworkRegistration({CONTROLLER_NODE_MAP:JSON.stringify({[a]:'mac',[b]:'mac'})},
  {id:'mac',enabled:0,ready:1,seen:100,network:stored},12,101);
 assert.equal(value.self_report.current,false);assert.equal(value.route,null);
});
test('metadata validation is bounded and preserves an omitted snapshot in SQL',()=>{
 const update=controllerNetworkUpdate(new Request(origin),{},100);
 assert.equal(update.expression,"COALESCE(network,'{}')");assert.deepEqual(update.parameters,[]);
 const empty=controllerNetworkUpdate(new Request(origin),{network:report([])},100);
 assert.deepEqual(JSON.parse(empty.parameters[0]),{available:true,observed_at:100,addresses:[]});
});

test('Cloudflare Worker proxy observations never become controller public IPs',async()=>{
 const f=fixture();try{
  await f.send({network:report([address()])},'mac',{'CF-Connecting-IP':'8.8.4.4'});
  await f.send({},'mac',{'CF-Connecting-IP':'2a06:98c0:3600::103'});
  let value=(await f.get()).network.gateway_observed;
  assert.equal(value.address,null);assert.equal(value.proxy_address,'2a06:98c0:3600::103');
  assert.equal(value.purpose,'proxy_observation');assert.equal(value.attribution,'unknown_controller_egress');
  assert.equal(value.current,true);assert.equal(value.inbound_reachable,null);
  await f.send({},'mac',{'CF-Connecting-IP':'9.9.9.9','CF-Worker':'upstream.example'});
  value=(await f.get()).network.gateway_observed;
  assert.equal(value.address,null);assert.equal(value.proxy_address,'9.9.9.9');
  assert.equal(value.via,'cloudflare_worker');
 }finally{f.sql.close();}
});

test('production bundle preserves registration exports and authenticated network reporting',async()=>{
 const built=await import('data:text/javascript;base64,'+Buffer.from(readFileSync('dist/server/index.js','utf8')).toString('base64'));
 for(const name of ['controllerNetworkSchema','controllerNetworkUpdate','controllerNetworkRegistration'])
  assert.equal(typeof built[name],'function',name);
 const f=fixture();try{
  const response=await built.default.fetch(new Request(origin+'/bridge/ha/heartbeat',{
   method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+'1'.repeat(64)},
   body:JSON.stringify({position:0,ready:true,load:0,host:'mac',network:report([address()])})}),f.env);
  assert.equal(response.status,200);
  const node=(await response.json()).nodes.find(value=>value.id==='mac');
  assert.equal(node.network.self_report.current,true);assert.equal(node.network.self_report.addresses[0].address,'192.168.1.55');
 }finally{f.sql.close();}
});

test('IPv6 special-purpose boundaries distinguish public exceptions from documentation and protocol space',()=>{
 const cases=[
  ['64:ff9b::808:808','public'],['64:ff9b:0:0:0:1::1','special'],['64:ff9b:1::1','special'],
  ['2001:1::1','public'],['2001:1::2','public'],['2001:1::3','public'],['2001:1::4','special'],
  ['2001:3:ffff::1','public'],['2001:4:111::1','special'],['2001:4:112::1','public'],['2001:4:113::1','special'],
  ['2001:1f::1','special'],['2001:20::1','public'],['2001:2f:ffff::1','public'],
  ['2001:30::1','public'],['2001:3f:ffff::1','public'],['2001:40::1','special'],
  ['2001:db8::1','special'],['2002:c000:201::1','special'],
  ['3fff::1','special'],['3fff:fff:ffff::1','special'],['3fff:1000::1','public']];
 for(const [ip,scope] of cases){
  const update=controllerNetworkUpdate(new Request(origin),{network:report([{address:ip,family:'ipv6',interface:'en1',scope:'public'}])},100);
  assert.equal(JSON.parse(update.parameters[0]).addresses[0].scope,scope,ip);
 }
});
