// Direct MCP OAuth. Browser identity remains owned by Sites; grants never accept
// a caller-supplied principal. Only SHA-256 token/code digests are persisted.
export const SCOPE='biomerte';
// Deployment-owned public clients are stable and cannot be overwritten or
// expired by anonymous dynamic registration or its cleanup.
const registeredClients=new Map([['meta-muse',{
 client_name:'Meta Muse',
 redirect_uris:['https://agent.meta.ai/api/hatch/oauth/callback'],
 token_endpoint_auth_method:'none',
 grant_types:['authorization_code','refresh_token'],
 response_types:['code']
}]]);
const TTL=3600, REFRESH_TTL=30*86400;
const enc=new TextEncoder();
const b64=b=>btoa(String.fromCharCode(...b)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
export const digest=async s=>b64(new Uint8Array(await crypto.subtle.digest('SHA-256',enc.encode(s))));
const random=()=>b64(crypto.getRandomValues(new Uint8Array(32)));
const now=()=>Math.floor(Date.now()/1000);
const safe=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
export function configuration(env){const origin=env.BIOME_OAUTH_ORIGIN || 'https://biomerte-controller.cantikash.chatgpt.site';if(!origin||new URL(origin).origin!==origin||!origin.startsWith('https://'))throw Error('BIOME_OAUTH_ORIGIN must be a canonical HTTPS origin');return {origin,resource:origin+'/api/mcp'};}
const json=(data,status=200,extra={})=>Response.json(data,{status,headers:{'Cache-Control':'no-store','Pragma':'no-cache',...extra}});
const failure=(error,description,status=400)=>json({error,error_description:description},status);
class OAuthRequestError extends Error{constructor(code,message){super(message);this.code=code;}}
// Muse can retain the legacy Humus scope in its connector recipe. This alias
// applies only to the pre-registered Muse client; resource and grant bindings
// still select this BiomeRTE server, and issued scope is always canonical.
function clientScope(id, value) {
 const scopes = (value || SCOPE).split(/\s+/).filter(Boolean);
 if (id === 'meta-muse' && scopes.includes('corm')) scopes[scopes.indexOf('corm')] = SCOPE;
 return scopes.includes(SCOPE) && scopes.every(s => s === SCOPE || s === 'offline_access') ? SCOPE : value;
}
export function resourceMetadata(env){const {origin,resource}=configuration(env);return json({resource,authorization_servers:[origin],scopes_supported:[SCOPE],resource_name:'BiomeRTE',bearer_methods_supported:['header']});}
export function serverMetadata(env){const {origin}=configuration(env);return json({issuer:origin,authorization_endpoint:origin+'/oauth/authorize',token_endpoint:origin+'/oauth/token',registration_endpoint:origin+'/oauth/register',revocation_endpoint:origin+'/oauth/revoke',response_types_supported:['code'],grant_types_supported:['authorization_code','refresh_token'],token_endpoint_auth_methods_supported:['none'],code_challenge_methods_supported:['S256'],scopes_supported:[SCOPE,'offline_access'],authorization_response_iss_parameter_supported:true});}
export function challenge(env){const {origin}=configuration(env);return `Bearer resource_metadata="${origin}/.well-known/oauth-protected-resource", scope="${SCOPE}"`;}
async function bounded(req){if(Number(req.headers.get('content-length'))>16384)throw Error('Request too large');const reader=req.body?.getReader();let n=0,text='';if(!reader)return '';const decoder=new TextDecoder();for(;;){const {value,done}=await reader.read();if(done)break;n+=value.length;if(n>16384){await reader.cancel();throw Error('Request too large');}text+=decoder.decode(value,{stream:true});}return text+decoder.decode();}
async function client(db,id){if(registeredClients.has(id))return registeredClients.get(id);const row=await db.prepare('SELECT metadata FROM biome_oauth_clients WHERE id=? AND expires>?').bind(id,now()).first();return row?JSON.parse(row.metadata):null;}
export async function browserPrincipal(req,env){return await env.ownerPrincipal(req);}
export async function bearerPrincipal(req,env){const match=/^Bearer ([A-Za-z0-9_-]{43})$/.exec(req.headers.get('authorization')||'');if(!match)return null;const row=await env.DB.prepare("SELECT principal,resource FROM biome_oauth_tokens WHERE hash=? AND kind='access' AND expires>?").bind(await digest(match[1]),now()).first();if(row?.resource!==configuration(env).resource)return null;const owner=await env.DB.prepare('SELECT user_id FROM biome_owner WHERE singleton=1').first();return owner?.user_id===row.principal?row.principal:null;}
function redirectError(url,error,env){url.searchParams.set('error',error);url.searchParams.set('iss',configuration(env).origin);return callbackCompletion(url,true);}
function callbackCompletion(redirect,denied=false){
  // End the form navigation here. A real user-activated normal link starts a
  // separate navigation, rather than dragging the callback/app redirect chain
  // through the consent form's form-action policy. No new trust or grant state.
  const callbackOrigin=redirect.origin;
  const title=denied?'Connection cancelled':'Consent accepted';
  const message=denied?'Access was not granted. Tap Continue to return to your app.':'Continue to your app to finish connecting. Your app will complete the connection after you return.';
  const guidance=denied?'No new access was granted.':'This page only confirms your consent. Check connection status in your app. This one-use link expires shortly.';
  const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>${title}</title><style>body{background:#30251f;color:#f5f0e6;font:16px system-ui;max-width:560px;margin:10vh auto;padding:28px}h1{font-size:32px}p{line-height:1.6;overflow-wrap:anywhere}a{display:inline-block;padding:12px 20px;border:1px solid #768875;background:#c58d5b;color:#30251f;text-decoration:none}small{color:#cdbda8}</style></head><body><h1>${title}</h1><p>${message}</p><p><a id="oauth-continue" href="${safe(redirect.href)}" rel="noreferrer">Continue to your app</a></p><p><small>Return to ${safe(callbackOrigin)}. ${guidance}</small></p></body></html>`;
  return new Response(html,{status:200,headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Pragma':'no-cache','Referrer-Policy':'no-referrer','Content-Security-Policy':`default-src 'none'; style-src 'unsafe-inline'; form-action 'self' ${callbackOrigin}; frame-ancestors 'none'; base-uri 'none'`,'X-Frame-Options':'DENY','X-Content-Type-Options':'nosniff'}});
}
async function validateAuthorization(q,env){
 const c=await client(env.DB,q.client_id);
 if(!c||!c.redirect_uris.includes(q.redirect_uri))throw new OAuthRequestError('invalid_request','Unknown client or unregistered redirect URI');
 if(q.response_type!=='code')throw new OAuthRequestError('unsupported_response_type','Use response_type=code');
 if(q.resource!==configuration(env).resource)throw new OAuthRequestError('invalid_target','Exact BiomeRTE resource required');
 q.scope=clientScope(q.client_id,q.scope);
 if(q.scope!==SCOPE)throw new OAuthRequestError('invalid_scope','BiomeRTE requires the biomerte scope');
 if(q.code_challenge_method!=='S256'||!/^[A-Za-z0-9_-]{43}$/.test(q.code_challenge||''))throw new OAuthRequestError('invalid_request','A valid S256 PKCE challenge is required');
 if(!q.state||q.state.length>2048)throw new OAuthRequestError('invalid_request','A nonempty state of at most 2048 characters is required');
 return c;
}
export async function oauth(req,env,action){
 try {
 configuration(env);
 const method=req.method;
 if(action==='register'&&method==='POST'){
  if(!req.headers.get('content-type')?.includes('application/json'))return failure('invalid_request','Use application/json');
  const m=JSON.parse(await bounded(req));
  if(!Array.isArray(m.redirect_uris)||m.redirect_uris.length<1||m.redirect_uris.length>5||m.redirect_uris.some(u=>{try{const x=new URL(u);return x.protocol!=='https:'||!!x.hash||!!x.username||!!x.password||u.length>2048;}catch{return true;}}))return failure('invalid_redirect_uri','Use 1–5 exact HTTPS redirect URIs without fragments or credentials');
  if(m.token_endpoint_auth_method&&m.token_endpoint_auth_method!=='none'||m.grant_types&&(!Array.isArray(m.grant_types)||m.grant_types.some(x=>!['authorization_code','refresh_token'].includes(x)))||m.response_types&&(!Array.isArray(m.response_types)||m.response_types.some(x=>x!=='code')))return failure('invalid_client_metadata','Only public PKCE clients and code/refresh grants are supported');
  const id='biome_'+random(),meta={redirect_uris:[...new Set(m.redirect_uris)],client_name:String(m.client_name||'MCP client').slice(0,100),token_endpoint_auth_method:'none',grant_types:['authorization_code','refresh_token'],response_types:['code']};
  await env.DB.batch(['biome_oauth_clients','biome_oauth_pending','biome_oauth_codes','biome_oauth_tokens'].map(table=>env.DB.prepare(`DELETE FROM ${table} WHERE expires<?`).bind(now())));
  // Reclaim abandoned registrations instead of letting anonymous registrations
  // exhaust a global pool. Never reclaim a live grant or consent/code handshake.
  await env.DB.prepare(`DELETE FROM biome_oauth_clients WHERE id IN (
   SELECT c.id FROM biome_oauth_clients c WHERE
   NOT EXISTS(SELECT 1 FROM biome_oauth_tokens t WHERE t.client_id=c.id AND t.expires>?) AND
   NOT EXISTS(SELECT 1 FROM biome_oauth_pending p WHERE json_extract(p.request,'$.client_id')=c.id AND p.expires>?) AND
   NOT EXISTS(SELECT 1 FROM biome_oauth_codes p WHERE json_extract(p.request,'$.client_id')=c.id AND p.expires>?)
   ORDER BY c.expires DESC,c.rowid DESC LIMIT -1 OFFSET 199
  )`).bind(now(),now(),now()).run();
  await env.DB.prepare('INSERT INTO biome_oauth_clients(id,metadata,expires) VALUES(?,?,?)').bind(id,JSON.stringify(meta),now()+REFRESH_TTL).run();
  return json({client_id:id,client_id_issued_at:now(),...meta},201);
 }
 if(action==='authorize'&&method==='GET'){
  const url=new URL(req.url),q=Object.fromEntries(url.searchParams),c=await validateAuthorization(q,env);
  if(!req.headers.get('oai-authenticated-user-id'))return new Response(null,{status:303,headers:{Location:'/signin-with-chatgpt?return_to='+encodeURIComponent(url.pathname+url.search),'Cache-Control':'no-store'}});
  const principal=await browserPrincipal(req,env);if(!principal)return failure('access_denied','Only the BiomeRTE owner can authorize access',403);
  const ticket=random();await env.DB.prepare('INSERT INTO biome_oauth_pending(hash,principal,request,expires) VALUES(?,?,?,?)').bind(await digest(ticket),principal,JSON.stringify(q),now()+600).run();
  const callbackOrigin=new URL(q.redirect_uri).origin;
  const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Connect BiomeRTE</title><style>body{background:#30251f;color:#f5f0e6;font:16px system-ui;max-width:560px;margin:10vh auto;padding:28px}h1{font-size:32px}p{line-height:1.6;overflow-wrap:anywhere}button{font:inherit;padding:12px 20px;border:1px solid #768875;background:#c58d5b;color:#30251f;margin-right:10px;cursor:pointer}small{color:#cdbda8}</style></head><body><h1>Connect BiomeRTE</h1><p>Allow <strong>${safe(c.client_name)}</strong> to read and modify work, run routines, and invoke your controller’s capabilities with your operator permissions?</p><p><small>Return address: ${safe(q.redirect_uri)}</small></p><form method="post" action="/oauth/authorize"><input type="hidden" name="ticket" value="${ticket}"><button name="decision" value="allow">Allow access</button><button name="decision" value="deny">Cancel</button></form><p><small>Access applies to your existing BiomeRTE owner workspace. You can revoke it from BiomeRTE’s connection page.</small></p></body></html>`;
  return new Response(html,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Content-Security-Policy':`default-src 'none'; style-src 'unsafe-inline'; form-action 'self' ${callbackOrigin}; frame-ancestors 'none'; base-uri 'none'`,'X-Frame-Options':'DENY','Referrer-Policy':'same-origin'}});
 }
 if(action==='authorize'&&method==='POST'){
  if(req.headers.get('origin')!==configuration(env).origin)return failure('access_denied','Same-origin consent required',403);
  const principal=await browserPrincipal(req,env);if(!principal)return failure('access_denied','Owner sign-in required',403);
  const f=new URLSearchParams(await bounded(req)),rows=await env.DB.prepare('DELETE FROM biome_oauth_pending WHERE hash=? AND principal=? AND expires>? RETURNING request').bind(await digest(f.get('ticket')||''),principal,now()).all();
  if(!rows.results.length)return failure('invalid_request','Consent expired or already used');const q=JSON.parse(rows.results[0].request);await validateAuthorization(q,env);
  const redirect=new URL(q.redirect_uri);redirect.searchParams.set('state',q.state);
  if(f.get('decision')!=='allow')return redirectError(redirect,'access_denied',env);
  const code=random();await env.DB.prepare('INSERT INTO biome_oauth_codes(hash,principal,request,expires) VALUES(?,?,?,?)').bind(await digest(code),principal,JSON.stringify(q),now()+120).run();redirect.searchParams.set('code',code);redirect.searchParams.set('iss',configuration(env).origin);return callbackCompletion(redirect);
 }
 if(action==='token'&&method==='POST'){
  if(!req.headers.get('content-type')?.startsWith('application/x-www-form-urlencoded'))return failure('invalid_request','Use form encoding');
  const f=new URLSearchParams(await bounded(req)),id=f.get('client_id');if(!await client(env.DB,id))return failure('invalid_client','Unknown or expired client',401);
  if(f.get('resource')!==configuration(env).resource)return failure('invalid_target','Exact BiomeRTE resource required');
  if(f.has('scope')&&clientScope(id,f.get('scope'))!==SCOPE)return failure('invalid_scope','Only biomerte scope is supported');
  const at=random(),rt=random(),ah=await digest(at),rh=await digest(rt);let principal,grant;
  if(f.get('grant_type')==='authorization_code'){
   const ch=await digest(f.get('code')||''),row=await env.DB.prepare('SELECT * FROM biome_oauth_codes WHERE hash=? AND expires>?').bind(ch,now()).first();if(!row)return failure('invalid_grant','Code invalid, expired or used');const q=JSON.parse(row.request),verifier=f.get('code_verifier')||'';
   if(q.client_id!==id||q.redirect_uri!==f.get('redirect_uri')||q.resource!==f.get('resource')||!/^[A-Za-z0-9._~-]{43,128}$/.test(verifier)||await digest(verifier)!==q.code_challenge)return failure('invalid_grant','Code binding or PKCE mismatch');principal=row.principal;grant=random();
   const rs=await env.DB.batch([
    env.DB.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) SELECT ?,'access',?,?,?,?,? WHERE EXISTS(SELECT 1 FROM biome_oauth_codes WHERE hash=? AND expires>?) RETURNING hash").bind(ah,grant,id,principal,q.resource,now()+TTL,ch,now()),
    env.DB.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) SELECT ?,'refresh',?,?,?,?,? WHERE EXISTS(SELECT 1 FROM biome_oauth_tokens WHERE hash=?)").bind(rh,grant,id,principal,q.resource,now()+REFRESH_TTL,ah),
    env.DB.prepare('DELETE FROM biome_oauth_codes WHERE hash=?').bind(ch)
   ]);if(!rs[0].results.length)return failure('invalid_grant','Code already used');
  } else if(f.get('grant_type')==='refresh_token'){
   const old=await digest(f.get('refresh_token')||''),row=await env.DB.prepare("SELECT * FROM biome_oauth_tokens WHERE hash=? AND kind IN ('refresh','spent') AND client_id=? AND resource=? AND expires>?").bind(old,id,f.get('resource'),now()).first();if(!row)return failure('invalid_grant','Refresh token invalid, expired or used');principal=row.principal;grant=row.grant_id;
   if(row.kind==='spent'){await env.DB.prepare('DELETE FROM biome_oauth_tokens WHERE grant_id=?').bind(grant).run();return failure('invalid_grant','Refresh token reuse detected; authorize again');}
   const rs=await env.DB.batch([
    env.DB.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) SELECT ?,'refresh',grant_id,client_id,principal,resource,? FROM biome_oauth_tokens WHERE hash=? AND kind='refresh' AND expires>? RETURNING hash").bind(rh,now()+REFRESH_TTL,old,now()),
    env.DB.prepare("INSERT INTO biome_oauth_tokens(hash,kind,grant_id,client_id,principal,resource,expires) SELECT ?,'access',?,?,?,?,? WHERE EXISTS(SELECT 1 FROM biome_oauth_tokens WHERE hash=?) RETURNING hash").bind(ah,grant,id,principal,row.resource,now()+TTL,rh),
    env.DB.prepare("UPDATE biome_oauth_tokens SET kind='spent' WHERE hash=? AND EXISTS(SELECT 1 FROM biome_oauth_tokens WHERE hash=?)").bind(old,rh),
    env.DB.prepare("UPDATE biome_oauth_tokens SET expires=? WHERE grant_id=? AND kind='spent' AND EXISTS(SELECT 1 FROM biome_oauth_tokens WHERE hash=?)").bind(now()+REFRESH_TTL,grant,rh)
   ]);if(!rs[0].results.length){await env.DB.prepare('DELETE FROM biome_oauth_tokens WHERE grant_id=?').bind(grant).run();return failure('invalid_grant','Refresh token reuse detected; authorize again');}
  }else return failure('unsupported_grant_type','Use authorization_code or refresh_token');
  if(!registeredClients.has(id))await env.DB.prepare('UPDATE biome_oauth_clients SET expires=? WHERE id=?').bind(now()+REFRESH_TTL,id).run();
  return json({access_token:at,token_type:'Bearer',expires_in:TTL,refresh_token:rt,scope:SCOPE});
 }
 if(action==='connections'&&method==='GET'){
  const principal=await browserPrincipal(req,env);if(!principal)return new Response(null,{status:303,headers:{Location:'/signin-with-chatgpt?return_to=%2Foauth%2Fconnections'}});
  const grants=await env.DB.prepare("SELECT DISTINCT t.grant_id,t.client_id,c.metadata FROM biome_oauth_tokens t LEFT JOIN biome_oauth_clients c ON c.id=t.client_id WHERE t.principal=? AND t.expires>?").bind(principal,now()).all();
  return new Response('<!doctype html><html><head><meta charset="utf-8"><title>BiomeRTE connections</title></head><body><h1>BiomeRTE connections</h1>'+grants.results.map(g=>'<form method="post" action="/oauth/disconnect"><p>'+safe(registeredClients.get(g.client_id)?.client_name || (g.metadata?JSON.parse(g.metadata).client_name:'MCP client'))+'</p><input type="hidden" name="grant_id" value="'+safe(g.grant_id)+'"><button>Revoke access</button></form>').join('')+'<p><a href="/">Return to BiomeRTE</a></p></body></html>',{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",'Referrer-Policy':'same-origin'}});
 }
 if(action==='disconnect'&&method==='POST'){
  const principal=await browserPrincipal(req,env);if(!principal||req.headers.get('origin')!==configuration(env).origin)return failure('access_denied','Same-origin owner request required',403);
  const f=new URLSearchParams(await bounded(req));await env.DB.prepare('DELETE FROM biome_oauth_tokens WHERE grant_id=? AND principal=?').bind(f.get('grant_id'),principal).run();return new Response(null,{status:303,headers:{Location:'/oauth/connections','Cache-Control':'no-store'}});
 }
 if(action==='revoke'&&method==='POST'){
  const f=new URLSearchParams(await bounded(req));await env.DB.prepare('DELETE FROM biome_oauth_tokens WHERE grant_id IN (SELECT grant_id FROM biome_oauth_tokens WHERE hash=? AND client_id=?)').bind(await digest(f.get('token')||''),f.get('client_id')||'').run();return new Response(null,{status:200,headers:{'Cache-Control':'no-store'}});
 }
 return failure('invalid_request','Unsupported endpoint or method',405);
 }catch(e){console.error('biome_oauth_error',{action,error:e instanceof OAuthRequestError?e.code:'internal_error'});if(e instanceof OAuthRequestError)return failure(e.code,e.message);return failure('invalid_request','Invalid OAuth request or unavailable OAuth configuration');}
}
