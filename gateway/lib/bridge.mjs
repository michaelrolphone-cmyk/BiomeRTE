import {controllerDevice,claimControllerJob,controllerStatus} from './ha.mjs';
import {openGrokJob} from './grok.mjs';
import {canDeliver} from './events.mjs';
import {sameSecret} from './access.mjs';
import {controllerSocket} from './transport.mjs';
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

// Each frame fits both the HTTP parser and D1 value limits. The result itself
// has no framing-size limit and is published only after all bytes are verified.
const resultChunkSize = 65536;
async function readResultChunks(db,id,lease,digest,parts,progress=async()=>{}) {
 const chunks=[];
 for(let start=0;start<parts;start+=16) {
  await progress();
  const rows=(await db.prepare('SELECT part,data FROM bridge_result_chunks WHERE job=? AND lease=? AND digest=? AND part>=? AND part<? ORDER BY part')
   .bind(id,lease,digest,start,Math.min(start+16,parts)).all()).results;
  for(const row of rows) {
   if(row.part!==chunks.length)throw Error('Incomplete controller result');
   chunks.push(row.data);
  }
 }
 if(chunks.length!==parts)throw Error('Incomplete controller result');
 const encoded=chunks.join('');
 if(await hexHash(encoded)!==digest)throw Error('Controller result digest mismatch');
 const value=JSON.parse(encoded);
 if(!Array.isArray(value?.content))throw Error('Invalid controller result');
 return value;
}
export async function readBridgeResult(db,id,encoded) {
 const value=JSON.parse(encoded);
 if(Array.isArray(value?.content))return value;
 const ref=value?.bridge_result_chunks;
 if(!ref)throw Error('Invalid stored controller result');
 return readResultChunks(db,id,ref.lease,ref.digest,ref.parts);
}
async function renewResultClaim(db,node,d) {
 const now=epoch();
 const row=await db.prepare("UPDATE bridge_jobs SET lease_until=?,deadline=max(deadline,?),updated=? WHERE id=? AND lease=? AND state='claimed' AND lease_until>=? AND (controller IS NULL OR controller=?) RETURNING id")
  .bind(now+35,now+35,now,d.id,d.lease,now,node?.id??'').first();
 if(!row)throw Error('Controller result ownership expired or changed');
}
async function resultFrame(db,node,d,path,time) {
 if(typeof d.id!=='string'||typeof d.lease!=='string'||!(/^[a-f0-9]{64}$/).test(d.digest))
  return reply({error:'Invalid result frame'},400);
 const owned=await db.prepare("SELECT state,result FROM bridge_jobs WHERE id=? AND lease=? AND (controller IS NULL OR controller=?)")
  .bind(d.id,d.lease,node?.id??'').first();
 if(!owned)return reply({accepted:false},409);
 if(owned.state==='done') {
  const ref=JSON.parse(owned.result)?.bridge_result_chunks;
  return reply({accepted:ref?.digest===d.digest},ref?.digest===d.digest?200:409);
 }
 if(owned.state!=='claimed')return reply({accepted:false},409);
 if(path==='/bridge/result-chunk') {
  if(!Number.isSafeInteger(d.part)||d.part<0||typeof d.data!=='string'||!d.data.length||
     new TextEncoder().encode(d.data).length>resultChunkSize)return reply({error:'Invalid result chunk'},400);
  try { await renewResultClaim(db,node,d); } catch { return reply({accepted:false},409); }
  // Immutable, retryable chunks. Recheck ownership in the write, not just above.
  const row=await db.prepare("INSERT INTO bridge_result_chunks(job,lease,digest,part,data) SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM bridge_jobs WHERE id=? AND lease=? AND state='claimed' AND lease_until>=? AND (controller IS NULL OR controller=?)) ON CONFLICT(job,lease,digest,part) DO UPDATE SET data=excluded.data WHERE data=excluded.data RETURNING part")
   .bind(d.id,d.lease,d.digest,d.part,d.data,d.id,d.lease,epoch(),node?.id??'').first();
  return reply({accepted:!!row},row?200:409);
 }
 if(!Number.isSafeInteger(d.parts)||d.parts<1)return reply({error:'Invalid result manifest'},400);
 const count=await db.prepare('SELECT count(*) n,min(part) first,max(part) last FROM bridge_result_chunks WHERE job=? AND lease=? AND digest=?')
  .bind(d.id,d.lease,d.digest).first();
 if(count.n!==d.parts||count.first!==0||count.last!==d.parts-1)return reply({error:'Incomplete controller result'},409);
 try { await readResultChunks(db,d.id,d.lease,d.digest,d.parts,()=>renewResultClaim(db,node,d)); }
 catch { return reply({error:'Invalid controller result'},400); }
 const manifest=JSON.stringify({bridge_result_chunks:{lease:d.lease,digest:d.digest,parts:d.parts}});
 const row=await db.prepare("UPDATE bridge_jobs SET state='done',request=CASE WHEN id LIKE 'event-%' THEN '{}' ELSE request END,result=?,updated=? WHERE id=? AND lease=? AND state='claimed' AND lease_until>=? AND (controller IS NULL OR controller=?) RETURNING id")
  .bind(manifest,epoch(),d.id,d.lease,epoch(),node?.id??'').first();
 return reply({accepted:!!row},row?200:409);
}

export async function device(req, env, path) {
 const token = /^Bearer ([0-9a-f]{64})$/.exec(req.headers.get('Authorization')||'')?.[1];
 const legacy=!!token&&!!env.BIOME_BRIDGE_PIN&&sameSecret(await hexHash(await hexHash(token)+'\n'),env.BIOME_BRIDGE_PIN);
 const node=token?await env.DB.prepare('SELECT * FROM controller_nodes WHERE token_hash=? AND enabled=1').bind(await hexHash(token)).first():null;
 if(!legacy&&!node)return reply({error:'Invalid device credential'},401);
 if(path==='/bridge/connect')return controllerSocket(req,env,node);
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
  const job = selected.legacy? await db.prepare("UPDATE bridge_jobs SET state='claimed',lease=?,lease_until=?,updated=? WHERE id=(SELECT id FROM bridge_jobs WHERE (deadline>=? OR id LIKE 'native-%') AND (state='queued' OR (state='claimed' AND lease_until<?)) ORDER BY updated,id LIMIT 1) RETURNING id,request")
   .bind(lease,time+35,time,time,time).first():selected;
  if (!job||job.idle) return reply({idle:true,primary:job?.primary});
  let request=JSON.parse(job.request);
  if (request.name==='sealed_grok_receiver')
   request={name:'grok_receiver_change',arguments:await openGrokJob(env,job.id,request.arguments)};
  return reply({id:job.id,lease,request});
 }
 if(path==='/bridge/result-chunk'||path==='/bridge/result-commit')
  return resultFrame(db,node,await limitedJSON(req,500000),path,time);
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
 // Discovery request_id identifies a resumable snapshot, including if upload
 // outlasts the HTTP waiter. A new request_id requests fresh discovery.
 // A status read must see later transitions even when a client repeats the
 // exact next_call returned in a queued receipt. Mutation retries stay durable.
 const liveRead=name==='biome_invoke' && ['cap.status','cap.submission','cap.lease','cap.leases','bus.inbox','provider.poll'].includes(args.operation);
 const key = name==='biome_invoke' && !liveRead ? 'invoke-'+await hexHash((args.realm||'site')+'\n'+args.request_id) : crypto.randomUUID();
 return enqueue(env,name,args,key);
}
export async function enqueue(env,name,args,key){
 const payload=JSON.stringify(canonical({name,arguments:args})), time=epoch(), db=env.DB;
 if(payload.length>96000) return toolError('Request too large');
 await db.prepare("DELETE FROM bridge_jobs WHERE updated<? AND id NOT LIKE 'native-%'").bind(time-3600).run();
 await db.prepare("INSERT OR IGNORE INTO bridge_jobs(id,request,state,deadline,updated) SELECT ?,?,'queued',?,? WHERE (SELECT count(*) FROM bridge_jobs WHERE state!='done' AND deadline>=?)<40")
  .bind(key,payload,time+22,time,time).run();
 let row=await db.prepare('SELECT request,state,result,deadline FROM bridge_jobs WHERE id=?').bind(key).first();
 if(!row) return toolError('Controller queue is busy. Retry with the same request_id.');
 if(key.startsWith('invoke-') && row.request!==payload) return toolError('request_id already belongs to a different request.');
 if(row.state==='done') return readBridgeResult(db,key,row.result);
 if(row.deadline<time) await db.prepare("UPDATE bridge_jobs SET deadline=?,updated=? WHERE id=? AND state!='done'").bind(time+22,time,key).run();
 for(let turn=0;turn<80;turn++){
  await wait(250);
  row=await db.prepare('SELECT result FROM bridge_jobs WHERE id=?').bind(key).first();
  if(row?.result) return readBridgeResult(db,key,row.result);
 }
 return toolError('The controller has not replied yet. Retry with the same request_id to recover the result.');
}
