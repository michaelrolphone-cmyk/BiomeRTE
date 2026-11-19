import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {coordinationSession,sessionSchema} from '../lib/coordination-session.mjs';
async function fixture(bounds={}){const sql=new DatabaseSync(':memory:');const db=sqliteD1(sql);sql.exec("CREATE TABLE controller_nodes(id TEXT,enabled INTEGER);INSERT INTO controller_nodes VALUES('a',1),('b',1),('x',1)");await sessionSchema(db);const call=(node,op,data={})=>coordinationSession(db,'site',node,op,{session:'test',closer_epoch:1,...data},10);await call('a','open',{participants:['a','b'],scope:[['work','r']],authorized_closers:['a','b'],successor_after:20,expires:20,max_entries:1000,max_bytes:10000000,...bounds,authorized_closers:bounds.authorized_closers??['a'],successor_after:bounds.successor_after??bounds.expires??20});return {sql,call};}
const entry=(seq=1,prev='',value='{"n":9007199254740993,"f":1.0,"z":-0.0,"u":"é"}')=>JSON.stringify({seq,prev,changes:[['work','r',value]]});
test('opaque values, frozen distinct recipient proofs, full deletion and lost close reply',async()=>{const {sql,call}=await fixture();const text=entry();const head=await call('a','append',{expected:0,entry:text});assert.equal((await call('b','read',{after:0})).record.entry,text);await call('a','freeze',{final_seq:head.seq,final_hash:head.hash});await assert.rejects(call('a','close'));for(const [node,digit] of [['a','a'],['b','b']])await call(node,'ack',{final_seq:head.seq,final_hash:head.hash,package_digest:digit.repeat(64)});await call('a','close');assert.deepEqual(await call('a','close'),{closed:true});for(const table of ['coordination_sessions','coordination_entries','coordination_ttl','coordination_acks','coordination_scope'])assert.equal(sql.prepare(`SELECT count(*) n FROM ${table}`).get().n,0);await assert.rejects(call('a','append',{expected:0,entry:text}),e=>e.status===410);sql.close();});
test('bounds and membership reject before changing head',async()=>{const {sql,call}=await fixture({max_entries:1});await assert.rejects(call('x','status'),e=>e.status===403);const head=await call('a','append',{expected:0,entry:entry()});await assert.rejects(call('b','append',{expected:1,entry:entry(2,head.hash)}),e=>e.status===413);assert.equal((await call('a','status')).seq,1);sql.close();});
test('expiry stops admission but permits frozen export acknowledgments',async()=>{const {sql}=await fixture();const call=(node,op,data)=>coordinationSession(sqliteD1(sql),'site',node,op,{session:'test',closer_epoch:1,...data},21);await assert.rejects(call('a','append',{expected:0,entry:entry()}),e=>e.status===410);await call('a','freeze',{final_seq:0,final_hash:''});await call('b','ack',{final_seq:0,final_hash:'',package_digest:'a'.repeat(64)});sql.close();});
test('CAS contention has one winner and no partial entries',async()=>{const {sql,call}=await fixture();const results=await Promise.allSettled([call('a','append',{expected:0,entry:entry()}),call('b','append',{expected:0,entry:entry(1,'','{}')})]);assert.equal(results.filter(r=>r.status==='fulfilled').length,1);assert.equal(sql.prepare('SELECT count(*) n FROM coordination_entries').get().n,1);sql.close();});
test('TTL churn is separate and bounded, D1-safe near-1-MiB entry accepted',async()=>{const {sql,call}=await fixture();await call('a','ttl',{key:'heartbeat',value:'{}',expires:15});await call('a','ttl',{key:'heartbeat',value:'{"ready":true}',expires:16});assert.equal(sql.prepare('SELECT count(*) n FROM coordination_entries').get().n,0);await call('a','append',{expected:0,entry:entry(1,'',JSON.stringify('x'.repeat(1024*1024-256)))});sql.close();});

test('overlapping unresolved exact record scopes cannot open',async()=>{const {sql,call}=await fixture();await assert.rejects(call('b','open',{session:'other',participants:['a','b'],scope:[['work','r']],authorized_closers:['a','b'],successor_after:20,expires:20,max_entries:20,max_bytes:10000}),e=>e.status===409);assert.equal(sql.prepare('SELECT count(*) n FROM coordination_sessions').get().n,1);sql.close();});
test('local-first dispatch cannot overflow to another SQLite owner',async()=>{
 const {readFileSync}=await import('node:fs');const {claimControllerJob}=await import('../lib/ha.mjs');
 const sql=new DatabaseSync(':memory:');for(const path of ['drizzle/0000_biome_gateway.sql','drizzle/0001_controller_ha.sql'])sql.exec(readFileSync(path,'utf8'));
 const now=Date.now()/1000;sql.prepare('INSERT INTO controller_config(id,site,enabled) VALUES(1,?,1)').run('site');
 for(const [id,priority] of [['a',1],['b',2]])sql.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity,seen,ready,position,load,host) VALUES(?,?,?,?,?,1,0,0,?)').run(id,id.repeat(64),priority,2,now,id);
 sql.prepare("INSERT INTO bridge_jobs(id,request,state,deadline,updated) VALUES(?,?,'queued',?,?)").run('job',JSON.stringify({name:'biome_invoke',arguments:{realm:'site',controller_owner:'b'}}),now+60,now-60);
 const db=sqliteD1(sql),env={CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'b'};
 assert.equal((await claimControllerJob(db,sql.prepare('SELECT * FROM controller_nodes WHERE id=?').get('a'),env)).idle,true);
 assert.equal((await claimControllerJob(db,sql.prepare('SELECT * FROM controller_nodes WHERE id=?').get('b'),env)).id,'job');sql.close();
});
test('built artifact exports sessions without permanent-state prototype',async()=>{const artifact=await import('../dist/server/index.js');assert.equal(typeof artifact.coordinationSession,'function');assert.equal(artifact.stateSchema,undefined);});
test('atomic 256-record session transaction and out-of-scope rejection',async()=>{
 const scope=Array.from({length:256},(_,i)=>['work',String(i)]);const {sql,call}=await fixture({scope});
 const text=JSON.stringify({seq:1,prev:'',changes:scope.map(([kind,id])=>[kind,id,'{"n":9007199254740993}'])});
 const head=await call('a','append',{expected:0,entry:text});assert.equal(head.seq,1);
 await assert.rejects(call('a','append',{expected:1,entry:JSON.stringify({seq:2,prev:head.hash,changes:[['work','outside','{}']]})}),e=>e.status===400);
 assert.equal((await call('a','status')).seq,1);sql.close();
});
test('durable signed local outbox allows closing with an offline recipient without claiming import',async()=>{
 const {sql,call}=await fixture();await call('a','freeze',{final_seq:0,final_hash:''});
 const proof={final_seq:0,final_hash:'',durable:true,packages:[{to:'a',package_digest:'a'.repeat(64)},{to:'b',package_digest:'b'.repeat(64)}]};
 await assert.rejects(call('b','outbox',proof),e=>e.status===403);
 await assert.rejects(call('a','outbox',{...proof,packages:proof.packages.slice(0,1)}));
 await call('a','ack',{final_seq:0,final_hash:'',package_digest:'a'.repeat(64)});
 const result=await call('a','outbox',proof);assert.deepEqual(await call('a','close'),{closed:true});assert.deepEqual(result.imported,['a']);assert.deepEqual(result.pending_delivery,['b']);assert.equal(result.exported_by,'a');
 assert.equal(sql.prepare('SELECT count(*) n FROM coordination_exports').get().n,0);assert.equal(sql.prepare('SELECT count(*) n FROM coordination_sessions').get().n,0);sql.close();
});
test('HTTP session membership uses verified application identities, not transport aliases',async()=>{
 const {controllerDevice}=await import('../lib/ha.mjs');const {sql}=await fixture();sql.exec("CREATE TABLE controller_config(id INTEGER,site TEXT);INSERT INTO controller_config VALUES(1,'site')");
 const db=sqliteD1(sql),a='a'.repeat(64),b='b'.repeat(64);const env={DB:db,CONTROLLER_NODE_MAP:JSON.stringify({[a]:'a',[b]:'b'})};
 const data={session:'mapped',participants:[a,b],scope:[['work','other']],expires:Date.now()/1000+60,max_entries:10,max_bytes:10000};
 data.authorized_closers=[a];data.successor_after=data.expires;
 const req=()=>new Request('https://test/bridge/ha/session/open',{method:'POST',body:JSON.stringify(data)});
 const response=await controllerDevice(req(),env,'/bridge/ha/session/open',{id:'a'},false);assert.equal(response.status,200);assert.equal((await response.json()).owner,a);
 const rejected=await controllerDevice(req(),{DB:db},'/bridge/ha/session/open',{id:'a'},false);assert.equal(rejected.status,409);sql.close();
});

test('scope accepts actual document records and rejects nonexistent knowledge namespace',async()=>{
 const {sql,call}=await fixture({scope:[['documents','doc'],['revisions','rev']]});
 await call('a','append',{expected:0,entry:JSON.stringify({seq:1,prev:'',changes:[['documents','doc','{"title":"Document"}'],['revisions','rev','{}']]})});
 await assert.rejects(call('a','open',{session:'bad-kind',participants:['a','b'],scope:[['knowledge','doc']],authorized_closers:['a','b'],successor_after:20,expires:20,max_entries:10,max_bytes:10000}),e=>e.status===400);sql.close();
});
test('only designated closer freezes and node-local incoming receipts are excluded',async()=>{const {sql,call}=await fixture();await assert.rejects(call('b','freeze',{final_seq:0,final_hash:''}),e=>e.status===403);await assert.rejects(call('a','open',{session:'incoming',participants:['a','b'],scope:[['incoming','receipt']],authorized_closers:['a','b'],successor_after:20,expires:20,max_entries:10,max_bytes:10000}),e=>e.status===400);sql.close();});
test('superseded permanent state routes are absent and local journal writes are disabled',async()=>{const {controllerDevice}=await import('../lib/ha.mjs');const {sql}=await fixture();sql.exec("CREATE TABLE controller_config(id INTEGER,site TEXT);INSERT INTO controller_config VALUES(1,'site')");const env={DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local'};const call=path=>controllerDevice(new Request('https://gateway'+path,{method:'POST',body:'{}'}),env,path,{id:'a'},false);assert.equal((await call('/bridge/ha/state/migration/begin')).status,404);assert.equal((await call('/bridge/ha/append')).status,410);sql.close();});
test('expired frozen closer claim permits exactly one named successor at same head and fences stale outbox',async()=>{
 const {sql,call}=await fixture({participants:['a','b','x'],authorized_closers:['a','b','x']});const db=sqliteD1(sql);
 const at=(node,op,data,time)=>coordinationSession(db,'site',node,op,{session:'test',...data},time);
 const first=await call('a','freeze',{final_seq:0,final_hash:''});assert.equal(first.closer_epoch,1);
 await call('a','ack',{final_seq:0,final_hash:'',package_digest:'a'.repeat(64)});
 await assert.rejects(at('b','freeze',{final_seq:0,final_hash:''},19));await assert.rejects(at('b','freeze',{final_seq:0,final_hash:''},21));
 const outcomes=await Promise.allSettled(['b','x'].map(node=>at(node,'freeze',{final_seq:0,final_hash:''},71)));assert.equal(outcomes.filter(r=>r.status==='fulfilled').length,1);
 const selected=outcomes.find(r=>r.status==='fulfilled').value;assert.equal(selected.closer_epoch,2);assert.equal(selected.final_seq,0);assert.equal(selected.final_hash,'');assert.equal(sql.prepare('SELECT count(*) n FROM coordination_acks').get().n,0);
 const proof={final_seq:0,final_hash:'',closer_epoch:2,durable:true,packages:['a','b','x'].map((to,i)=>({to,package_digest:['a','b','c'][i].repeat(64)}))};
 await assert.rejects(at('a','outbox',{...proof,closer_epoch:1},72),e=>e.status===409);
 await assert.rejects(at(selected.closer,'outbox',{...proof,closer_epoch:1},72),e=>e.status===409);
 const renewed=await at(selected.closer,'freeze',{final_seq:0,final_hash:''},72);assert.equal(renewed.closer_epoch,2);
 await at(selected.closer,'outbox',proof,72);await assert.rejects(at('a','status',{},73),e=>e.status===410);assert.deepEqual(await at(selected.closer,'close',{},73),{closed:true});sql.close();
});
test('all authorized closers offline leaves bounded recoverable state rather than deleting unexported data',async()=>{const {sql,call}=await fixture({authorized_closers:['a','b']});await call('a','freeze',{final_seq:0,final_hash:''});const status=await coordinationSession(sqliteD1(sql),'site','b','status',{session:'test'},10000);assert.equal(status.state,'frozen');assert.equal(status.closer,'a');assert.equal(sql.prepare('SELECT count(*) n FROM coordination_sessions').get().n,1);sql.close();});
test('atomic site count, reserved byte and duration admission rejects without orphan scope claims',async()=>{
 const {sql}=await fixture();const db=sqliteD1(sql);const open=(session,overrides={},limits={})=>coordinationSession(db,'site','a','open',{session,participants:['a','b'],authorized_closers:['a'],successor_after:20,expires:20,scope:[['work',session]],max_entries:10,max_bytes:10000000,...overrides},10,limits);
 const outcomes=await Promise.allSettled([open('other-a',{}, {maxActive:2}),open('other-b',{}, {maxActive:2})]);assert.equal(outcomes.filter(r=>r.status==='fulfilled').length,1);assert.equal(outcomes.filter(r=>r.status==='rejected'&&r.reason.status===413).length,1);assert.equal(sql.prepare('SELECT count(*) n FROM coordination_scope').get().n,2);
 await assert.rejects(open('bytes',{max_bytes:1},{maxReservedBytes:20000000}),e=>e.status===413);await assert.rejects(open('duration',{expires:1000000,successor_after:1000000}),e=>e.status===413);assert.equal(sql.prepare('SELECT count(*) n FROM coordination_sessions').get().n,2);sql.close();
});

test('D1-safe inline admission rejects oversized whole entry before changing head',async()=>{const {sql,call}=await fixture();await assert.rejects(call('a','append',{expected:0,entry:entry(1,'',JSON.stringify('x'.repeat(1048576)))}),e=>e.status===413);assert.equal((await call('a','status')).seq,0);sql.close();});
test('old pre-release session schemas fail explicitly without overwriting unresolved state',async()=>{const sql=new DatabaseSync(':memory:');sql.exec("CREATE TABLE coordination_sessions(site TEXT,session TEXT,owner TEXT,definition TEXT,state TEXT,expires REAL,max_entries INTEGER,max_bytes INTEGER,seq INTEGER,hash TEXT,bytes INTEGER,PRIMARY KEY(site,session));INSERT INTO coordination_sessions VALUES('site','old','a','{}','open',20,10,10000,1,'preserve',5)");await assert.rejects(coordinationSession(sqliteD1(sql),'site','a','status',{session:'old'},10),e=>e.status===503&&e.message.includes('Incompatible'));assert.equal(sql.prepare('SELECT hash FROM coordination_sessions').get().hash,'preserve');sql.close();});
