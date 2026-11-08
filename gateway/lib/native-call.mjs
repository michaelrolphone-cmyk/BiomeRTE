import {enqueue,reply} from './bridge.mjs';

// Forward the existing native protocol, not an operator/MCP invocation. Only
// the controller authenticates the signature, current enrollment and scopes.
export async function nativeCall(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 if(req.headers.get('Content-Type')?.split(';')[0].trim()!=='application/json')return reply({error:'Expected application/json'},415);
 let raw,e;
 try{
  const reader=req.body?.getReader();if(!reader)throw Error();
  const chunks=[];let size=0;
  for(;;){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>125000){await reader.cancel();throw Object.assign(Error(),{status:413})}chunks.push(value)}
  const bytes=new Uint8Array(size);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.length}
  raw=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);e=JSON.parse(raw);
  const object=v=>v&&typeof v==='object'&&!Array.isArray(v);
  if(!object(e)||Object.keys(e).sort().join(',')!=='arguments,auth,id,operation,realm,site'||
   e.realm!=='site'||typeof e.site!=='string'||!e.site||e.site.length>128||
   typeof e.id!=='string'||!e.id.trim()||e.id.length>128||
   typeof e.operation!=='string'||!e.operation||e.operation.length>128||!object(e.arguments)||
   !object(e.auth)||Object.keys(e.auth).sort().join(',')!=='actor,signature'||
   !/^[a-f0-9]{64}$/.test(e.auth.actor)||typeof e.auth.signature!=='string'||
   !/^[A-Za-z0-9+/]+={0,2}$/.test(e.auth.signature)||e.auth.signature.length>1400)throw Error();
 }catch(error){return reply({error:error.status===413?'Request exceeds gateway transport limit':'Invalid native request'},error.status??400)}
 const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
 if(!config)return reply({error:'Controller unavailable',retryable:true},503);
 if(config.site!==e.site)return reply({error:'Wrong controller site'},403);
 const args={realm:'site',site:e.site,body:raw};
 if(JSON.stringify({name:'biome_native_call',arguments:args}).length>96000)return reply({error:'Native request too large'},413);
 // Never reuse a bridge result. Even the exact same signed native request must
 // pass current controller authentication and revocation checks on every HTTP
 // attempt. Native actor+ID mutation receipts deduplicate any uncertain effect.
 const result=await enqueue(env,'biome_native_call',args,crypto.randomUUID());
 if(result.isError)return reply({error:'Controller response uncertain; retry identical native request',retryable:true},503);
 let value;try{value=result.structuredContent??JSON.parse(result.content[0].text)}catch{}
 if(!value||!Number.isInteger(value.status)||value.status<200||value.status>=500||
   value.status>=300&&value.status<400||!value.body||typeof value.body!=='object'||Array.isArray(value.body))
  return reply({error:'Controller response uncertain; retry identical native request',retryable:true},503);
 return reply(value.body,value.status);
}
