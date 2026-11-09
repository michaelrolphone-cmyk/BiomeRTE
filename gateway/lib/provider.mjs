import {enqueue,limitedText,reply} from './bridge.mjs';

// This endpoint transports a native scoped-provider envelope. It never converts
// a caller into the owner. Only the controller verifies signatures and grants.
export const nativeProviderOperations=new Set([
 'cap.offer','provider.poll','cap.fenced','provider.claim','provider.result',
 'cap.status','cap.configured','agent.progress'
]);
const providerIdent=value=>typeof value==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const providerObject=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
export function validProviderEnvelope(d){
 if(!providerObject(d)||Object.keys(d).sort().join(',')!=='arguments,auth,id,operation,realm,site')return false;
 if(!providerIdent(d.site)||!providerIdent(d.id)||d.realm!=='site'||!nativeProviderOperations.has(d.operation)||!providerObject(d.arguments))return false;
 const auth=d.auth;
 if(!providerObject(auth)||Object.keys(auth).sort().join(',')!=='actor,signature'||
    typeof auth.actor!=='string'||!/^[0-9a-f]{64}$/.test(auth.actor)||
    typeof auth.signature!=='string'||!auth.signature.length||auth.signature.length%4||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(auth.signature))return false;
 return d.operation==='cap.status'||d.arguments.provider===auth.actor;
}
export async function nativeProviderCall(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 if((req.headers.get('Content-Type')||'').split(';')[0].trim()!=='application/json')
  return reply({error:'Expected application/json'},415);
 let raw,d;
 try{raw=await limitedText(req,125000);d=JSON.parse(raw)}catch{return reply({error:'Invalid provider JSON'},400)}
 if(!d?.auth)return reply({error:'Signed provider credentials required'},401);
 if(!validProviderEnvelope(d))return reply({error:'Invalid scoped-provider envelope'},400);
 // Preserve the original JSON text so large integers and 1.0 are not rewritten
 // by JavaScript before Python reconstructs the signed canonical object.
 // A fresh bridge job is mandatory even for identical request IDs: a cached
 // result must never bypass current signature/revocation checks. Native
 // controller receipts still deduplicate the original mutation ID.
 const value=await enqueue(env,'signed_provider_call',{envelope:raw},'provider-'+crypto.randomUUID());
 if(value.isError)return reply({error:'Controller reply unavailable',retry_same_id:true},503);
 let answer;
 try{answer=JSON.parse(value.content.find(x=>x.type==='text').text)}catch{
  return reply({error:'Invalid controller response',retry_same_id:true},502);
 }
 if(!providerObject(answer)||!Number.isInteger(answer.status)||answer.status<200||answer.status>599||
    !providerObject(answer.body))return reply({error:'Invalid controller response',retry_same_id:true},502);
 return reply(answer.body,answer.status);
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
 try{raw=await limitedText(req,125000);d=JSON.parse(raw)}catch{return reply({error:'Invalid pairing JSON'},400)}
 if(!validProviderPair(phase,d))return reply({error:'Invalid pairing envelope'},400);
 // Pairing only creates an untrusted candidate or proves possession AFTER an
 // operator has approved that exact fingerprint. Approval is never public.
 const value=await enqueue(env,'signed_provider_pair',{phase,envelope:raw},'provider-pair-'+crypto.randomUUID());
 if(value.isError)return reply({error:'Controller reply unavailable',retry_same_id:true},503);
 let answer;
 try{answer=JSON.parse(value.content.find(x=>x.type==='text').text)}catch{
  return reply({error:'Invalid controller response',retry_same_id:true},502);
 }
 if(!providerObject(answer)||!Number.isInteger(answer.status)||answer.status<200||answer.status>599||
    !providerObject(answer.body))return reply({error:'Invalid controller response',retry_same_id:true},502);
 return reply(answer.body,answer.status);
}
