import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync,readdirSync} from 'node:fs';
import {oauth,digest,bearerPrincipal,serverMetadata,resourceMetadata,SCOPE} from '../lib/oauth.mjs';
import {ownerPrincipal} from '../lib/access.mjs';
const origin='https://bus.test',redirect='https://grok.com/connectors-oauth-exchange-code/';
const owner={'oai-authenticated-user-id':'owner-principal','oai-authenticated-user-email':'owner@example.test'};
function fixture(){
 const sql=new DatabaseSync(':memory:');for(const file of readdirSync('drizzle').filter(x=>x.endsWith('.sql')).sort())sql.exec(readFileSync('drizzle/'+file,'utf8'));
 const db={prepare(q){let args=[];const all=()=>{const s=sql.prepare(q);return s.columns().length?s.all(...args):((s.run(...args)),[])};return {bind(...a){args=a;return this},async first(){return sql.prepare(q).get(...args)||null},async all(){return {results:all()}},async run(){return {results:all()}},execute(){return {results:all()}}}},async batch(statements){sql.exec('BEGIN');try{const results=statements.map(s=>s.execute());sql.exec('COMMIT');return results}catch(e){sql.exec('ROLLBACK');throw e}}};
 const env={DB:db,BIOME_OAUTH_ORIGIN:origin,BIOME_OWNER_EMAIL:owner['oai-authenticated-user-email'],BIOME_SERVICE_TOKEN:'existing-service-credential'};env.ownerPrincipal=req=>ownerPrincipal(req,env);
 const request=(action,body,headers={})=>new Request(origin+'/oauth/'+action,{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded',...headers},body:new URLSearchParams(body)});
 const form=(action,body,headers={})=>oauth(request(action,body,headers),env,action);
 const register=async(metadata={})=>oauth(new Request(origin+'/oauth/register',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({client_name:'Grok',redirect_uris:[redirect],...metadata})}),env,'register');
 async function setup(){const registered=await register();assert.equal(registered.status,201);const {client_id}=await registered.json(),verifier='v'.repeat(43);const q=new URLSearchParams({client_id,redirect_uri:redirect,response_type:'code',resource:origin+'/api/mcp',scope:SCOPE,state:'grok-state',code_challenge:await digest(verifier),code_challenge_method:'S256'});return {client_id,verifier,q};}
 const consent=async q=>oauth(new Request(origin+'/oauth/authorize?'+q,{headers:owner}),env,'authorize');
 const ticket=async q=>/name="ticket" value="([A-Za-z0-9_-]+)"/.exec(await (await consent(q)).text())[1];
 async function approve(q,decision='allow'){const response=await form('authorize',{ticket:await ticket(q),decision},{...owner,origin});assert.equal(response.status,200);assert.equal(response.headers.get('referrer-policy'),'no-referrer');const html=await response.text(),link=/id="oauth-continue" href="([^"]+)"/.exec(html)[1].replaceAll('&amp;','&');return new URL(link);}
 const exchange=(s,code,more={})=>form('token',{client_id:s.client_id,code,code_verifier:s.verifier,grant_type:'authorization_code',redirect_uri:redirect,resource:origin+'/api/mcp',...more});
 const bearer=token=>bearerPrincipal(new Request(origin+'/api/mcp',{headers:{authorization:'Bearer '+token}}),env);
 return {sql,env,request,form,register,setup,consent,ticket,approve,exchange,bearer};
}
test('discovery advertises exact resource, DCR and S256; authentication cannot be supplied as labels',async()=>{
 const f=fixture();assert.equal((await resourceMetadata(f.env).json()).resource,origin+'/api/mcp');const metadata=await serverMetadata(f.env).json();assert.equal(metadata.registration_endpoint,origin+'/oauth/register');assert.deepEqual(metadata.code_challenge_methods_supported,['S256']);
 for(const uri of ['javascript:alert(1)','http://grok.com/callback','https://grok.com/callback#fragment','https://user:pass@grok.com/'])assert.equal((await f.register({redirect_uris:[uri]})).status,400);
 assert.equal((await f.register({token_endpoint_auth_method:'client_secret_post'})).status,400);
 const s=await f.setup();const anon=await oauth(new Request(origin+'/oauth/authorize?'+s.q),f.env,'authorize');assert.equal(anon.status,303);assert.match(anon.headers.get('location'),/^\/signin-with-chatgpt\?/);
 const impostor=await oauth(new Request(origin+'/oauth/authorize?'+s.q,{headers:{...owner,'oai-authenticated-user-email':'other@example.test'}}),f.env,'authorize');assert.equal(impostor.status,403);
 f.sql.close();
});
test('owner consent, CSRF, PKCE, exact binding, denial and completion link',async()=>{
 const f=fixture(),s=await f.setup(),t=await f.ticket(s.q);
 const page=await f.consent(s.q);assert.equal(page.headers.get('referrer-policy'),'same-origin');assert.match(page.headers.get('content-security-policy'),/form-action 'self' https:\/\/grok.com/);
 for(const bad of ['null','','https://evil.test'])assert.equal((await f.form('authorize',{ticket:t,decision:'allow'},{...owner,origin:bad})).status,403);
 const approved=await f.form('authorize',{ticket:t,decision:'allow'},{...owner,origin});assert.equal(approved.status,200);assert.equal(approved.headers.get('location'),null);
 assert.equal((await f.form('authorize',{ticket:t,decision:'allow'},{...owner,origin})).status,400);
 const callback=await f.approve(s.q);assert.equal(callback.searchParams.get('state'),'grok-state');assert.equal(callback.searchParams.get('iss'),origin);const code=callback.searchParams.get('code');
 for(const bad of [{code_verifier:'x'.repeat(43)},{resource:'https://other.test/api/mcp'},{redirect_uri:'https://grok.com/other'},{scope:'admin'}])assert.equal((await f.exchange(s,code,bad)).status,400);
 const another=await f.setup();assert.equal((await f.exchange(s,code,{client_id:another.client_id})).status,400);
 const token=await (await f.exchange(s,code)).json();assert.equal(await f.bearer(token.access_token),'owner-principal');assert.equal((await f.exchange(s,code)).status,400);
 const denied=await f.approve(s.q,'deny');assert.equal(denied.searchParams.get('error'),'access_denied');assert.equal(denied.searchParams.get('code'),null);
 const rows=f.sql.prepare('SELECT hash FROM biome_oauth_tokens').all();assert.ok(!rows.some(r=>[token.access_token,token.refresh_token].includes(r.hash)));
 await f.form('revoke',{client_id:s.client_id,token:token.refresh_token});assert.equal(await f.bearer(token.access_token),null);f.sql.close();
});
test('concurrent code exchange and refresh reuse cannot duplicate execution authority',async()=>{
 const f=fixture(),s=await f.setup(),callback=await f.approve(s.q),code=callback.searchParams.get('code');
 const race=await Promise.all([f.exchange(s,code),f.exchange(s,code)]);assert.deepEqual(race.map(r=>r.status).sort(),[200,400]);const token=await race.find(r=>r.status===200).json();
 const refresh=()=>f.form('token',{client_id:s.client_id,grant_type:'refresh_token',refresh_token:token.refresh_token,resource:origin+'/api/mcp'});
 const rotated=await Promise.all([refresh(),refresh()]);assert.deepEqual(rotated.map(r=>r.status).sort(),[200,400]);const next=await rotated.find(r=>r.status===200).json();assert.equal(await f.bearer(next.access_token),null);assert.equal(await f.bearer(token.access_token),null);f.sql.close();
});
test('expiry, unknown bearer and registered redirect tampering are rejected',async()=>{
 const f=fixture(),s=await f.setup();assert.equal(await f.bearer('x'.repeat(43)),null);
 const bad=new URLSearchParams(s.q);bad.set('redirect_uri','https://evil.test/');assert.equal((await f.consent(bad)).status,400);
 const callback=await f.approve(s.q),code=callback.searchParams.get('code');f.sql.prepare('UPDATE biome_oauth_codes SET expires=0').run();assert.equal((await f.exchange(s,code)).status,400);f.sql.close();
});
test('meta-muse is pre-registered with an exact callback and mandatory PKCE',async()=>{
 const f=fixture(),callback='https://agent.meta.ai/api/hatch/oauth/callback',verifier='m'.repeat(43);
 const q=new URLSearchParams({client_id:'meta-muse',redirect_uri:callback,response_type:'code',resource:origin+'/api/mcp',scope:SCOPE,state:'muse-state',code_challenge:await digest(verifier),code_challenge_method:'S256'});
 assert.equal(f.sql.prepare('SELECT count(*) n FROM biome_oauth_clients').get().n,0);
 const anon=await oauth(new Request(origin+'/oauth/authorize?'+q),f.env,'authorize');assert.equal(anon.status,303);
 const consent=await f.consent(q);assert.equal(consent.status,200);assert.match(await consent.text(),/Meta Muse/);
 for(const [key,value] of [['redirect_uri',callback+'/'],['redirect_uri',callback+'?extra=1'],['redirect_uri','https://evil.test/callback'],['client_id','META-MUSE'],['code_challenge_method','plain'],['code_challenge','']]){
  const bad=new URLSearchParams(q);bad.set(key,value);assert.equal((await f.consent(bad)).status,400);
 }
 const issued=await f.register({client_id:'meta-muse',redirect_uris:['https://evil.test/callback']});assert.notEqual((await issued.json()).client_id,'meta-muse');
 const returned=await f.approve(q);assert.equal(returned.origin+returned.pathname,callback);assert.equal(returned.searchParams.get('state'),'muse-state');
 const exchange={client_id:'meta-muse',grant_type:'authorization_code',code:returned.searchParams.get('code'),code_verifier:verifier,redirect_uri:callback,resource:origin+'/api/mcp'};
 assert.equal((await f.form('token',{...exchange,code_verifier:'x'.repeat(43)})).status,400);
 assert.equal((await f.form('token',{...exchange,redirect_uri:callback+'/'})).status,400);
 const response=await f.form('token',exchange);assert.equal(response.status,200);const token=await response.json();
 assert.equal(await f.bearer(token.access_token),'owner-principal');assert.ok(token.refresh_token);assert.equal(token.scope,SCOPE);
 assert.equal((await f.form('token',exchange)).status,400);
 const connections=await oauth(new Request(origin+'/oauth/connections',{headers:owner}),f.env,'connections');assert.match(await connections.text(),/Meta Muse/);
 // The public registration persists across anonymous-client cleanup and access expiry.
 f.sql.prepare('DELETE FROM biome_oauth_clients').run();f.sql.prepare("UPDATE biome_oauth_tokens SET expires=0 WHERE kind='access'").run();assert.equal(await f.bearer(token.access_token),null);
 const refreshed=await f.form('token',{client_id:'meta-muse',grant_type:'refresh_token',refresh_token:token.refresh_token,resource:origin+'/api/mcp'});assert.equal(refreshed.status,200);
 const rotated=await refreshed.json();assert.notEqual(rotated.refresh_token,token.refresh_token);assert.equal(await f.bearer(rotated.access_token),'owner-principal');
 await f.form('revoke',{client_id:'meta-muse',token:rotated.refresh_token});assert.equal(await f.bearer(rotated.access_token),null);assert.equal((await f.consent(q)).status,200);f.sql.close();
});
test('production Muse legacy corm scope maps only to BiomeRTE for meta-muse',async()=>{
 const f=fixture(),callback='https://agent.meta.ai/api/hatch/oauth/callback',verifier='m'.repeat(43);
 // Nonsecret parameters match production request c96f453fe915311bed9f5eea72046217.
 const q=new URLSearchParams({response_type:'code',client_id:'meta-muse',redirect_uri:callback,scope:'corm',resource:origin+'/api/mcp',state:'sanitized-production-replay',code_challenge_method:'S256',code_challenge:await digest(verifier)});
 assert.deepEqual((await serverMetadata(f.env).json()).scopes_supported,[SCOPE,'offline_access']);
 const login=await oauth(new Request(origin+'/oauth/authorize?'+q),f.env,'authorize');assert.equal(login.status,303);
 const returned=await f.approve(q),code=returned.searchParams.get('code');
 const stored=JSON.parse(f.sql.prepare('SELECT request FROM biome_oauth_codes WHERE hash=?').get(await digest(code)).request);assert.equal(stored.scope,SCOPE);assert.equal(stored.resource,origin+'/api/mcp');
 const exchange={client_id:'meta-muse',grant_type:'authorization_code',code,code_verifier:verifier,redirect_uri:callback,resource:origin+'/api/mcp',scope:'corm'};
 for(const bad of [{scope:'admin'},{resource:'https://context-store.test/api/mcp'},{redirect_uri:callback+'/'},{code_verifier:'x'.repeat(43)}])assert.equal((await f.form('token',{...exchange,...bad})).status,400);
 const response=await f.form('token',exchange);assert.equal(response.status,200);const token=await response.json();assert.equal(token.scope,SCOPE);assert.ok(token.refresh_token);assert.equal(await f.bearer(token.access_token),'owner-principal');
 const refresh={client_id:'meta-muse',grant_type:'refresh_token',refresh_token:token.refresh_token,resource:origin+'/api/mcp',scope:'corm'};
 assert.equal((await f.form('token',{...refresh,scope:'admin'})).status,400);
 const refreshed=await f.form('token',refresh);assert.equal(refreshed.status,200);const rotated=await refreshed.json();assert.equal(rotated.scope,SCOPE);assert.equal(await f.bearer(rotated.access_token),'owner-principal');
 await f.form('revoke',{client_id:'meta-muse',token:rotated.refresh_token});assert.equal(await f.bearer(rotated.access_token),null);
 const dynamic=await f.setup();dynamic.q.set('scope','corm');const dynamicFailure=await f.consent(dynamic.q);assert.equal(dynamicFailure.status,400);assert.equal((await dynamicFailure.json()).error,'invalid_scope');
 for(const [key,value,expected] of [['scope','admin','invalid_scope'],['resource','https://context-store.test/api/mcp','invalid_target'],['redirect_uri',callback+'/','invalid_request'],['code_challenge_method','plain','invalid_request'],['state','','invalid_request']]){
  const bad=new URLSearchParams(q);bad.set(key,value);const r=await f.consent(bad);assert.equal(r.status,400);const body=await r.text();assert.equal(JSON.parse(body).error,expected);assert.ok(!body.includes(q.get('state')));assert.ok(!body.includes(q.get('code_challenge')));
 }
 f.sql.close();
});
