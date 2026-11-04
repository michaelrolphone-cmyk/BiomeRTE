import {controllerDevice,claimControllerJob,controllerStatus} from './ha.mjs';
import {openGrokJob} from './grok.mjs';
import {canDeliver} from './events.mjs';
import {sameSecret} from './access.mjs';
export const reply = (data, status=200) => Response.json(data, {status, headers:{'Cache-Control':'no-store'}});
const wait = ms => new Promise(r => setTimeout(r, ms));
const epoch = () => Math.floor(Date.now()/1000);
export const hexHash = async text => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))), b => b.toString(16).padStart(2,'0')).join('');

export async function limitedJSON(req, max) {
 const reader = req.body?.getReader();
 if (!reader || Number(req.headers.get('content-length') || 0) > max) throw Error('Invalid request size');
 const chunks = []; let size = 0;
 for (;;) {
  const {value, done} = await reader.read(); if (done) break;
  size += value.length;
  if (size > max) { await reader.cancel(); throw Error('Request too large'); }
  chunks.push(value);
 }
 const data = new Uint8Array(size); let offset = 0;
 for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length; }
 return JSON.parse(new TextDecoder().decode(data));
}

export async function device(req, env, path) {
 const token = /^Bearer ([0-9a-f]{64})$/.exec(req.headers.get('Authorization')||'')?.[1];
 const legacy=!!token&&!!env.BIOME_BRIDGE_PIN&&sameSecret(await hexHash(await hexHash(token)+'\n'),env.BIOME_BRIDGE_PIN);
 const node=token?await env.DB.prepare('SELECT * FROM controller_nodes WHERE token_hash=? AND enabled=1').bind(await hexHash(token)).first():null;
 if(!legacy&&!node)return reply({error:'Invalid device credential'},401);
 if(path.startsWith('/bridge/ha/'))return controllerDevice(req,env,path,node,legacy);
 if (req.method !== 'POST') return reply({error:'Use POST'},405);
 if (path === '/bridge/event-authorize') {
  const {owner,grant}=await limitedJSON(req,2048);
  return reply({allowed:await canDeliver(env.DB,owner,grant)});
 }
 if (path === '/bridge/check') {
  // A device can verify its own round trip. This credential cannot choose an
  // operation or use the client MCP endpoint.
  return reply(await runTool(env, 'biome_status'));
 }
 const db = env.DB, time = epoch();
 if (path === '/bridge/next') {
  const selected=await claimControllerJob(db,node);
  const lease = selected.lease||crypto.randomUUID();
  const job = selected.legacy? await db.prepare("UPDATE bridge_jobs SET state='claimed',lease=?,lease_until=?,updated=? WHERE id=(SELECT id FROM bridge_jobs WHERE deadline>=? AND (state='queued' OR (state='claimed' AND lease_until<?)) ORDER BY updated,id LIMIT 1) RETURNING id,request")
   .bind(lease,time+35,time,time,time).first():selected;
  if (!job||job.idle) return reply({idle:true,primary:job?.primary});
  let request=JSON.parse(job.request);
  if (request.name==='sealed_grok_receiver')
   request={name:'grok_receiver_change',arguments:await openGrokJob(env,job.id,request.arguments)};
  return reply({id:job.id,lease,request});
 }
 if (path === '/bridge/complete') {
  const d = await limitedJSON(req,500000);
  if (typeof d.id !== 'string' || typeof d.lease !== 'string' || !Array.isArray(d.result?.content))
   return reply({error:'Invalid controller result'},400);
  const row = await db.prepare("UPDATE bridge_jobs SET state='done',request=CASE WHEN id LIKE 'event-%' THEN '{}' ELSE request END,result=?,updated=? WHERE id=? AND lease=? AND state='claimed' AND (controller IS NULL OR controller=?) RETURNING id")
   .bind(JSON.stringify(d.result),time,d.id,d.lease,node?.id??'').first();
  return reply({accepted:!!row},row?200:409);
 }
 return reply({error:'Unknown bridge operation'},404);
}

function canonical(value) {
 if (Array.isArray(value)) return value.map(canonical);
 if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])]));
 return value;
}
export const toolError = message => ({isError:true,content:[{type:'text',text:message}]});
export const toolsList = [
 {name:'biome_status',description:'Verify connectivity to the running physical controller and read its health.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true}},
 {name:'biome_discover',description:'Discover controller operations, contracts, offers and caller guidance. Pass section to filter or cursor to continue. OAuth discovery is paged by default. Cached clients can call discovery.page via biome_invoke.',inputSchema:{type:'object',properties:{section:{type:'string',enum:['operations','contracts','offers','guide']},cursor:{type:'string'}},additionalProperties:false},annotations:{readOnlyHint:true}},
 {name:'biome_work_list',description:'List work stored on the running controller.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true}},
 {name:'biome_invoke',description:'Acquire a lease, invoke with a unique command ID, then read cap.status using that ID. Follow next_call for current state. provider.poll is the worker inbox. Reuse a mutation request_id only for an identical retry.',inputSchema:{type:'object',properties:{operation:{type:'string'},arguments:{type:'object',additionalProperties:true},request_id:{type:'string',minLength:1,maxLength:128},realm:{type:'string',enum:['site','local']}},required:['operation','arguments','request_id'],additionalProperties:false}}
];
export async function runTool(env, name, args={}) {
 if (!toolsList.some(t=>t.name===name)) return toolError('Unknown tool');
 if (!args || typeof args!=='object' || Array.isArray(args)) return toolError('Expected an arguments object');
 if (name==='biome_invoke') {
  if (typeof args.operation!=='string' || !args.arguments || typeof args.arguments!=='object' || Array.isArray(args.arguments) ||
      typeof args.request_id!=='string' || !args.request_id.trim() || args.request_id.length>128 ||
      Object.keys(args).some(k=>!['operation','arguments','request_id','realm'].includes(k)) ||
      !['site','local'].includes(args.realm??'site')) return toolError('Expected operation, arguments, request_id and optional realm');
 } else if (Object.keys(args).length) return toolError('This tool takes no arguments');
 // A status read must see later transitions even when a client repeats the
 // exact next_call returned in a queued receipt. Mutation retries stay durable.
 const liveRead=name==='biome_invoke' && ['cap.status','cap.lease','cap.leases','cap.discover','bus.inbox','provider.poll'].includes(args.operation);
 const key = name==='biome_invoke' && !liveRead ? 'invoke-'+await hexHash((args.realm||'site')+'\n'+args.request_id) : crypto.randomUUID();
 return enqueue(env,name,args,key);
}
export async function enqueue(env,name,args,key){
 const payload=JSON.stringify(canonical({name,arguments:args})), time=epoch(), db=env.DB;
 if(payload.length>96000) return toolError('Request too large');
 await db.prepare('DELETE FROM bridge_jobs WHERE updated<?').bind(time-3600).run();
 await db.prepare("INSERT OR IGNORE INTO bridge_jobs(id,request,state,deadline,updated) SELECT ?,?,'queued',?,? WHERE (SELECT count(*) FROM bridge_jobs WHERE state!='done' AND deadline>=?)<40")
  .bind(key,payload,time+22,time,time).run();
 let row=await db.prepare('SELECT request,state,result,deadline FROM bridge_jobs WHERE id=?').bind(key).first();
 if(!row) return toolError('Controller queue is busy. Retry with the same request_id.');
 if(key.startsWith('invoke-') && row.request!==payload) return toolError('request_id already belongs to a different request.');
 if(row.state==='done') return JSON.parse(row.result);
 if(row.deadline<time) await db.prepare("UPDATE bridge_jobs SET deadline=?,updated=? WHERE id=? AND state!='done'").bind(time+22,time,key).run();
 for(let turn=0;turn<20;turn++){
  await wait(turn===0?250:1000);
  row=await db.prepare('SELECT result FROM bridge_jobs WHERE id=?').bind(key).first();
  if(row?.result) return JSON.parse(row.result);
 }
 return toolError('The controller has not replied yet. Retry with the same request_id to recover the result.');
}
