import test from 'node:test';
import assert from 'node:assert/strict';
import {controllerEventAuthorization} from '../lib/controller-event-auth.mjs';
const key='k'.repeat(48);
const env={CONTROLLER_EVENT_AUTH_SECRET:key,DB:{prepare(sql){let args=[];return{
 bind(...x){args=x;return this},async first(){
  if(sql.includes('biome_owner'))return {user_id:'owner'};
  if(sql.includes('biome_oauth_tokens'))return args[0]==='owner'&&args[1]==='live-grant'?{ok:1}:null;
  throw Error('Unexpected query');
 }}}}};
const request=(body,headers={})=>new Request('https://site.example/internal/controller-events/authorize',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+key,...headers},body:JSON.stringify(body)});
test('callback authorization requires its dedicated secret',async()=>{
 assert.equal((await controllerEventAuthorization(request({owner:'owner',grant:null},{Authorization:'Bearer other'}),env)).status,401);
 assert.equal((await controllerEventAuthorization(request({owner:'owner',grant:null},{Origin:'https://site.example'}),env)).status,403);
});
test('owner and grant revocation are checked live for each delivery',async()=>{
 for(const [owner,grant,allowed] of [['owner',null,true],['owner','live-grant',true],['owner','revoked-grant',false],['other',null,false]]){
  const response=await controllerEventAuthorization(request({owner,grant}),env);
  assert.equal(response.status,200);assert.deepEqual(await response.json(),{allowed});
 }
});
test('invalid auth payloads cannot introduce additional authorization fields',async()=>{
 for(const body of [{owner:'owner',grant:null,approved:true},{owner:'owner'},null,[]])
  assert.equal((await controllerEventAuthorization(request(body),env)).status,400);
});
