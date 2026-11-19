// Durable logical scope metadata; payloads and ownership grants stay in flight.
import {reply} from './bridge.mjs';

const initialized=new WeakSet();
const hash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const fail=(message,status=503)=>Object.assign(Error(message),{status});
async function schema(db){
 if(initialized.has(db))return;
 await db.prepare('CREATE TABLE IF NOT EXISTS logical_service_scopes(site TEXT,kind TEXT,identity TEXT,service_id TEXT,authority TEXT,PRIMARY KEY(site,kind,identity))').run();
 initialized.add(db);
}
function origin(env){
 if(!env.SERVICE_AUTHORITY_ORIGIN)return null;
 let url;
 try{url=new URL(env.SERVICE_AUTHORITY_ORIGIN)}catch{throw fail('Invalid service authority origin')}
 if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/'||url.search||url.hash)
  throw fail('Invalid service authority origin');
 return url.origin;
}
function nativeAuthority(env,authority){
 if(!authority||!env.CONTROLLER_NODE_MAP)return false;
 let nodes;
 try{nodes=JSON.parse(env.CONTROLLER_NODE_MAP)}catch{throw fail('Invalid native controller map')}
 return nodes&&Object.hasOwn(nodes,authority);
}
function declaredScope(env,kind,identity){
 if(!env.SERVICE_AUTHORITY_DECLARATIONS)return null;
 let rows;
 try{rows=JSON.parse(env.SERVICE_AUTHORITY_DECLARATIONS)}catch{throw fail('Invalid logical scope declarations')}
 if(!Array.isArray(rows))throw fail('Invalid logical scope declarations');
 const identities=new Map();
 for(const row of rows){
  if(!row||typeof row.service_id!=='string'||!row.service_id||!hash(row.authority)||
     !Array.isArray(row.providers)||row.providers.some(value=>!hash(value))||
     nativeAuthority(env,row.authority))throw fail('Invalid logical scope declarations');
  for(const key of ['authority:'+row.authority,...row.providers.map(value=>'provider:'+value)]){
   const prior=identities.get(key);
   if(prior&&(prior.service_id!==row.service_id||prior.authority!==row.authority))
    throw fail('Ambiguous logical scope declaration');
   identities.set(key,{service_id:row.service_id,authority:row.authority});
  }
 }
 return identities.get(kind+':'+identity)??null;
}
export async function logicalServiceCall(env,selector,raw){
 let base;
 try{base=origin(env)}catch{return reply({error:'Invalid service authority origin'},503)}
 if(!base)return null;
 const keys=Object.keys(selector).filter(key=>selector[key]!==undefined);
 if(keys.length===0)return null;
 if(keys.length!==1||!['provider','authority'].includes(keys[0])||!hash(selector[keys[0]]))
  return reply({error:'Invalid logical authority selector'},400);
 const kind=keys[0],identity=selector[kind];
 // Existing native identities remain explicit destinations, including old
 // pinned leases and submissions. Logical services cannot claim these IDs.
 if(kind==='authority'&&nativeAuthority(env,identity))return null;
 let envelope;
 try{envelope=JSON.parse(raw)}catch{return reply({error:'Invalid signed envelope'},400)}
 try{
  const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
  if(!config)throw fail('Controller site is unavailable');
  if(envelope.site!==config.site)return reply({error:'Wrong controller site'},403);
  await schema(env.DB);
  let retained=await env.DB.prepare('SELECT service_id,authority FROM logical_service_scopes WHERE site=? AND kind=? AND identity=?').bind(config.site,kind,identity).first();
  const declared=declaredScope(env,kind,identity);
  if(declared){
   if(retained&&(retained.service_id!==declared.service_id||retained.authority!==declared.authority))
    throw fail('Logical service scope changed; reconcile its existing authority',409);
   await env.DB.prepare('INSERT INTO logical_service_scopes(site,kind,identity,service_id,authority) VALUES(?,?,?,?,?) ON CONFLICT(site,kind,identity) DO NOTHING').bind(config.site,kind,identity,declared.service_id,declared.authority).run();
   retained=await env.DB.prepare('SELECT service_id,authority FROM logical_service_scopes WHERE site=? AND kind=? AND identity=?').bind(config.site,kind,identity).first();
   if(!retained||retained.service_id!==declared.service_id||retained.authority!==declared.authority)
    throw fail('Logical service scope changed concurrently',409);
  }
  // Enrollment binds a provider to a logical service, never to a host. Other
  // native providers keep their existing path even when the witness is down.
  if(kind==='provider'&&!retained)return null;
  const resolved=await fetch(base+'/api/service-authorities/resolve',{
   method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({[kind]:identity}),
   redirect:'error',signal:AbortSignal.timeout(8000)
  });
  if(resolved.status===404){
   // A witness restart or lost host catalog cannot undo a known service scope.
   return retained?reply({error:'Declared logical service is unavailable',retry_same_id:true},503):null;
  }
  if(resolved.status!==200)
   return reply({error:'Logical service authority is unavailable',retry_same_id:true},503);
  const route=await resolved.json();
  if(typeof route.service_id!=='string'||!route.service_id||!hash(route.authority)||
     kind==='authority'&&route.authority!==identity)
   throw fail('Invalid logical service route');
  if(retained&&(retained.service_id!==route.service_id||retained.authority!==route.authority))
   throw fail('Logical service scope changed; reconcile its existing authority',409);
  await env.DB.prepare('INSERT INTO logical_service_scopes(site,kind,identity,service_id,authority) VALUES(?,?,?,?,?) ON CONFLICT(site,kind,identity) DO NOTHING').bind(config.site,kind,identity,route.service_id,route.authority).run();
  const current=await env.DB.prepare('SELECT service_id,authority FROM logical_service_scopes WHERE site=? AND kind=? AND identity=?').bind(config.site,kind,identity).first();
  if(!current||current.service_id!==route.service_id||current.authority!==route.authority)
   throw fail('Logical service scope changed concurrently',409);
  if(envelope.controller!==undefined&&envelope.controller!==route.authority)
   return reply({error:'Signed envelope addresses another authority'},409);
  const response=await fetch(base+'/api/service-authorities/call',{
   method:'POST',headers:{'Content-Type':'application/json'},body:raw,
   redirect:'error',signal:AbortSignal.timeout(30000)
  });
  if(![200,400,401,403,404,409,413,429,503].includes(response.status))
   throw fail('Logical authority reply is unavailable');
  // Preserve signed numbers on ingress and the authority response on egress.
  return new Response(await response.text(),{status:response.status,headers:{
   'Content-Type':'application/json','Cache-Control':'no-store',
   'X-Content-Type-Options':'nosniff'
  }});
 }catch(error){
  return reply({error:error?.status===409?error.message:'Logical service transport unavailable',
                retry_same_id:true},error?.status===409?409:503);
 }
}
