// Grok routines use a bearer key, while the separate Grok Automations product
// uses Standard Webhooks. The key is independent of the caller's OAuth grant.
const grokFields = {
 receiver:{type:'string',description:'Stable receiver name chosen by this Grok routine.'},
 realm:{type:'string',enum:['site','local'],description:'Defaults to site.'},
 request_id:{type:'string',description:'Unique mutation ID. Reuse exactly on retry.'}
};
const grokSchema=(properties,required)=>({type:'object',properties,required,additionalProperties:false});
export const grokTools=[
 {name:'biome_register_grok_webhook',description:'Attach or update a Grok webhook for one controller topic. Use the routine-generated POST URL and raw key. Starts with new events; registration sends no POST.',
  inputSchema:grokSchema({...grokFields,url:{type:'string',format:'uri'},key:{type:'string',description:'Private Grok routine webhook key or whsec_ signing secret.'},
   auth_mode:{type:'string',enum:['bearer','standard_webhooks'],description:'Default bearer for Grok Bot routines.'},
   topic:{type:'string',description:'Exact controller event topic. Defaults to workflow.delegation.requested, routed only for Grok.'},
   reply_to:{type:'string',description:'Optional exact inbox address for lease.available.'},
   workflow:{type:'string',description:'Optional exact workflow filter for delegation events.'},
   from_cursor:{type:'integer',minimum:0,description:'Optional explicit replay from this sequence, excluding the supplied cursor.'}},['receiver','url','key','request_id'])},
 {name:'biome_list_grok_webhooks',description:'List your Grok receivers, filters, delivery cursors and last accepted time. Secrets and callback paths are redacted.',
  inputSchema:grokSchema({},[]),annotations:{readOnlyHint:true}},
 {name:'biome_unregister_grok_webhook',description:'Detach a Grok receiver in its realm.',inputSchema:grokSchema(grokFields,['receiver','request_id'])}
];
const grokActions = new Map(grokTools.map((tool,i)=>[tool.name,['register','list','unregister'][i]]));
export function grokAction(name){return grokActions.get(name);}
const grokOperations=new Map([['grok.receiver.register','register'],['grok.receiver.list','list'],['grok.receiver.unregister','unregister']]);
export function grokCall(params){
 const direct=grokAction(params.name);
 if(direct)return {action:direct,args:params.arguments??{}};
 if(params.name!=='biome_invoke')return null;
 const envelope=params.arguments??{};
 const action=grokOperations.get(envelope.operation);
 if(!action)return null;
 const supplied=envelope.arguments;
 if(!supplied||typeof supplied!=='object'||Array.isArray(supplied))return {action,args:null};
 if(action==='list')return {action,args:supplied};
 return {action,args:{...supplied,realm:envelope.realm??'site',request_id:envelope.request_id}};
}
const toB64=bytes=>btoa(Array.from(bytes,byte=>String.fromCharCode(byte)).join(''));
const fromB64=str=>Uint8Array.from(atob(str),c=>c.charCodeAt(0));
async function wrappingKey(env){
 return crypto.subtle.importKey('raw',fromB64(env.BIOME_RECEIVER_WRAP_KEY),{name:'AES-GCM'},false,['encrypt','decrypt']);
}
export async function sealGrokJob(env,id,body){
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const key=await wrappingKey(env);
 const params={name:'AES-GCM',iv,additionalData:new TextEncoder().encode(id)};
 const ciphertext=await crypto.subtle.encrypt(params,key,new TextEncoder().encode(JSON.stringify(body)));
 return {iv:toB64(iv),ciphertext:toB64(new Uint8Array(ciphertext))};
}
export async function openGrokJob(env,id,sealed){
 const params={name:'AES-GCM',iv:fromB64(sealed.iv),additionalData:new TextEncoder().encode(id)};
 const body=await crypto.subtle.decrypt(params,await wrappingKey(env),fromB64(sealed.ciphertext));
 return JSON.parse(new TextDecoder().decode(body));
}
