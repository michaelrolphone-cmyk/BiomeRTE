// Private devices receive signed MCP webhooks through this outbound HTTPS mailbox.
import {bearerPrincipal,digest} from './oauth.mjs';
import {eventGrant} from './events.mjs';
import {reply,limitedJSON,resolveControllerOwner} from './bridge.mjs';
const agentEncoder=new TextEncoder();
async function inboxKey(env,box){
 const root=await crypto.subtle.importKey('raw',Uint8Array.from(atob(env.BIOME_RECEIVER_WRAP_KEY),c=>c.charCodeAt(0)),{name:'HMAC',hash:'SHA-256'},false,['sign']);
 return new Uint8Array(await crypto.subtle.sign('HMAC',root,agentEncoder.encode('biome-mcp-event-inbox-v1/'+box)));
}
async function saveCallback(req,env,box){
 const id=req.headers.get('webhook-id')||'', ts=req.headers.get('webhook-timestamp')||'';
 if(!/^[a-zA-Z0-9_-]{1,180}$/.test(id)||!/^\d{10,12}$/.test(ts)||Math.abs(Date.now()/1000-Number(ts))>300)return reply({error:'Invalid delivery'},401);
 const reader=req.body?.getReader(); if(!reader)return reply({},400);
 let size=0,parts=[];
 for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>16384){await reader.cancel();return reply({},413)}parts.push(value)}
 const raw=new Uint8Array(size);let offset=0;for(const p of parts){raw.set(p,offset);offset+=p.length}
 const prefix=agentEncoder.encode(id+'.'+ts+'.'), bytes=new Uint8Array(prefix.length+size); bytes.set(prefix);bytes.set(raw,prefix.length);
 const key=await crypto.subtle.importKey('raw',await inboxKey(env,box),{name:'HMAC',hash:'SHA-256'},false,['verify']);
 let valid=false;
 for(const sig of (req.headers.get('webhook-signature')||'').split(' ')){
  if(/^v1,[A-Za-z0-9+/]{43}=$/.test(sig)&&await crypto.subtle.verify('HMAC',key,Uint8Array.from(atob(sig.slice(3)),c=>c.charCodeAt(0)),bytes)){valid=true;break}
 }
 if(!valid)return reply({error:'Invalid signature'},401);
 const body=new TextDecoder().decode(raw);let ev;try{ev=JSON.parse(body)}catch{return reply({},400)}
 if(ev.type==='verification'&&typeof ev.challenge==='string')return reply({challenge:ev.challenge});
 if(ev.eventId!==id||ev.name!=='workflow.delegation.requested'||!ev.data?.delegation_id)return reply({},400);
 // A mailbox secret and its signed event both belong to the same selected
 // producer. Do not infer routing authority from event payload contents.
 const routed=await env.DB.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_event_owners'").first();
 const selected=routed?await env.DB.prepare('SELECT controller FROM agent_event_owners WHERE box=?').bind(box).first():null;
 if(selected&&ev.controller!==selected.controller)return reply({error:'Event controller does not match this inbox'},409);
 await env.DB.prepare('INSERT OR IGNORE INTO agent_event_inbox (box,event_id,body) VALUES (?,?,?)').bind(box,id,body).run();
 return reply({accepted:true});
}
export async function agentInbox(req,env,path){
 if(req.method!=='POST')return reply({},405);
 const dest=/^\/agent-events\/callback\/([a-zA-Z0-9_-]{43})$/.exec(path);
 if(dest)return saveCallback(req,env,dest[1]);
 const match=/^\/agent-events\/([a-zA-Z0-9_.-]{1,80})\/(connect|next|ack)$/.exec(path);
 if(!match)return reply({},404);
 const owner=await bearerPrincipal(req,env);if(!owner)return reply({},401);
 let params={};
 if(req.body){try{params=await limitedJSON(req,2048)}catch{return reply({error:'Invalid inbox request'},400)}}
 if(!params||typeof params!=='object'||Array.isArray(params)||Object.keys(params).some(key=>key!=='controller'))return reply({error:'Invalid inbox request'},400);
 if(params.controller!==undefined){try{await resolveControllerOwner(env,params.controller)}catch(error){return reply({error:error.message},error.status??503)}}
 const grant=await eventGrant(req,env,true),identity=[owner,grant,match[1]];
 if(params.controller!==undefined)identity.push(params.controller);
 const box=await digest(JSON.stringify(identity));
 if(match[2]==='connect'){
  await env.DB.prepare('CREATE TABLE IF NOT EXISTS agent_event_inbox (box TEXT NOT NULL,event_id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(box,event_id))').run();
  if(params.controller!==undefined){
   await env.DB.prepare('CREATE TABLE IF NOT EXISTS agent_event_owners (box TEXT PRIMARY KEY,controller TEXT NOT NULL)').run();
   await env.DB.prepare('INSERT OR IGNORE INTO agent_event_owners(box,controller) VALUES(?,?)').bind(box,params.controller).run();
  }
  return reply({url:env.BIOME_OAUTH_ORIGIN+'/agent-events/callback/'+box,secret:'whsec_'+btoa(String.fromCharCode(...await inboxKey(env,box)))});
 }
 if(match[2]==='ack'){
  const id=req.headers.get('x-event-id');if(!id||id.length>180)return reply({},400);
  await env.DB.prepare('DELETE FROM agent_event_inbox WHERE box=? AND event_id=?').bind(box,id).run();
  return reply({accepted:true});
 }
 const rows=await env.DB.prepare('SELECT body FROM agent_event_inbox WHERE box=? ORDER BY rowid LIMIT 16').bind(box).all();
 return reply({events:rows.results.map(row=>JSON.parse(row.body))});
}
