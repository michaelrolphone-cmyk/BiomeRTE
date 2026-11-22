import {controllerRuntimeNative,controllerRuntimePair} from './controller-runtime.mjs';
import {logicalServiceCall} from './service-authorities.mjs';
import {logProviderFailure} from './diagnostics.mjs';
import {enqueue,reply,resolveControllerOwner} from './bridge.mjs';
import {resolveOwnershipRoute} from './ownership-transfer.mjs';
import {dispatchController,rememberProviderInvocation} from './provider-topology.mjs';

// This endpoint transports a native scoped-provider envelope. It never converts
// a caller into the owner. Only the controller verifies signatures and grants.
export const nativeProviderOperations=new Set([
 'cap.offer','cap.withdraw','cap.registrations','cap.publish','provider.poll','cap.fenced','provider.claim','provider.result',
 'cap.status','cap.configured','agent.progress','cap.heartbeat','cap.command.state','stream.attach',
 'provider.custody','provider.reconcile'
]);
const providerIdent=value=>typeof value==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const providerObject=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const controllerIdent=value=>typeof value==='string'&&/^[0-9a-f]{64}$/.test(value);
function providerControllers(env){
 if(env.PROVIDER_CONTROLLER_MAP===undefined)return {};
 let mapping;
 try{mapping=JSON.parse(env.PROVIDER_CONTROLLER_MAP)}catch{throw Object.assign(Error('Invalid provider ownership configuration'),{status:503})}
 if(!providerObject(mapping)||Object.entries(mapping).some(([actor,controller])=>!controllerIdent(actor)||!controllerIdent(controller)))
  throw Object.assign(Error('Invalid provider ownership configuration'),{status:503});
 return mapping;
}
export async function providerController(env,actor,selected){
 const mapping=providerControllers(env);let assigned=Object.hasOwn(mapping,actor)?mapping[actor]:undefined;
 if(env.CONTROLLER_COORDINATION_MODE==='local'){
  const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
  if(!config)throw Object.assign(Error('Controller site is not configured'),{status:409});
  const current=await resolveOwnershipRoute(env.DB,config.site,actor);
  if(current){
   if(assigned!==undefined&&assigned!==current.owner)throw Object.assign(Error('Legacy provider mapping conflicts with current ownership'),{status:409});
   if(current.frozen)throw Object.assign(Error('Provider ownership handoff is fenced pending reconciliation'),{status:409});
   assigned=current.owner;
  }else if(await env.DB.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='provider_placements'").first()){
   // An authenticated registration's current gateway placement supersedes the
   // old deployment-wide affinity map. Native grants/signatures stay checked.
   const placement=await env.DB.prepare('SELECT controller FROM provider_placements WHERE site=? AND provider=?').bind(config.site,actor).first();
   if(placement)assigned=placement.controller;
  }
 }
 if(selected!==undefined&&!controllerIdent(selected))throw Object.assign(Error('Invalid controller identity'),{status:400});
 if(assigned&&selected!==undefined&&selected!==assigned)throw Object.assign(Error('Provider belongs to a different controller'),{status:409});
 const controller=assigned??selected;
 if(controller===undefined&&env.CONTROLLER_COORDINATION_MODE==='local')
  throw Object.assign(Error('Provider controller ownership is not configured'),{status:409});
 if(controller!==undefined)await resolveControllerOwner(env,controller);
 return controller;
}
export async function providerFingerprint(pem){
 const match=/^\s*-----BEGIN PUBLIC KEY-----([\s\S]+?)-----END PUBLIC KEY-----\s*$/.exec(pem);
 if(!match)throw Error('Invalid provider public key');
 const encoded=match[1].replace(/\s/g,'');
 if(!encoded.length||encoded.length%4||!/^[A-Za-z0-9+/]+={0,2}$/.test(encoded))throw Error('Invalid provider public key');
 const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
 const key=await crypto.subtle.importKey('spki',bytes,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},true,['verify']);
 const der=await crypto.subtle.exportKey('spki',key);
 return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',der)),b=>b.toString(16).padStart(2,'0')).join('');
}
function pairingController(req){
 const values=new URL(req.url).searchParams.getAll('controller');
 if(values.length>1||values.length===1&&!controllerIdent(values[0]))throw Object.assign(Error('Invalid pairing controller'),{status:400});
 return values[0];
}
export function validProviderEnvelope(d){
 if(!providerObject(d)||!['arguments,auth,id,operation,realm,site','arguments,auth,controller,id,operation,realm,site'].includes(Object.keys(d).sort().join(',')))return false;
 if('controller' in d&&!controllerIdent(d.controller))return false;
 if(!providerIdent(d.site)||!providerIdent(d.id)||d.realm!=='site'||!nativeProviderOperations.has(d.operation)||!providerObject(d.arguments))return false;
 const auth=d.auth;
 if(!providerObject(auth)||Object.keys(auth).sort().join(',')!=='actor,signature'||
    typeof auth.actor!=='string'||!/^[0-9a-f]{64}$/.test(auth.actor)||
    typeof auth.signature!=='string'||!auth.signature.length||auth.signature.length%4||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(auth.signature))return false;
 return d.operation==='cap.status'||(d.operation==='stream.attach'?d.arguments.role==='provider'&&providerIdent(d.arguments.session):d.arguments.provider===auth.actor);
}
export async function nativeProviderCall(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 if((req.headers.get('Content-Type')||'').split(';')[0].trim()!=='application/json')
  return reply({error:'Expected application/json'},415);
 let raw,d;
 try{raw=await req.text();d=JSON.parse(raw)}catch{return reply({error:'Invalid provider JSON'},400)}
 if(!d?.auth)return reply({error:'Signed provider credentials required'},401);
 if(!validProviderEnvelope(d))return reply({error:'Invalid scoped-provider envelope'},400);
 if(new URL(req.url).searchParams.has('controller'))return reply({error:'Call controller must be inside the signed envelope'},400);
 let controller;
 try{
  const original=await dispatchController(env,d.operation,d.arguments);
  if(original){
   const runtime=await controllerRuntimeNative(env,raw,original,req);
   if(runtime)return runtime;
   if(d.controller!==undefined&&d.controller!==original)throw Object.assign(Error('Command belongs to a different dispatch controller'),{status:409});
   await resolveControllerOwner(env,original);controller=original;
  }else{
   const logical=await logicalServiceCall(env,{provider:d.auth.actor},raw);
   if(logical)return logical;
   const runtime=await controllerRuntimeNative(env,raw,undefined,req);
   if(runtime)return runtime;
   controller=await providerController(env,d.auth.actor,d.controller);
  }
 }catch(error){return reply({error:error.message},error.status??400)}
 // Preserve the original JSON text so large integers and 1.0 are not rewritten
 // by JavaScript before Python reconstructs the signed canonical object.
 // A fresh bridge job is mandatory even for identical request IDs: a cached
 // result must never bypass current signature/revocation checks. Native
 // controller receipts still deduplicate the original mutation ID.
 const arguments_={envelope:raw};if(controller!==undefined)arguments_.controller=controller;
 return forwardProvider(req,env,'signed_provider_call',arguments_,d.operation,'provider-',async answer=>{
  if(env.CONTROLLER_COORDINATION_MODE!=='local'||answer.status<200||answer.status>=300)return;
  const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
  if(!config)return;
  await rememberProviderInvocation(env,d,{args:{controller},site:config.site},{structuredContent:answer.body.result});
 });
}

export function validProviderPair(phase,d){
 if(!providerObject(d))return false;
 if(phase==='begin')return Object.keys(d).sort().join(',')==='label,public_key'&&
  typeof d.public_key==='string'&&d.public_key.length<=8192&&d.public_key.includes('BEGIN PUBLIC KEY')&&
  typeof d.label==='string'&&d.label.length>0;
 return phase==='finish'&&Object.keys(d).sort().join(',')==='challenge,key,signature,site'&&
  typeof d.challenge==='string'&&/^[0-9a-f]{64}$/.test(d.challenge)&&
  typeof d.key==='string'&&/^[0-9a-f]{64}$/.test(d.key)&&providerIdent(d.site)&&
  typeof d.signature==='string'&&d.signature.length>0&&d.signature.length%4===0&&
  /^[A-Za-z0-9+/]+={0,2}$/.test(d.signature);
}
export async function nativeProviderPair(req,env,phase){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 if((req.headers.get('Content-Type')||'').split(';')[0].trim()!=='application/json')
  return reply({error:'Expected application/json'},415);
 let raw,d;
 try{raw=await req.text();d=JSON.parse(raw)}catch{return reply({error:'Invalid pairing JSON'},400)}
 if(!validProviderPair(phase,d))return reply({error:'Invalid pairing envelope'},400);
 let controller;
 try{
  const selected=pairingController(req);
  const runtime=await controllerRuntimePair(env,phase,raw,selected,req);
  if(runtime)return runtime;
  const mapping=providerControllers(env);
  const actor=phase==='finish'?d.key:(env.CONTROLLER_COORDINATION_MODE==='local'||Object.keys(mapping).length?await providerFingerprint(d.public_key):'');
  controller=await providerController(env,actor,selected);
 }catch(error){return reply({error:error.message},error.status??400)}
 // Pairing only creates an untrusted candidate or proves possession AFTER an
 // operator has approved that exact fingerprint. Approval is never public.
 const arguments_={phase,envelope:raw};if(controller!==undefined)arguments_.controller=controller;
 return forwardProvider(req,env,'signed_provider_pair',arguments_,'pair.'+phase,'provider-pair-');
}

async function forwardProvider(req,env,name,args,operation,prefix,onResponse){
 const diagnostic={started:performance.now(),operation,bridge_job:prefix+crypto.randomUUID(),
  stage:'queue_admission',reason:'enqueue_failed'};
 try {
  const value=await enqueue(env,name,args,diagnostic.bridge_job,diagnostic);
  if(value.isError){
   logProviderFailure(req,diagnostic,503,value.content?.length===1?value.content[0]?.text:undefined);
   return reply({error:'Controller reply unavailable',retry_same_id:true},503);
  }
  diagnostic.stage='response';diagnostic.reason='controller_response_json_invalid';
  let answer;
  try{answer=JSON.parse(value.content.find(x=>x.type==='text').text)}catch(error){
   logProviderFailure(req,diagnostic,502,error);
   return reply({error:'Invalid controller response',retry_same_id:true},502);
  }
  diagnostic.reason='controller_response_shape_invalid';
  if(!providerObject(answer)||!Number.isInteger(answer.status)||answer.status<200||answer.status>599||
     !providerObject(answer.body)){
   logProviderFailure(req,diagnostic,502);
   return reply({error:'Invalid controller response',retry_same_id:true},502);
  }
  diagnostic.controller_status=answer.status;diagnostic.reason='controller_response_serialization_failed';
  if(onResponse)await onResponse(answer);
  const response=reply(answer.body,answer.status);
  if(answer.status>=500){
   diagnostic.stage='controller';diagnostic.reason='controller_http_error';
   logProviderFailure(req,diagnostic,answer.status,answer.body.error);
  }
  return response;
 }catch(error){
  logProviderFailure(req,diagnostic,503,error);
  return reply({error:'Controller gateway unavailable'},503);
 }
}

