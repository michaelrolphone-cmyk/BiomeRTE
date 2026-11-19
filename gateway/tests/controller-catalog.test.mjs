import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {sqliteD1} from './sqlite-d1.mjs';
import {controllerCatalog,discoverPage} from '../lib/discovery.mjs';
function fixture(){
 const sql=new DatabaseSync(':memory:');sql.exec('CREATE TABLE controller_config(id INTEGER,enabled INTEGER,heartbeat_seconds REAL);INSERT INTO controller_config VALUES(1,1,10);CREATE TABLE controller_nodes(id TEXT,enabled INTEGER,seen REAL,ready INTEGER)');
 const now=Date.now()/1000;for(const [id,enabled,seen,ready] of [['mac',1,now-100,1],['pi',1,now,1],['disabled',0,now,1]])sql.prepare('INSERT INTO controller_nodes VALUES(?,?,?,?)').run(id,enabled,seen,ready);
 return {sql,env:{DB:sqliteD1(sql),CONTROLLER_COORDINATION_MODE:'local',LOCAL_CONTROLLER_ID:'mac',CONTROLLER_NODE_MAP:JSON.stringify({['a'.repeat(64)]:'mac',['b'.repeat(64)]:'pi',['c'.repeat(64)]:'disabled'})}};
}
test('local controller catalog works without default controller or bridge queue',async()=>{
 const {sql,env}=fixture();try{const response=await discoverPage(env,{section:'controllers'});assert.equal(response.isError,undefined);const page=JSON.parse(response.content[0].text);assert.equal(page.controllers.length,2);assert.deepEqual(page.controllers.map(c=>[c.label,c.ready,c.default]),[['mac',false,true],['pi',true,false]]);assert.equal(page.controllers[1].next_call.controller,undefined);assert.equal(page.controllers[1].next_call.operation,'controller.invoke');assert.deepEqual(page.controllers[1].next_call.arguments,{controller:'b'.repeat(64),operation:'system.discovery',arguments:{section:'offers'}});assert.equal(page.complete,true);}finally{sql.close()}
});
test('catalog rejects ambiguous mapping and does not expose disabled identities',async()=>{
 const {sql,env}=fixture();try{assert.equal((await controllerCatalog(env)).some(c=>c.label==='disabled'),false);env.CONTROLLER_NODE_MAP=JSON.stringify({['a'.repeat(64)]:'mac',['b'.repeat(64)]:'mac'});await assert.rejects(controllerCatalog(env),/Ambiguous/);}finally{sql.close()}
});
test('catalog refuses pagination, destination ambiguity and legacy mode',async()=>{
 const {sql,env}=fixture();try{for(const args of [{section:'controllers',cursor:'x'},{section:'controllers',controller:'a'.repeat(64)}])assert.equal((await discoverPage(env,args)).isError,true);delete env.CONTROLLER_COORDINATION_MODE;assert.equal((await discoverPage(env,{section:'controllers'})).isError,true);}finally{sql.close()}
});
