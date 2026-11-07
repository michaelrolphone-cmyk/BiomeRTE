import {limitedJSON,reply,hexHash,readBridgeResult} from './bridge.mjs';

const nativeProviderName='biome_native_provider';
const nativeObject=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
function nativeCanonical(v){
 if(Array.isArray(v))return '['+v.map(nativeCanonical).join(',')+']';
 if(nativeObject(v))return '{'+Object.keys(v).sort().map(k=>nativeCanonical(k)+':'+nativeCanonical(v[k])).join(',')+'}';
 return JSON.stringify(v).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
}
function nativeIntegers(v){
 if(typeof v==='number')return Number.isSafeInteger(v);
 if(v&&typeof v==='object')return Object.values(v).every(nativeIntegers);
 return true;
}
function nativeExactKeys(v,keys){return nativeObject(v)&&Object.keys(v).sort().join(',')===[...keys].sort().join(',')}
function nativeBytes(s){return Uint8Array.from(atob(s),c=>c.charCodeAt(0))}

async function nativeProof(env,d,actor,signature,signed){
 const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
 if(!config||config.site!==d.site)throw Error('Wrong controller site');
 const allowed=String(env.BIOME_PROVIDER_ACTORS||'').split(',').map(x=>x.trim());
 if(!allowed.includes(actor))throw Error('Provider not admitted');
 try{
  const m=/^\s*-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\s]+)\s+-----END PUBLIC KEY-----\s*$/.exec(d.public_key);
  if(!m)throw Error();
  const der=nativeBytes(m[1].replace(/\s/g,''));
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',der)),b=>b.toString(16).padStart(2,'0')).join('');
  if(digest!==actor)throw Error();
  const key=await crypto.subtle.importKey('spki',der,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
  if(key.algorithm.modulusLength<2048||key.algorithm.modulusLength>8192||!await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,nativeBytes(signature),new TextEncoder().encode(signed)))throw Error();
 }catch{throw Error('Invalid provider proof')}
}

// This is not an operator/MCP ingress. Native identity and scope checks still
// happen at /v1/call with NO operator token. The allowlist limits queue admission
// only; it cannot enroll a key or grant any controller permission.
export async function nativeProvider(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 if(req.headers.get('Origin')&&req.headers.get('Origin')!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 let d,e,wire,signed;
 try{
  d=await limitedJSON(req,96000);
  if(!nativeObject(d)||Object.keys(d).some(k=>!['site','public_key','envelope','signed_payload'].includes(k)))throw Error();
  wire=typeof d.envelope==='string'?d.envelope:JSON.stringify(d.envelope);
  e=JSON.parse(wire);
  if(!nativeExactKeys(e,['id','realm','operation','arguments','auth'])||e.realm!=='site'||
    typeof e.id!=='string'||!e.id.trim()||e.id.length>128||
    typeof e.operation!=='string'||!e.operation.length||e.operation.length>128||!nativeObject(e.arguments)||
    !nativeExactKeys(e.auth,['actor','signature'])||!/^[a-f0-9]{64}$/.test(e.auth.actor)||
    typeof e.auth.signature!=='string'||e.auth.signature.length>1400||typeof d.site!=='string'||
    typeof d.public_key!=='string'||d.public_key.length>8192)throw Error();
  const payload={site:d.site,realm:e.realm,id:e.id,operation:e.operation,arguments:e.arguments};
  // A raw envelope + signed_payload preserves Python native number spelling.
  // Object form is deliberately integer-only rather than silently changing a
  // float (1.0 -> 1) under a signature during JSON parsing/serialization.
  if(typeof d.envelope!=='string'&&!nativeIntegers(e))throw Error();
  signed=d.signed_payload??nativeCanonical(payload);
  if(typeof signed!=='string'||nativeCanonical(JSON.parse(signed))!==nativeCanonical(payload))throw Error();
 }catch{return reply({error:'Invalid native envelope'},400)}
 try{await nativeProof(env,d,e.auth.actor,e.auth.signature,signed)}catch{return reply({error:'Provider proof or admission rejected'},403)}
 // Full proof and body are required on every poll. No result-by-ID API exists.
 const key='native-'+await hexHash(nativeCanonical({site:d.site,envelope:wire,signed_payload:signed}));
 const payload=JSON.stringify({name:nativeProviderName,arguments:{realm:'site',site:d.site,envelope:wire}}),now=Math.floor(Date.now()/1000);
 await env.DB.prepare("INSERT OR IGNORE INTO bridge_jobs(id,request,state,deadline,updated) SELECT ?,?,'queued',0,? WHERE (SELECT count(*) FROM bridge_jobs WHERE id LIKE 'native-%' AND state!='done')<40")
  .bind(key,payload,now).run();
 const row=await env.DB.prepare('SELECT state,result FROM bridge_jobs WHERE id=?').bind(key).first();
 if(!row)return reply({error:'Provider queue is busy',retryable:true},503);
 if(row.state!=='done')return reply({pending:true,retry_after_ms:1000},202);
 const result=await readBridgeResult(env.DB,key,row.result);
 // Transport faults are not operation results. Keep the same job eligible for
 // retry; controller receipts deduplicate effects after ambiguous link failures.
 if(result.isError){
  await env.DB.prepare("UPDATE bridge_jobs SET state='queued',result=NULL,updated=? WHERE id=? AND state='done'").bind(now,key).run();
  return reply({pending:true,retry_after_ms:1000},202);
 }
 const value=result.structuredContent??JSON.parse(result.content[0].text);
 if(!Number.isInteger(value.status)||value.status<200||value.status>599||!nativeObject(value.body))return reply({error:'Invalid controller relay response'},502);
 return reply({...value.body,relay_receipt:key},value.status);
}

// Acknowledgement is a distinct signed operation, never an unsigned delete or
// a repurposed controller signature. No tombstone is needed: absent is success.
export async function nativeProviderAck(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 if(req.headers.get('Origin')&&req.headers.get('Origin')!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 let d;
 try{
  d=await limitedJSON(req,12000);
  if(!nativeExactKeys(d,['site','public_key','actor','receipt','signature'])||
   typeof d.site!=='string'||typeof d.public_key!=='string'||d.public_key.length>8192||
   !/^[a-f0-9]{64}$/.test(d.actor)||!/^native-[a-f0-9]{64}$/.test(d.receipt)||
   typeof d.signature!=='string'||d.signature.length>1400)throw Error();
 }catch{return reply({error:'Invalid acknowledgement'},400)}
 const signed=nativeCanonical({domain:'biomerte-provider-relay-ack-v1',operation:'acknowledge',site:d.site,actor:d.actor,receipt:d.receipt});
 try{await nativeProof(env,d,d.actor,d.signature,signed)}catch{return reply({error:'Provider proof or admission rejected'},403)}
 const row=await env.DB.prepare('SELECT request,state,result FROM bridge_jobs WHERE id=?').bind(d.receipt).first();
 const accepted=()=>reply({acknowledged:true,receipt:d.receipt});
 if(!row)return accepted();
 const request=JSON.parse(row.request);
 if(request.name!==nativeProviderName||request.arguments.site!==d.site||JSON.parse(request.arguments.envelope).auth.actor!==d.actor)
  return reply({error:'Receipt belongs to another provider'},403);
 if(row.state!=='done')return reply({error:'Result not complete'},409);
 const result=await readBridgeResult(env.DB,d.receipt,row.result);
 if(result.isError)return reply({error:'Result not complete'},409);
 const value=result.structuredContent??JSON.parse(result.content[0].text);
 if(!Number.isInteger(value.status)||value.status<200||value.status>=500||!nativeObject(value.body))return reply({error:'Result not complete'},409);
 // D1 foreign keys cascade chunk deletion. Compare the exact terminal row so
 // a concurrent reclaim/requeue cannot turn this into a pending-job deletion.
 const removed=await env.DB.prepare("DELETE FROM bridge_jobs WHERE id=? AND state='done' AND request=? AND result=? RETURNING id")
  .bind(d.receipt,row.request,row.result).first();
 if(removed)return accepted();
 const remaining=await env.DB.prepare('SELECT id FROM bridge_jobs WHERE id=?').bind(d.receipt).first();
 return remaining?reply({error:'Result changed; retry acknowledgement'},409):accepted();
}
