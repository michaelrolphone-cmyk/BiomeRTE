import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {ownershipSchema,ownershipTransfer,resolveOwnershipRoute} from '../lib/ownership-transfer.mjs';
import {execFileSync} from 'node:child_process';

const A='a'.repeat(64),B='b'.repeat(64),C='c'.repeat(64),AUTH='d'.repeat(64),SNAP='e'.repeat(64),SIGN='f'.repeat(64);
const routes=['resource:disk:one','1'.repeat(64)];
const adoption=(extra={})=>({unit:'domain-one',expected_epoch:0,owner:A,authority_digest:AUTH,aliases:routes,snapshot_digest:SNAP,local_authority:true,durable:true,...extra});
const transfer=(extra={})=>({unit:'domain-one',epoch:1,owner:A,target:B,transfer:'move-one',authority_digest:AUTH,scope:[['work','work-one'],['grants','grant-one']],aliases:routes,snapshot_digest:SNAP,authorization_digest:SIGN,expires:100,...extra});
async function fixture(t,{adopt=true,options={}}={}){
 const sql=new DatabaseSync(':memory:');t.after(()=>sql.close());
 sql.exec("CREATE TABLE controller_nodes(id TEXT PRIMARY KEY,enabled INTEGER);INSERT INTO controller_nodes VALUES('a',1),('b',1),('c',1)");
 const env={DB:sqliteD1(sql),CONTROLLER_NODE_MAP:JSON.stringify({[A]:'a',[B]:'b',[C]:'c'})};
 const call=(node,op,data={},now=10,opts={})=>ownershipTransfer(env,'site',{id:node},op,data,now,{...options,...opts});
 const boot=(extra={},node='a',opts={})=>call(node,'bootstrap',adoption(extra),10,{bootstrapAuthorized:true,...opts});
 if(adopt)await boot();
 const start=(proof=transfer())=>call('a','prepare',{...proof,source_frozen:true});
 const ready=(proof=transfer())=>call('b','ready',{...proof,verified:true,staged:true});
 const count=table=>sql.prepare(`SELECT count(*) AS n FROM ${table}`).get().n;
 return {sql,env,call,boot,start,ready,count};
}
const rejected=(promise,status)=>assert.rejects(promise,error=>status===undefined||error.status===status);

test('canonical aliases and raw scope tuple order match Python, including supplementary Unicode and quotes',async t=>{
 const {boot,call}=await fixture(t,{adopt:false});
 const names=['resource:😀','resource:\uE000'];
 const scope=[['work','a#'],['work','a"'],['work','😀'],['work','\uE000']];
 const expected=JSON.parse(execFileSync('python',['-c','import json,sys;x=json.load(sys.stdin);print(json.dumps([sorted(x[0]),sorted(x[1])]))'],{input:JSON.stringify([names,scope]),encoding:'utf8'}));
 await boot({aliases:names});
 assert.deepEqual((await call('a','status',{unit:'domain-one'})).aliases,expected[0]);
 const row=await call('a','prepare',{...transfer({scope,aliases:names}),source_frozen:true});
 assert.deepEqual(row.scope,expected[1]);assert.deepEqual(row.aliases,expected[0]);
});

test('cancel records an interrupted local prepare and advances epoch against late replay',async t=>{
 const {call,count}=await fixture(t);const proof=transfer();
 const row=await call('a','cancel',proof,1000);
 assert.equal(row.state,'aborted');assert.equal(row.current_epoch,2);
 await call('a','ack',{...proof,durable:true},1000);
 await call('b','ack',{...proof,durable:true},1000);
 assert.equal(count('ownership_transfers'),0);
 await rejected(call('a','prepare',{...proof,source_frozen:true},10),409);
 assert.equal(count('ownership_transfers'),0);
});

test('source can explicitly cancel even after expiry, never after commitment',async t=>{
 const {call,start,ready,env,count}=await fixture(t);const proof=transfer();
 await start();await ready();
 await rejected(call('b','cancel',proof,1000),403);
 const cancelled=await call('a','cancel',proof,1000);
 assert.equal(cancelled.state,'aborted');assert.equal(cancelled.current_owner,A);
 assert.equal((await resolveOwnershipRoute(env.DB,'site',routes[0])).frozen,false);
 await rejected(call('b','commit',proof,50),409);
 await rejected(ready(),409);
 await call('a','ack',{...proof,durable:true},1000);
 await call('b','ack',{...proof,durable:true},1000);
 assert.equal(count('ownership_transfers'),0);assert.equal(count('ownership_transfer_scope'),0);
 assert.equal((await call('a','status',{unit:proof.unit})).epoch,2);
});

test('commit and cancellation have a single atomic winner',async t=>{
 const {call,start,ready}=await fixture(t);const proof=transfer();
 await start();await ready();
 const results=await Promise.allSettled([call('b','commit',proof),call('a','cancel',proof)]);
 assert.equal(results.filter(x=>x.status==='fulfilled').length,1);
 const state=await call('a','status',{unit:proof.unit});
 assert.ok(['committed','aborted'].includes(state.transfer.state));
 assert.equal(state.owner,state.transfer.state==='committed'?B:A);
 assert.equal(state.epoch,2);
});

test('bootstrap defaults closed and accepts only authenticated current-authority adoption',async t=>{
 const {env,call,boot,count}=await fixture(t,{adopt:false});
 await rejected(call('a','bootstrap',adoption()),403);
 await rejected(call('a','bootstrap',{...adoption(),bootstrapAuthorized:true}),400);
 await rejected(boot({owner:B}),400);
 await rejected(boot({local_authority:false}),400);
 await rejected(boot({durable:false}),400);
 await rejected(boot({expected_epoch:1}),400);
 assert.equal(count('ownership_current'),0);
 const accepted=await boot();assert.equal(accepted.epoch,1);assert.equal(accepted.owner,A);
 assert.deepEqual(await boot(),accepted);
 assert.deepEqual(await call('c','status',{unit:'domain-one'}),accepted);
 assert.equal((await resolveOwnershipRoute(env.DB,'site',routes[1])).owner,A);
 assert.equal(await resolveOwnershipRoute(env.DB,'site','unknown'),null);
});

test('bootstrap stores only present authority and aliases, not initial snapshot or record keys',async t=>{
 const {sql,call,boot}=await fixture(t);
 assert.deepEqual(sql.prepare('PRAGMA table_info(ownership_current)').all().map(x=>x.name),['site','unit','owner','epoch','authority_digest']);
 const stored=JSON.stringify(sql.prepare('SELECT * FROM ownership_current').all());
 assert.ok(!stored.includes(SNAP));assert.ok(!stored.includes('work-one'));
 assert.deepEqual(await boot({snapshot_digest:SIGN}),await call('a','status',{unit:'domain-one'}));
 await rejected(boot({authority_digest:SIGN}),409);
 await rejected(boot({aliases:[...routes,'unexpected']}),409);
});

test('full transfer atomically moves current owner and discards all finite metadata after both ACKs',async t=>{
 const {sql,env,call,start,ready,count}=await fixture(t);const proof=transfer();
 const first=await start();assert.equal(first.state,'prepared');assert.equal(first.current_owner,A);
 let route=await resolveOwnershipRoute(env.DB,'site',routes[0]);assert.equal(route.frozen,true);assert.equal(route.owner,A);
 await ready();const committed=await call('b','commit',proof);
 assert.equal(committed.epoch,1);assert.equal(committed.owner,A);assert.equal(committed.current_epoch,2);assert.equal(committed.current_owner,B);
 route=await resolveOwnershipRoute(env.DB,'site',routes[0]);assert.equal(route.owner,B);assert.equal(route.frozen,false);assert.equal(route.epoch,2);
 const ack=await call('a','ack',{...proof,durable:true});assert.equal(ack.source_ack,true);assert.equal(ack.target_ack,false);
 const closed=await call('b','ack',{...proof,durable:true});assert.equal(closed.closed,true);assert.equal(closed.target_ack,true);
 assert.equal(count('ownership_transfers'),0);assert.equal(count('ownership_transfer_scope'),0);assert.equal(count('ownership_current'),1);assert.equal(count('ownership_routes'),routes.length);
 const current=await call('b','status',{unit:proof.unit});assert.equal(current.owner,B);assert.equal(current.epoch,2);assert.equal(current.transfer,null);assert.equal(current.frozen,false);
 assert.ok(!JSON.stringify(sql.prepare('SELECT * FROM ownership_current').all()).includes('work-one'));
 await rejected(call('b','ack',{...proof,durable:true}),410);
 await rejected(call('b','commit',proof),410);
});

test('roles, attestations and ordering fail closed',async t=>{
 const {call,start,ready}=await fixture(t);const proof=transfer();
 await rejected(ready(),410);await rejected(call('a','commit',proof),410);await rejected(call('b','ack',{...proof,durable:true}),410);
 await rejected(call('b','prepare',{...proof,source_frozen:true}),403);
 await rejected(call('c','prepare',{...proof,source_frozen:true}),403);
 await rejected(call('a','prepare',proof),400);
 await start();
 await rejected(call('a','commit',proof),409);await rejected(call('b','commit',proof),409);
 await rejected(call('a','ready',{...proof,verified:true,staged:true}),403);
 await rejected(call('b','ready',{...proof,verified:true}),400);
 await rejected(call('b','ack',{...proof,durable:true}),409);
 await ready();await call('a','commit',proof);
 await rejected(ready(),409);await rejected(call('b','ack',proof),409);
});

test('every immutable binding component is checked before staging, commit and cleanup',async t=>{
 const {call,start,ready}=await fixture(t);const proof=transfer();await start();await ready();
 const alterations=[{epoch:2},{owner:C},{target:C},{transfer:'different'},{authority_digest:SIGN},{snapshot_digest:SIGN},{authorization_digest:SNAP},{expires:99},{scope:[['work','other']]},{aliases:['other']}];
 for(const change of alterations){
  await rejected(call('b','ready',{...proof,...change,verified:true,staged:true}));
  await rejected(call('b','commit',{...proof,...change}));
 }
 const before=await call('a','status',{unit:proof.unit});assert.equal(before.owner,A);assert.equal(before.epoch,1);assert.equal(before.transfer.state,'ready');
 await call('a','commit',proof);
 for(const change of alterations)await rejected(call('b','ack',{...proof,...change,durable:true}));
 assert.equal((await call('b','status',{unit:proof.unit})).transfer.target_ack,false);
});

test('exact retries are idempotent and canonical scope/alias ordering does not change the binding',async t=>{
 const {call,start,ready,count}=await fixture(t);const proof=transfer();
 const reordered={...proof,aliases:[...proof.aliases].reverse(),scope:[...proof.scope].reverse()};
 assert.deepEqual(await start(proof),await start(reordered));assert.equal(count('ownership_transfer_scope'),2);
 assert.deepEqual(await ready(proof),await ready(reordered));
 const committed=await call('a','commit',proof);assert.deepEqual(await call('b','commit',reordered),committed);
 // Read-only retry reports an already completed CAS, not a post-expiry action.
 assert.deepEqual(await call('b','commit',proof,1000),committed);
 assert.deepEqual(await call('a','ack',{...proof,durable:true}),await call('a','ack',{...proof,durable:true}));
 assert.equal(count('ownership_transfers'),1);
});

test('expiry never transfers, unfreezes or deletes incomplete state',async t=>{
 const {call,start,ready,env,count}=await fixture(t);const proof=transfer();
 await rejected(call('a','prepare',{...proof,source_frozen:true},100),410);assert.equal(count('ownership_transfers'),0);
 await start();await rejected(call('b','ready',{...proof,verified:true,staged:true},100),410);
 await ready();await rejected(call('b','commit',proof,100),410);await rejected(call('a','prepare',{...proof,source_frozen:true},100),410);
 const current=await call('c','status',{unit:proof.unit},100000);assert.equal(current.owner,A);assert.equal(current.epoch,1);assert.equal(current.frozen,true);assert.equal(current.transfer.state,'ready');
 assert.equal((await resolveOwnershipRoute(env.DB,'site',routes[1])).frozen,true);assert.equal(count('ownership_transfers'),1);assert.equal(count('ownership_transfer_scope'),2);
 await rejected(call('b','bootstrap',adoption({owner:B}),100000,{bootstrapAuthorized:true}),409);
});

test('recipient can finish after source loss only with previously frozen and staged exact snapshot',async t=>{
 const {sql,call,start,ready,count}=await fixture(t);const proof=transfer();
 await start();sql.exec("UPDATE controller_nodes SET enabled=0 WHERE id='a'");
 await rejected(call('b','commit',proof),409);await ready();
 const result=await call('b','commit',proof);assert.equal(result.current_owner,B);assert.equal(result.current_epoch,2);
 await rejected(call('a','ack',{...proof,durable:true}),403);
 await call('b','ack',{...proof,durable:true},1000);assert.equal(count('ownership_transfers'),1);
 // Return of the source permits only durable cleanup, never reclaiming owner.
 sql.exec("UPDATE controller_nodes SET enabled=1 WHERE id='a'");
 assert.equal((await call('a','ack',{...proof,durable:true},1000)).closed,true);assert.equal(count('ownership_transfers'),0);
});

test('two-phase prepared recovery does not require live source registration after ready',async t=>{
 const {sql,env,call,start,ready}=await fixture(t);await start();await ready();
 sql.exec("DELETE FROM controller_nodes WHERE id='a'");env.CONTROLLER_NODE_MAP=JSON.stringify({[B]:'b',[C]:'c'});
 assert.equal((await call('b','commit',transfer())).current_owner,B);
});

test('alias uniqueness and bounded bootstrap races never union competing alias sets',async t=>{
 const {sql,call,boot,count}=await fixture(t,{adopt:false});
 const outcomes=await Promise.allSettled([boot({aliases:['first']}),boot({aliases:['second']})]);
 assert.equal(outcomes.filter(x=>x.status==='fulfilled').length,1);assert.equal(count('ownership_current'),1);assert.equal(count('ownership_routes'),1);
 const current=await call('a','status',{unit:'domain-one'});assert.equal(current.aliases.length,1);
 await rejected(boot({unit:'domain-two',aliases:current.aliases}),409);assert.equal(count('ownership_current'),1);
 assert.deepEqual(sql.prepare('SELECT DISTINCT unit FROM ownership_routes').all().map(x=>x.unit),['domain-one']);
});

test('competing owners have exactly one successful bootstrap, even at the site limit',async t=>{
 const {boot,count}=await fixture(t,{adopt:false,options:{maxUnits:1}});
 const results=await Promise.allSettled([boot(),boot({owner:B},'b')]);assert.equal(results.filter(x=>x.status==='fulfilled').length,1);
 await rejected(boot({unit:'another-unit',aliases:['another-route']}),413);assert.equal(count('ownership_current'),1);assert.equal(count('ownership_routes'),2);
});

test('conflicting transient scopes roll back all reservations and all new metadata',async t=>{
 const {boot,start,call,count}=await fixture(t);await start();
 await boot({unit:'domain-two',aliases:['second']});
 const proof=transfer({unit:'domain-two',transfer:'move-two',aliases:['second'],scope:[['work','independent'],['grants','grant-one']]});
 await rejected(call('a','prepare',{...proof,source_frozen:true}),409);
 assert.equal(count('ownership_transfers'),1);assert.equal(count('ownership_transfer_scope'),2);
 assert.equal((await call('a','status',{unit:'domain-two'})).frozen,false);
});

test('fresh future snapshots use changed record keys without retaining historic scope',async t=>{
 const {call,start,ready,count,sql}=await fixture(t);const old=transfer();await start();await ready();await call('b','commit',old);
 await call('a','ack',{...old,durable:true});await call('b','ack',{...old,durable:true});
 const next=transfer({owner:B,target:A,epoch:2,transfer:'move-back',scope:[['work','brand-new-work'],['events','brand-new-event']],snapshot_digest:'2'.repeat(64),authorization_digest:'3'.repeat(64)});
 await call('b','prepare',{...next,source_frozen:true});await call('a','ready',{...next,verified:true,staged:true});
 await rejected(call('a','commit',old),409);assert.equal((await call('a','commit',next)).current_epoch,3);
 await call('a','ack',{...next,durable:true});await call('b','ack',{...next,durable:true});
 assert.equal(count('ownership_transfer_scope'),0);assert.equal(count('ownership_transfers'),0);
 const stored=JSON.stringify(sql.prepare('SELECT * FROM ownership_current').all());assert.ok(!stored.includes('work-one'));assert.ok(!stored.includes('brand-new-work'));
 assert.equal((await call('a','status',{unit:'domain-one'})).owner,A);
});

test('concurrent prepare contention and commit CAS never create a second epoch increment',async t=>{
 const {call,ready,count}=await fixture(t);const one=transfer(),two=transfer({transfer:'competitor',snapshot_digest:'2'.repeat(64)});
 const opened=await Promise.allSettled([one,two].map(proof=>call('a','prepare',{...proof,source_frozen:true})));
 assert.equal(opened.filter(x=>x.status==='fulfilled').length,1);assert.equal(count('ownership_transfers'),1);assert.equal(count('ownership_transfer_scope'),2);
 const winner=opened[0].status==='fulfilled'?one:two;await ready(winner);
 const committed=await Promise.all([call('a','commit',winner),call('b','commit',winner)]);for(const r of committed)assert.equal(r.current_epoch,2);
 assert.equal((await call('b','status',{unit:'domain-one'})).epoch,2);
 const acks=await Promise.allSettled([call('a','ack',{...winner,durable:true}),call('b','ack',{...winner,durable:true})]);
 assert.equal(acks.filter(x=>x.status==='fulfilled').length,2);assert.equal(count('ownership_transfers'),0);assert.equal(count('ownership_transfer_scope'),0);
});

test('site count, transfer count, duration and byte bounds reject without orphan records',async t=>{
 const {call,boot,start,count}=await fixture(t);await start();await boot({unit:'second',aliases:['second-route']});
 const next=transfer({unit:'second',transfer:'next',aliases:['second-route'],scope:[['work','second']]});
 await rejected(call('a','prepare',{...next,source_frozen:true},10,{maxActive:1}),413);
 await rejected(call('a','prepare',{...next,source_frozen:true},10,{maxBytes:100}),413);
 await rejected(call('a','prepare',{...next,source_frozen:true},10,{maxSeconds:89}),413);
 assert.equal(count('ownership_transfers'),1);assert.equal(count('ownership_transfer_scope'),2);
 await rejected(call('a','status',{unit:'domain-one'},10,{maxSeconds:604801}),503);
 await rejected(call('a','status',{unit:'domain-one'},10,{maxUnits:0}),503);
});

test('all admitted metadata is explicitly bounded and no payload or attestation extras are stored',async t=>{
 const {call,start,count}=await fixture(t);const proof=transfer();
 for(const change of [{scope:[]},{scope:Array.from({length:257},(_,i)=>['work',String(i)])},{scope:[['work','x'.repeat(513)]]},{scope:[['work','x'],['work','x']]},{aliases:['repeat','repeat']},{aliases:['x'.repeat(513)]},{scope:[['work','secret\nline']]},{epoch:Number.MAX_SAFE_INTEGER},{transfer:'bad / id'}])await rejected(start({...proof,...change}),400);
 await rejected(call('a','prepare',{...proof,source_frozen:true,snapshot:{secret:'body'}}),400);
 await rejected(call('a','prepare',{...proof,source_frozen:true,signature:'opaque'}),400);
 assert.equal(count('ownership_transfers'),0);
 const scoped=transfer({scope:Array.from({length:256},(_,i)=>['records',String(i)])});await start(scoped);assert.equal(count('ownership_transfer_scope'),256);
});

test('registered application/transport association must be exact and enabled',async t=>{
 const {sql,env,call,boot}=await fixture(t,{adopt:false});
 await rejected(boot({},'missing'),403);
 env.CONTROLLER_NODE_MAP=JSON.stringify({[A]:'a',[B]:'a'});await rejected(boot(),503);
 env.CONTROLLER_NODE_MAP=JSON.stringify({[A]:'a',[B]:'b'});sql.exec("UPDATE controller_nodes SET enabled=0 WHERE id='a'");await rejected(boot(),403);
 sql.exec("UPDATE controller_nodes SET enabled=1 WHERE id='a'");await boot();
 sql.exec("UPDATE controller_nodes SET enabled=0 WHERE id='b'");await rejected(call('a','prepare',{...transfer(),source_frozen:true}),403);
 env.CONTROLLER_NODE_MAP='[';await rejected(call('a','status',{unit:'domain-one'}),503);
});

test('status is a consistent current snapshot while commit races',async t=>{
 const {call,start,ready}=await fixture(t);await start();await ready();
 const results=await Promise.all([call('a','commit',transfer()),...Array.from({length:12},()=>call('c','status',{unit:'domain-one'}))]);
 for(const value of results.slice(1)){
  assert.equal(value.epoch,value.transfer.current_epoch);assert.equal(value.owner,value.transfer.current_owner);
  if(value.transfer.state==='committed'){assert.equal(value.owner,B);assert.equal(value.frozen,false)}else{assert.equal(value.owner,A);assert.equal(value.frozen,true)}
 }
});

test('site isolation and generic Unicode route aliases preserve canonical current metadata',async t=>{
 const {env,boot,call}=await fixture(t,{adopt:false});const names=['resource:😀','resource:\uE000'];await boot({aliases:names});
 assert.deepEqual((await call('a','status',{unit:'domain-one'})).aliases,['resource:\uE000','resource:😀']);
 const adopted=await ownershipTransfer(env,'other-site',{id:'b'},'bootstrap',adoption({owner:B,aliases:names}),10,{bootstrapAuthorized:true});assert.equal(adopted.owner,B);
 assert.equal((await resolveOwnershipRoute(env.DB,'site',names[0])).owner,A);assert.equal((await resolveOwnershipRoute(env.DB,'other-site',names[0])).owner,B);
});

test('incompatible current metadata schema is preserved and never silently reinterpreted',async t=>{
 const sql=new DatabaseSync(':memory:');t.after(()=>sql.close());
 sql.exec("CREATE TABLE ownership_current(site TEXT,unit TEXT,owner TEXT,epoch INTEGER,authority_digest TEXT,old_scope TEXT,PRIMARY KEY(site,unit));INSERT INTO ownership_current VALUES('site','old','owner',1,'authority','preserve')");
 await rejected(ownershipSchema(sqliteD1(sql)),503);assert.equal(sql.prepare('SELECT old_scope FROM ownership_current').get().old_scope,'preserve');
});
