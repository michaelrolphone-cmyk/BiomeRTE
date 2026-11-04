import {limitedJSON,reply,runTool} from './bridge.mjs';

// Phone keys are enrolled and verified by Biome. This ingress cannot select a
// controller operation, realm, or privileged identity from caller input.
export async function mobileDecision(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 let e;try{e=await limitedJSON(req,96000)}catch{return reply({error:'Invalid decision envelope'},400)}
 if(!e||Array.isArray(e)||!['enroll','inbox','respond'].includes(e.command))return reply({error:'Unsupported decision command'},400);
 if(e.command!=='enroll'&&(!/^[0-9a-f]{64}$/.test(e.device||'')||typeof e.signature!=='string'||typeof e.payload!=='string'||typeof e.id!=='string'))return reply({error:'Signed phone identity required'},401);
 // Do not key bridge result caching by a caller's unverified ID. The controller
 // verifies signatures first, then applies its authenticated response receipt.
 const result=await runTool(env,'biome_invoke',{operation:e.command==='inbox'?'decision.mobile.inbox':'decision.mobile',arguments:{envelope:e},realm:'site',request_id:crypto.randomUUID()});
 if(result.isError){
  const detail=(result.content??[]).filter(c=>c.type==='text').map(c=>c.text).join(' ');
  const stale=/Question changed|already settled|Obsolete decision/.test(detail);
  return reply({error:stale?'Question changed or was withdrawn. Your original reply is preserved.':'Decision service unavailable or request rejected',code:stale?'decision_changed':'retry',retryable:!stale},409);
 }
 const data=result.structuredContent??JSON.parse(result.content[0].text);
 return reply(data);
}
