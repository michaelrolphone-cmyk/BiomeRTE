import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {sqliteD1} from './sqlite-d1.mjs';
import {providerInboxReply,providerJournalSignal,providerSocket} from '../lib/transport.mjs';

const value=body=>({content:[{type:'text',text:JSON.stringify(body)}]});
const empty=value({actions:[],fences:[],activations:[]});
const work=value({actions:[{id:'original-native-action'}],fences:[],activations:[]});
function fixture(){
 const h={time:0,head:'site:1:hash',open:true,authorized:true,calls:[],signals:[],auth:[],sleeps:[],sequence:0,
  args:{operation:'provider.poll',arguments:{provider:'provider',limit:3},realm:'site',request_id:'original-read-id'}};
 h.deps={
  active:()=>h.open,now:()=>h.time,requestId:()=> 'next-read-'+(++h.sequence),
  invoke:async args=>{const i=h.calls.length;h.calls.push({args,at:h.time});return h.onInvoke?h.onInvoke(i,args):i===0?empty:work},
  signal:async(args,budget)=>{const i=h.signals.length;h.signals.push({at:h.time,budget,args});return h.onSignal?h.onSignal(i,args,budget):h.head},
  authorize:async()=>{h.auth.push(h.time);return h.onAuth?h.onAuth(h.auth.length):h.authorized},
  sleep:async ms=>{h.sleeps.push(ms);h.time+=ms;await h.onSleep?.(ms)}
 };
 return h;
}
function journalDB(){
 const sql=new DatabaseSync(':memory:');
 for(const name of ['0000_biome_gateway.sql','0001_controller_ha.sql'])sql.exec(readFileSync('drizzle/'+name,'utf8'));
 const queries=[],base=sqliteD1(sql);
 const DB={...base,prepare(query){queries.push(query);return base.prepare(query)}};
 return {sql,DB,queries};
}

test('unchanged shared journal makes no additional native poll during the five-second idle budget',async()=>{
 const h=fixture(),original=structuredClone(h.args);
 h.onSleep=()=>assert.equal(h.calls.length,1);
 const reply=await providerInboxReply(h.args,h.deps);
 assert.strictEqual(reply,work);assert.deepEqual(h.calls.map(c=>c.at),[0,5000]);
 assert.strictEqual(h.calls[0].args,h.args);assert.deepEqual(h.args,original);
 assert.deepEqual(h.calls[1].args,{...original,request_id:'next-read-1'});
 assert.equal(h.sleeps.length,20);assert.ok(h.sleeps.every(ms=>ms<=250));
 assert.ok(h.auth.length>=22,'wait, dispatch and delivery each recheck authorization');
});

test('a journal advance before dispatch or during an empty native reply is not lost',async()=>{
 for(const phase of ['before-dispatch','during-reply']){
  const h=fixture();
  h.onAuth=n=>{if(phase==='before-dispatch'&&n===1)h.head='site:2:next';return true};
  h.onInvoke=async i=>{if(i===0){if(phase==='during-reply'){h.time+=12000;h.head='site:2:next'}return empty}return work};
  assert.strictEqual(await providerInboxReply(h.args,h.deps),work);
  assert.deepEqual(h.calls.map(c=>c.at),phase==='before-dispatch'?[0,250]:[0,12250]);
 }
});

test('every subsequent native poll captures its own baseline',async()=>{
 const h=fixture();h.onInvoke=i=>{if(i===0){h.head='site:2:next';return empty}return i===1?empty:work};
 assert.strictEqual(await providerInboxReply(h.args,h.deps),work);
 assert.deepEqual(h.calls.map(c=>c.at),[0,250,5250]);
 assert.deepEqual(h.calls.map(c=>c.args.request_id),['original-read-id','next-read-1','next-read-2']);
});

test('the idle budget starts after a slow native reply and uses monotonic rather than wall time',async t=>{
 const h=fixture();delete h.deps.now;
 t.mock.method(performance,'now',()=>h.time);
 let wall=1794240000000;t.mock.method(Date,'now',()=>wall);
 h.onInvoke=i=>{if(i===0){h.time+=20000;return empty}return work};
 h.onSleep=()=>{wall=wall>0?-900000000000:900000000000};
 assert.strictEqual(await providerInboxReply(h.args,h.deps),work);
 assert.deepEqual(h.calls.map(c=>c.at),[0,25000]);
});

test('unknown signals and hint errors retain the existing 250 ms native polling fallback',async()=>{
 for(const missing of [null,undefined,'',new Error('Hint unavailable')]){
  const h=fixture();h.onSignal=()=>{if(missing instanceof Error)throw missing;return missing};
  assert.strictEqual(await providerInboxReply(h.args,h.deps),work);
  assert.deepEqual(h.calls.map(c=>c.at),[0,250]);
 }
 const h=fixture();h.onSignal=i=>{if(i===1)throw Error('Head read failed after empty reply');return h.head};
 assert.strictEqual(await providerInboxReply(h.args,h.deps),work);assert.deepEqual(h.calls.map(c=>c.at),[0,250]);
});

test('close and authorization revocation during each awaited stage prevent further native reads or delivery',async()=>{
 const cases=[
  h=>{h.onSignal=()=>{h.open=false;return h.head}},
  h=>{h.onAuth=()=>{h.open=false;return true}},
  h=>{h.authorized=false},
  h=>{h.onSleep=()=>{h.open=false}},
  h=>{h.onSleep=()=>{h.authorized=false}},
  h=>{h.onSignal=i=>{if(i===1)h.open=false;return h.head}},
  h=>{h.onInvoke=()=>{h.open=false;return work}},
  h=>{h.onInvoke=()=>{h.authorized=false;return work}}
 ];
 for(const setup of cases){
  const h=fixture();setup(h);
  assert.equal(await providerInboxReply(h.args,h.deps),null);
  assert.ok(h.calls.length<=1,'no repeated native poll after close/revocation');
 }
});

test('nonempty native work and native errors are returned unchanged without waiting or synthesizing results',async()=>{
 for(const result of [work,value({actions:[],fences:[{id:'native-fence'}],activations:[]}),
  value({actions:[],fences:[],activations:[],streams:[{id:'native-stream',epoch:'current-epoch'}]}),
  value({actions:[],fences:[],activations:[{id:'native-activation'}]}),
  {isError:true,content:[{type:'text',text:'Native permission denied'}]}]){
  const h=fixture();h.onInvoke=()=>result;
  assert.strictEqual(await providerInboxReply(h.args,h.deps),result);
  assert.equal(h.calls.length,1);assert.equal(h.sleeps.length,0);assert.equal(h.auth.length,2);
 }
});

test('only enabled site journals provide a cheap indexed sequence/hash hint',async()=>{
 const f=journalDB();try{
  assert.equal(await providerJournalSignal({DB:f.DB},{realm:'local'}),null);assert.equal(f.queries.length,0);
  assert.equal(await providerJournalSignal({DB:f.DB},{realm:'site'}),null);
  f.sql.prepare("INSERT INTO controller_config(id,site,enabled) VALUES(1,'site',0)").run();
  assert.equal(await providerJournalSignal({DB:f.DB},{}),null);
  f.sql.prepare('UPDATE controller_config SET enabled=1').run();
  const initial=await providerJournalSignal({DB:f.DB},{});assert.equal(initial,JSON.stringify(['site',0,'']));
  f.sql.prepare("INSERT INTO controller_journal(site,seq,hash,entry) VALUES('site',1,'hash-a','PRIVATE-JOURNAL-BODY')").run();
  const first=await providerJournalSignal({DB:f.DB},{});assert.equal(first,JSON.stringify(['site',1,'hash-a']));
  f.sql.prepare("UPDATE controller_journal SET hash='hash-b'").run();
  assert.notEqual(await providerJournalSignal({DB:f.DB},{}),first);
  assert.doesNotMatch(f.queries[0],/checkpoint|\bentry\b|SELECT \*/i);
  const plan=f.sql.prepare('EXPLAIN QUERY PLAN '+f.queries[0]).all().map(row=>row.detail).join('\n');
  assert.doesNotMatch(plan,/SCAN (?:j|controller_journal)\b/);
  const h=fixture();h.args.realm='local';h.deps.signal=(args,budget)=>providerJournalSignal({DB:f.DB},args,budget);
  assert.strictEqual(await providerInboxReply(h.args,h.deps),work);assert.deepEqual(h.calls.map(c=>c.at),[0,250]);
 }finally{f.sql.close()}
});

test('slow or failed journal hints are bounded and cannot indefinitely hold the provider',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 let finish;
 const slow={DB:{prepare(){return {first:()=>new Promise(resolve=>{finish=resolve})}}}};
 const read=providerJournalSignal(slow,{},1000);await Promise.resolve();
 t.mock.timers.tick(999);let done=false;read.then(()=>{done=true});await Promise.resolve();assert.equal(done,false);
 t.mock.timers.tick(1);assert.equal(await read,null);
 finish({site:'site',enabled:1,seq:2,hash:'late'});await Promise.resolve();
 const failed={DB:{prepare(){throw Error('Database unavailable')}}};
 assert.equal(await providerJournalSignal(failed,{}),null);
 const shorter=providerJournalSignal(slow,{},100);await Promise.resolve();t.mock.timers.tick(100);assert.equal(await shorter,null);
});

test('non-poll socket requests preserve the original auth and native dispatch path without journal reads',async t=>{
 const f=journalDB(),NativeResponse=globalThis.Response,NativePair=globalThis.WebSocketPair;
 let server,delivered;
 const reply=new Promise(resolve=>{delivered=resolve});
 class Socket{
  events=new Map();accept(){}
  addEventListener(name,fn){this.events.set(name,fn)}
  send(text){delivered(JSON.parse(text))}
  close(){this.events.get('close')?.({})}
  message(data){this.events.get('message')?.({data:JSON.stringify(data)})}
 }
 globalThis.WebSocketPair=class{constructor(){this[0]=new Socket();this[1]=server=new Socket()}};
 globalThis.Response=class{constructor(body,init){if(init?.status===101){Object.assign(this,init);return}return new NativeResponse(body,init)}static json(...args){return NativeResponse.json(...args)}};
 t.after(()=>{globalThis.Response=NativeResponse;if(NativePair===undefined)delete globalThis.WebSocketPair;else globalThis.WebSocketPair=NativePair;f.sql.close()});
 const base='https://biome.test',token='r'.repeat(43),queries=[];
 f.sql.prepare("INSERT INTO biome_owner VALUES(1,'owner')").run();
 f.sql.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) VALUES(?,'access','grant','client','owner',?,?)")
  .run(createHash('sha256').update(token).digest('base64url'),base+'/api/mcp',Date.now()/1000+60);
 const native={content:[{type:'text',text:JSON.stringify({state:'succeeded',id:'same-native-result'})}]};
 const DB={prepare(query){
  queries.push(query);const statement=f.DB.prepare(query);
  if(query.includes('INSERT OR IGNORE INTO bridge_jobs')){
   const run=statement.run.bind(statement);statement.run=async()=>{
    const result=await run();f.sql.prepare("UPDATE bridge_jobs SET state='done',result=? WHERE state='queued'").run(JSON.stringify(native));return result;
   };
  }
  return statement;
 }};
 const env={DB,BIOME_OAUTH_ORIGIN:base};
 const response=await providerSocket(new Request(base+'/api/provider/connect',{headers:{Upgrade:'websocket',Authorization:'Bearer '+token}}),env,env);
 assert.equal(response.status,101);
 const args={operation:'provider.result',arguments:{provider:'p',operation:'original-action',generation:1,state:'succeeded',result:{mask:0}},request_id:'original-mutation',realm:'site'};
 server.message({type:'invoke',id:'socket-call',arguments:args});
 const frame=await reply;assert.deepEqual(frame,{type:'reply',id:'socket-call',result:native});
 assert.equal(queries.filter(query=>query.includes("kind='access'")).length,2,'handshake and existing frame auth only');
 assert.ok(!queries.some(query=>query.includes('controller_journal')));
 const stored=JSON.parse(f.sql.prepare('SELECT request FROM bridge_jobs').get().request);assert.deepEqual(stored.arguments,args);
 server.close();
});
