import {ownerPrincipal,privateHeaders} from './access.mjs';
import {limitedJSON,enqueue} from './bridge.mjs';
import {defaultMappings} from './dashboard-defaults.mjs';
const mappingKinds=new Set(['workflow','run','capability','namespace','resource','instance','document','operation','event']);
const uiReply=(body,status=200)=>Response.json(body,{status,headers:{...privateHeaders,'X-Content-Type-Options':'nosniff'}});
async function uiOwner(req,env){return ownerPrincipal(req,env);}
function uiSameOrigin(req){return req.headers.get('Origin')===new URL(req.url).origin&&!['cross-site','same-site'].includes(req.headers.get('Sec-Fetch-Site'));}
function cleanMapping(body){
 if(!body||typeof body!=='object'||Array.isArray(body)||!mappingKinds.has(body.kind)||typeof body.identity!=='string'||!body.identity||body.identity.length>256||!['site','local'].includes(body.realm)||!body.metadata||typeof body.metadata!=='object'||Array.isArray(body.metadata))throw Error('Invalid mapping');
 if(Object.keys(body).some(k=>!['realm','kind','identity','metadata'].includes(k)))throw Error('Unexpected mapping field');
 const meta=body.metadata;
 if(Object.keys(meta).some(k=>!['name','summary','steps'].includes(k)))throw Error('Unexpected metadata field');
 if(typeof meta.name!=='string'||!meta.name.trim()||Array.from(meta.name).length>80||typeof (meta.summary??'')!=='string'||Array.from(meta.summary??'').length>240)throw Error('Use a name up to 80 characters and a description up to 240 characters');
 const result={name:meta.name.trim(),summary:meta.summary??''};
 if(meta.steps!==undefined){
  if(body.kind!=='workflow'||!meta.steps||typeof meta.steps!=='object'||Array.isArray(meta.steps)||Object.keys(meta.steps).length>500)throw Error('Invalid step metadata');
  result.steps=Object.fromEntries(Object.entries(meta.steps).map(([id,value])=>{
   if(!id||id.length>256||!value||typeof value!=='object'||Object.keys(value).some(k=>!['name','summary'].includes(k))||typeof value.name!=='string'||!value.name.trim()||Array.from(value.name).length>80||typeof(value.summary??'')!=='string'||Array.from(value.summary??'').length>240)throw Error('Invalid step name');
   return [id,{name:value.name.trim(),summary:value.summary??''}];
  }));
 }
 return {realm:body.realm,kind:body.kind,identity:body.identity,metadata:result};
}
export async function dashboardMappings(req,env){
 const owner=await uiOwner(req,env);if(!owner)return uiReply({error:'Controller owner sign-in required'},req.headers.get('oai-authenticated-user-id')?403:401);
 if(!['GET','POST'].includes(req.method))return new Response(null,{status:405,headers:{...privateHeaders,Allow:'GET, POST'}});
 if(req.method==='POST'&&!uiSameOrigin(req))return uiReply({error:'Same-origin request required'},403);
 let body;
 if(req.method==='POST'){
  if(req.headers.get('content-type')?.split(';')[0]!=='application/json')return uiReply({error:'Expected application/json'},415);
  try{body=cleanMapping(await limitedJSON(req,60000));}catch(error){return uiReply({error:error.message},400);}
 }
 const realm=body?.realm||new URL(req.url).searchParams.get('realm')||'site';if(!['site','local'].includes(realm))return uiReply({error:'Invalid workspace'},400);
 await env.DB.prepare('CREATE TABLE IF NOT EXISTS dashboard_mappings (realm TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL, metadata TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, PRIMARY KEY(realm,kind,identity))').run();
 if(body){
  const now=new Date().toISOString();
  await env.DB.prepare('INSERT INTO dashboard_mappings(realm,kind,identity,metadata,updated_at,updated_by) VALUES(?,?,?,?,?,?) ON CONFLICT(realm,kind,identity) DO UPDATE SET metadata=excluded.metadata,updated_at=excluded.updated_at,updated_by=excluded.updated_by').bind(realm,body.kind,body.identity,JSON.stringify(body.metadata),now,owner).run();
  return uiReply({mapping:{kind:body.kind,identity:body.identity,metadata:body.metadata,updated_at:now}});
 }
 const records=await env.DB.prepare('SELECT kind,identity,metadata,updated_at FROM dashboard_mappings WHERE realm=? ORDER BY kind,identity').bind(realm).all();
 const result=new Map(defaultMappings.map(m=>[m.kind+':'+m.identity,m]));
 for(const row of records.results||[]){const key=row.kind+':'+row.identity,base=result.get(key)?.metadata||{},saved=JSON.parse(row.metadata);result.set(key,{kind:row.kind,identity:row.identity,metadata:{...base,...saved,...((base.steps||saved.steps)?{steps:{...(base.steps||{}),...(saved.steps||{})}}:{})},updated_at:row.updated_at});}
 return uiReply({mappings:[...result.values()],realm});
}
export async function dashboardDefinition(req,env,deps={ownerPrincipal,enqueue}){
 const owner=await (deps.ownerPrincipal||ownerPrincipal)(req,env);if(!owner)return uiReply({error:'Controller owner sign-in required'},req.headers.get('oai-authenticated-user-id')?403:401);
 if(req.method!=='POST')return new Response(null,{status:405,headers:{...privateHeaders,Allow:'POST'}});
 if(!uiSameOrigin(req))return uiReply({error:'Same-origin request required'},403);
 let args;
 try{
  args=await limitedJSON(req,3000);
  if(!args||typeof args!=='object'||Array.isArray(args)||Object.keys(args).some(k=>!['name','revision','realm','mode'].includes(k))||!['site','local'].includes(args.realm))throw Error();
  if(args.mode!=='summaries'&&(typeof args.name!=='string'||!args.name||args.name.length>256||typeof(args.revision??'')!=='string'||(args.revision&&!/^[a-f0-9]{64}$/.test(args.revision))))throw Error();
 }catch{return uiReply({error:'Invalid workflow request'},400)}
 const result=await (deps.enqueue||enqueue)(env,'biome_dashboard_definition',args,'dashboard-definition-'+crypto.randomUUID());
 if(result.isError)return uiReply({error:result.content?.find(c=>c.type==='text')?.text||'Workflow data unavailable'},503);
 try{const value=JSON.parse(result.content.find(c=>c.type==='text').text);if(!value.body||!Number.isInteger(value.status))throw Error();return uiReply(value.body,value.status)}catch{return uiReply({error:'Invalid workflow data response'},502)}
}
