import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import worker from '../worker/index.js';
import {oauth} from '../lib/oauth.mjs';
import {dashboardHTML,dashboardJS} from '../lib/dashboard-assets.mjs';
const origin='https://biomerte-controller.cantikash.chatgpt.site';
test('public homepage omits connections management; authenticated dashboard contains it',async()=>{
 const page=await worker.fetch(new Request(origin+'/'),{});
 assert.doesNotMatch(await page.text(),/oauth\/connections|Manage authorized connections/);
 assert.match(dashboardHTML,/id="connections"[^>]*href="\/oauth\/connections"/);
});
test('connections management denies anonymous and other users before touching grant records',async()=>{
 const env={BIOME_OAUTH_ORIGIN:origin,ownerPrincipal:async()=>null,DB:{prepare(){throw Error('unauthorized grant read')}}};
 let res=await oauth(new Request(origin+'/oauth/connections'),env,'connections');
 assert.equal(res.status,401);assert.match(await res.text(),/Sign in/);
 res=await oauth(new Request(origin+'/oauth/connections',{headers:{'oai-authenticated-user-id':'another'}}),env,'connections');
 assert.equal(res.status,403);
 res=await oauth(new Request(origin+'/oauth/disconnect',{method:'POST',headers:{Origin:origin},body:'grant_id=any'}),env,'disconnect');assert.equal(res.status,403);
});
test('owner connections require no-store and cross-origin revocation is refused',async()=>{
 let reads=0,revocations=0;
 const env={BIOME_OAUTH_ORIGIN:origin,ownerPrincipal:async()=> 'owner',DB:{prepare(){return {bind(){return this},async all(){reads++;return {results:[]}},async run(){revocations++;return {}}}}}};
 const res=await oauth(new Request(origin+'/oauth/connections'),env,'connections');
 assert.equal(res.status,200);assert.equal(reads,1);assert.match(res.headers.get('Cache-Control'),/private.*no-store/);
 assert.equal((await oauth(new Request(origin+'/oauth/disconnect',{method:'POST',headers:{Origin:'https://evil.example'},body:'grant_id=any'}),env,'disconnect')).status,403);
 assert.equal(revocations,0);
 assert.equal((await oauth(new Request(origin+'/oauth/disconnect',{method:'POST',headers:{Origin:origin},body:'grant_id=any'}),env,'disconnect')).status,303);
 assert.equal(revocations,1);
});
function connectContext(call,refresh=async()=>{}) {
 const elements={connect:{disabled:false,textContent:'Connect'},login:{hidden:false},workspace:{hidden:true},operation:{textContent:''},status:{textContent:''},'connection-title':{textContent:''}};
 const c={el:id=>elements[id],say:s=>{elements.status.textContent=s},call,refresh,option(){},help(){},showTab(){},operations:[],connecting:false,connected:false,render:async()=>{elements.renderWasVisible=!elements.workspace.hidden;}};
 vm.createContext(c);
 const a=dashboardJS.indexOf('async function connectDashboard('),b=dashboardJS.indexOf('function workflowCards(',a);
 assert.ok(a>=0&&b>a);vm.runInContext(dashboardJS.slice(a,b),c);
 return {c,elements};
}
test('Connect responds immediately while pending and failed attempts are visibly retryable',async()=>{
 let reject;const {c,elements}=connectContext(()=>new Promise((resolve,r)=>{reject=r}));
 const task=vm.runInContext('connectDashboard({preventDefault(){}})',c);
 assert.equal(elements.connect.disabled,true);assert.match(elements.status.textContent,/Connecting/);
 reject(new Error('controller unavailable'));await task;
 assert.match(elements.status.textContent,/Connection failed: controller unavailable/);
 assert.equal(elements.login.hidden,false);assert.equal(elements.connect.disabled,false);assert.equal(elements.connect.textContent,'Connect');
});
test('Connect reveals the actual workspace after successful calls',async()=>{
 const {c,elements}=connectContext(async()=>[]);
 await vm.runInContext('connectDashboard({preventDefault(){}})',c);
 assert.equal(elements.login.hidden,true);assert.equal(elements.workspace.hidden,false);assert.equal(elements.status.textContent,'Connected');assert.equal(elements.renderWasVisible,true);
 assert.equal(elements.connect.disabled,false);
});
