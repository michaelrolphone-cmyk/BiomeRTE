import {controllerRuntimeTool} from './controller-runtime.mjs';
import {routeProviderInvocation,rememberProviderInvocation} from './provider-topology.mjs';
import {routeCapabilityInvocation,federatedCatalog} from './capability-map.mjs';
import {activeQueueDiagnostics} from './diagnostics.mjs';
import {journalText,journalRecord} from './journal-storage.mjs';
import {maintenanceCall} from './maintenance.mjs';
import {controllerDevice,claimControllerJob,controllerStatus} from './ha.mjs';
import {openGrokJob} from './grok.mjs';
import {canDeliver} from './events.mjs';
import {sameSecret} from './access.mjs';
import {controllerSocket} from './transport.mjs';
import {compactRuns} from './recovery.mjs';
export const reply = (data, status=200) => Response.json(data, {status, headers:{'Cache-Control':'no-store'}});
const wait = ms => new Promise(r => setTimeout(r, ms));
const epoch = () => Math.floor(Date.now()/1000);
export const hexHash = async text => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))), b => b.toString(16).padStart(2,'0')).join('');

export async function limitedText(req, max) {
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
 return new TextDecoder().decode(data);
}

export async function limitedJSON(req, max) {
 return JSON.parse(await limitedText(req,max));
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
 const row=await db.prepare("UPDATE bridge_jobs SET state='done',request=CASE WHEN id LIKE 'event-%' THEN '{}' WHEN id LIKE 'packet-%' THEN json_remove(request,'$.arguments.chunk') ELSE request END,result=?,updated=? WHERE id=? AND lease=? AND state='claimed' AND lease_until>=? AND (controller IS NULL OR controller=?) RETURNING id")
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
  // A registered device verifies its own round trip, independently of the
  // gateway default. Only its authenticated transport selects the existing
  // application reference; request data cannot choose an owner or operation.
  let args={};
  if(node){
   let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{return reply({error:'Invalid controller routing configuration'},503)}
   if(!mapping||typeof mapping!=='object'||Array.isArray(mapping))return reply({error:'Invalid controller routing configuration'},503);
   const identities=Object.entries(mapping).filter(([identity,transport])=>/^[a-f0-9]{64}$/.test(identity)&&transport===node.id);
   if(identities.length!==1)return reply({error:'Device check requires one configured application identity'},409);
   args={controller:identities[0][0]};
  }
  // This authenticated device check proves the physical link's round trip.
  // Managed controller routing belongs to operator calls, not recovery probes:
  // redirecting here would hide a failed link behind the logical authority.
  // Unregistered legacy bridge credentials retain their native default check.
  return reply(await enqueue(env, 'biome_status',args,crypto.randomUUID()));
 }
 const db = env.DB, time = epoch();
 if (path === '/bridge/next') {
  const selected=await claimControllerJob(db,node,env);
  const lease = selected.lease||crypto.randomUUID();
  const job = selected.legacy? await db.prepare("UPDATE bridge_jobs SET state='claimed',lease=?,lease_until=?,updated=? WHERE id=(SELECT id FROM bridge_jobs WHERE deadline>=? AND (state='queued' OR (state='claimed' AND lease_until<?)) ORDER BY updated,id LIMIT 1) RETURNING id,request")
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
  const row = await db.prepare("UPDATE bridge_jobs SET state='done',request=CASE WHEN id LIKE 'event-%' THEN '{}' WHEN id LIKE 'packet-%' THEN json_remove(request,'$.arguments.chunk') ELSE request END,result=?,updated=? WHERE id=? AND lease=? AND state='claimed' AND (controller IS NULL OR controller=?) RETURNING id")
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
// Cached MCP schemas allow arbitrary operation arguments but may not yet expose
// the optional top-level controller selector. Unwrap exactly one routing layer.
export function controllerInvocation(args){
 if(args?.operation!=='controller.invoke')return args;
 const call=args.arguments;
 if(args.controller!==undefined||!call||typeof call!=='object'||Array.isArray(call)||
    Object.keys(call).some(key=>!['controller','operation','arguments','realm'].includes(key))||
    typeof call.controller!=='string'||!/^[a-f0-9]{64}$/.test(call.controller)||
    typeof call.operation!=='string'||!call.operation.length||call.operation.length>160||
    !call.arguments||typeof call.arguments!=='object'||Array.isArray(call.arguments)||
    !['site','local'].includes(call.realm??'site')||
    (args.realm!==undefined&&call.realm!==undefined&&args.realm!==call.realm))throw Error('Invalid controller.invoke arguments');
 if(call.operation.startsWith('controller.')||call.operation.startsWith('maintenance.')||call.operation.startsWith('grok.receiver.'))throw Error('Controller routing cannot nest or redirect gateway management operations');
 if(['system.discovery','discovery.page'].includes(call.operation)&&call.arguments.section==='controllers')throw Error('Controller catalog does not accept a selected controller');
 const realm=call.realm??args.realm;
 return {controller:call.controller,operation:call.operation==='discovery.page'?'system.discovery':call.operation,
  arguments:call.arguments,request_id:args.request_id,...(realm===undefined?{}:{realm})};
}

export function routedControllerCall(controller,call,realm){
 if(!call||typeof call!=='object'||Array.isArray(call)||
    Object.keys(call).some(key=>!['controller','operation','arguments','realm','request_id'].includes(key))||
    typeof call.request_id!=='string'||!call.request_id.trim()||call.request_id.length>128||
    (call.controller!==undefined&&call.controller!==controller))throw Error('Controller continuation changes the selected owner');
 const next={operation:'controller.invoke',arguments:{controller,operation:call.operation,arguments:call.arguments,
  ...((call.realm??realm)===undefined?{}:{realm:call.realm??realm})},request_id:call.request_id};
 // Validate the same bounded form used for incoming invocations. Never pass a
 // native continuation through unpinned when it cannot be safely represented.
 controllerInvocation(next);
 return next;
}

function routedControllerResult(result,controller,realm,discovery=false){
 if(result.isError)return result;
 const route=value=>{
  if(!value||typeof value!=='object'||Array.isArray(value))return value;
  if(discovery&&value.controller!==undefined&&value.controller!==controller)throw Error('Controller result changes the selected owner');
  return {...value,...(discovery?{controller}:{}),...(value.next_call?{next_call:routedControllerCall(controller,value.next_call,realm)}:{})};
 };
 try{
  const out={...result};
  if(result.structuredContent!==undefined)out.structuredContent=route(result.structuredContent);
  if(Array.isArray(result.content))out.content=result.content.map(item=>{
   if(item.type!=='text')return item;
   let value;try{value=JSON.parse(item.text)}catch{return item}
   return {...item,text:JSON.stringify(route(value))};
  });
  return out;
 }catch(error){return toolError(error.message)}
}

function retryableJournalError(result) {
 // These errors originate in GatewayJournal, outside the domain transaction.
 // Never infer retryability from provider output, arbitrary error substrings,
 // or a successful result which happens to contain the same words.
 return result?.isError===true && result.content?.length===1 &&
  result.content[0].type==='text' &&
  /^(?:Gateway journal HTTP 5\d\d|Gateway journal unavailable)$/.test(result.content[0].text);
}
export const DEFAULT_DISCOVERY_LIMIT = 25;
export const toolsList = [
 {name:'biome_status',description:'Verify connectivity to the running physical controller and read its health.',inputSchema:{type:'object',properties:{controller:{type:'string',pattern:'^[a-f0-9]{64}$'}},additionalProperties:false},annotations:{readOnlyHint:true}},
 {name:'biome_discover',description:'Discover the system capability map. Contracts and offers aggregate all controllers unless an explicit controller is selected. Implementation placement is optional per request. Discover controller operations, contracts, offers and caller guidance. In local mode section controllers lists routable controllers even when the default is offline. Pages default to 25 items. Pass a positive integer limit to change the page size, section to filter, or cursor to continue. Cached clients can call discovery.page via biome_invoke.',inputSchema:{type:'object',properties:{controller:{type:'string',pattern:'^[a-f0-9]{64}$'},section:{type:'string',enum:['operations','contracts','offers','guide','controllers']},cursor:{type:'string'},limit:{type:'integer',minimum:1,default:DEFAULT_DISCOVERY_LIMIT}},additionalProperties:false},annotations:{readOnlyHint:true}},
 {name:'biome_work_list',description:'List work stored on the running controller.',inputSchema:{type:'object',properties:{controller:{type:'string',pattern:'^[a-f0-9]{64}$'}},additionalProperties:false},annotations:{readOnlyHint:true}},
 {name:'biome_invoke',description:'Acquire a lease, invoke with a unique command ID, then read cap.status using that ID. Follow next_call for current state. provider.poll is the worker inbox. Cached schemas select an owner with operation controller.invoke and arguments {controller,operation,arguments,realm?}. Reuse a mutation request_id only for an identical retry.',inputSchema:{type:'object',properties:{provider_context:{type:'object',properties:{provider:{type:'string'},host:{type:'string'},addresses:{type:'array',items:{type:'string'}}},required:['provider','host'],additionalProperties:false},controller:{type:'string',pattern:'^[a-f0-9]{64}$'},operation:{type:'string'},arguments:{type:'object',additionalProperties:true},request_id:{type:'string',minLength:1,maxLength:128},realm:{type:'string',enum:['site','local']}},required:['operation','arguments','request_id'],additionalProperties:false}}
];
export async function runTool(env, name, args={}) {
 if (!toolsList.some(t=>t.name===name)) return toolError('Unknown tool');
 if (!args || typeof args!=='object' || Array.isArray(args)) return toolError('Expected an arguments object');
 if (name==='biome_invoke') {
  if (typeof args.operation!=='string' || !args.arguments || typeof args.arguments!=='object' || Array.isArray(args.arguments) ||
      typeof args.request_id!=='string' || !args.request_id.trim() || args.request_id.length>128 ||
      Object.keys(args).some(k=>!['operation','arguments','request_id','realm','controller','provider_context'].includes(k)) ||
      !['site','local'].includes(args.realm??'site')) return toolError('Expected operation, arguments, request_id and optional realm');
 } else if (Object.keys(args).some(k=>k!=='controller')) return toolError('This tool accepts only an optional controller selector');
 const routed=name==='biome_invoke'&&args.operation==='controller.invoke';
 if(routed){try{args=controllerInvocation(args)}catch(error){return toolError(error.message)}}
 const managed=await controllerRuntimeTool(env,name,args);
 if(managed)return routed?routedControllerResult(managed,args.controller,args.realm,args.operation==='system.discovery'):managed;
 if(args.controller!==undefined){try{await resolveControllerOwner(env,args.controller)}catch(error){return toolError(error.message)}
  if(name==='biome_invoke'&&(args.operation.startsWith('maintenance.')||args.operation.startsWith('controller.')))return toolError('Gateway maintenance and recovery cannot be redirected with a controller selector');
 }
 if(name==='biome_invoke' && args.operation.startsWith('maintenance.')) {
  try{return {content:[{type:'text',text:JSON.stringify(await maintenanceCall(env,args.operation,args.arguments,args.request_id))}]}}catch(e){return toolError(e.message)}
 }
 if(name==='biome_invoke' && ['controller.reconnect','controller.compact_runs'].includes(args.operation))return toolError('Explicit Android confirmation is required. Prepare a maintenance command for approval.');
 if(name==='biome_invoke' && args.operation==='controller.reconnect') {
  const {node}=args.arguments;
  const row=await env.DB.prepare('SELECT enabled FROM controller_nodes WHERE id=?').bind(node).first();
  if(!row?.enabled)return toolError('Unknown or disabled controller');
  await env.DB.prepare('UPDATE controller_nodes SET enabled=0 WHERE id=?').bind(node).run();
  try{await wait(1000)}finally{await env.DB.prepare('UPDATE controller_nodes SET enabled=1 WHERE id=?').bind(node).run()}
  return {content:[{type:'text',text:JSON.stringify({node,reconnect_requested:true})}]};
 }
 if(name==='biome_invoke' && args.operation==='controller.compact_runs') {
  try{return {content:[{type:'text',text:JSON.stringify(await compactRuns(env,args.arguments,args.request_id))}]}}catch(e){return toolError(e.message)}
 }
 // Authenticated recovery reads cannot depend on the unhealthy controller.
 if(name==='biome_invoke' && args.operation==='controller.diagnostics') {
  const cluster=await controllerStatus(env.DB,env);
  const journal=env.CONTROLLER_COORDINATION_MODE==='local'?{results:[]}:await env.DB.prepare('SELECT seq,length(entry) AS bytes FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 8').bind(cluster.site).all();
  const now=epoch();
  const [queue,active_queue]=await Promise.all([env.DB.prepare("SELECT state,COUNT(*) AS count FROM bridge_jobs WHERE deadline>=? GROUP BY state").bind(now).all(),activeQueueDiagnostics(env.DB,now)]);
  return {content:[{type:'text',text:JSON.stringify({cluster,journal:journal.results,queue:queue.results,active_queue})}]};
 }
 if(env.CONTROLLER_COORDINATION_MODE==='local'&&name==='biome_invoke'&&['controller.record_page','controller.journal_page'].includes(args.operation))return toolError('Shared recovery records unavailable in local mode; inspect the owning controller');
 if(name==='biome_invoke' && args.operation==='controller.journal_page') {
  const {seq,offset=0}=args.arguments;
  if(!Number.isInteger(seq)||seq<1||!Number.isInteger(offset)||offset<0)return toolError('Expected positive seq and nonnegative offset');
  const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
  const row=await env.DB.prepare('SELECT seq,hash,entry FROM controller_journal WHERE site=? AND seq=?').bind(config.site,seq).first();
  const text=await journalText(env,config.site,row),part=text?.slice(offset,offset+24000);
  return {content:[{type:'text',text:JSON.stringify(row?{seq,length:text.length,part,next:offset+part.length<text.length?offset+part.length:null}:null)}]};
 }
 if(name==='biome_invoke' && args.operation==='controller.record_page') {
  const {kind,id,offset=0}=args.arguments;
  if(!['process_versions','runs','subscriptions','_meta','_submission_active','leases','actions','contracts','offers','resources'].includes(kind)||typeof id!=='string'||!Number.isInteger(offset)||offset<0)return toolError('Invalid recovery record');
  const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
  const found=await journalRecord(env,config.site,kind,id);
  const value=found?.value??null;
  const body=typeof value==='string'?value:JSON.stringify(value);
  const part=body.slice(offset,offset+24000);
  return {content:[{type:'text',text:JSON.stringify({length:body.length,part,next:offset+part.length<body.length?offset+part.length:null})}]};
 }
 // Discovery request_id identifies a resumable snapshot, including if upload
 // outlasts the HTTP waiter. A new request_id requests fresh discovery.
 // A status read must see later transitions even when a client repeats the
 // exact next_call returned in a queued receipt. Mutation retries stay durable.
 const liveRead=name==='biome_invoke' && ['cap.status','cap.submission','cap.lease','cap.leases','bus.inbox','provider.poll'].includes(args.operation);
 const key = name==='biome_invoke' && !liveRead ? 'invoke-'+await hexHash((args.realm||'site')+'\n'+args.request_id) : crypto.randomUUID();
 if(name==='biome_invoke'&&args.operation==='cap.discover'&&args.controller===undefined&&env.CONTROLLER_COORDINATION_MODE==='local'){
  try{if(Object.keys(args.arguments).some(k=>k!=='contract')||(args.arguments.contract!==undefined&&typeof args.arguments.contract!=='string'))return toolError('Invalid capability discovery arguments');const catalog=await federatedCatalog(env,(await hexHash(args.request_id)).slice(0,32),args.arguments.contract??'',runTool,args.realm??'site');return {content:[{type:'text',text:JSON.stringify(catalog)}]};}catch(error){return toolError(error.message)}
 }
 const original=args;let placement={args};
 if(name==='biome_invoke'){
  try{placement=await routeProviderInvocation(env,args,async(controller,arguments_,request_id)=>{
   const r=await enqueue(env,'biome_invoke',{controller,operation:'cap.offer',arguments:arguments_,request_id,realm:'site'},'invoke-'+await hexHash('site\n'+request_id));
   if(r.isError)throw Error(r.content?.[0]?.text??'Provider re-registration failed');
  });args=await routeCapabilityInvocation(env,placement.args);if(args._capability_result)return routedControllerResult(args._capability_result,args.controller,args.realm);placement.args=args;}catch(error){return toolError(error.message)}
 }
 const result=await enqueue(env,name,args,key);
 if(name==='biome_invoke')await rememberProviderInvocation(env,original,placement,result);
 const out=(routed||(!original.controller&&args.controller))?routedControllerResult(result,args.controller,args.realm,args.operation==='system.discovery'):result;
 return placement.placement?{...out,_meta:{...out._meta,provider_assignment:placement.placement}}:out;
}
export async function resolveControllerOwner(env,controller){
 if(controller!==undefined){
  if(typeof controller!=='string'||!/^[a-f0-9]{64}$/.test(controller))throw Object.assign(Error('Invalid controller identity'),{status:400});
  let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{throw Object.assign(Error('Invalid controller routing configuration'),{status:503})}
  const owner=mapping&&Object.hasOwn(mapping,controller)?mapping[controller]:null;
  if(typeof owner!=='string'||!await env.DB.prepare('SELECT id FROM controller_nodes WHERE id=? AND enabled=1').bind(owner).first())throw Object.assign(Error('Explicit controller is not registered for routing'),{status:409});
  return owner;
 }
 if(env.CONTROLLER_COORDINATION_MODE==='local'){
  if(!env.LOCAL_CONTROLLER_ID)throw Object.assign(Error('Local controller owner is not configured'),{status:503});
  return env.LOCAL_CONTROLLER_ID;
 }
 return null;
}
export async function enqueue(env,name,args,key,diagnostic=null){
 const mark=(stage,reason,fields={})=>{if(diagnostic)Object.assign(diagnostic,{stage,reason,...fields})};
 const readResult=async(encoded,source)=>{
  mark('response','stored_result_read_failed',{result_source:source});
  const value=await readBridgeResult(env.DB,key,encoded);
  mark('controller',value?.isError?'controller_tool_error':'controller_reply');
  return value;
 };
 let owner;
 try{owner=await resolveControllerOwner(env,args.controller);}catch(error){return toolError(error.message)}
 args={...args};delete args.controller_owner;
 if(owner){args.controller_owner=owner;key=key.startsWith('invoke-')?'invoke-'+await hexHash(owner+'\n'+key):key;}

 mark('request','request_serialization_failed');
 const payload=JSON.stringify(canonical({name,arguments:args})), time=epoch(), db=env.DB;
 mark('queue_admission','queue_cleanup_failed');
 await db.prepare('DELETE FROM bridge_jobs WHERE updated<?').bind(time-3600).run();
 if(key.startsWith('packet-'))await db.prepare("DELETE FROM bridge_jobs WHERE id IN (SELECT id FROM bridge_jobs WHERE id LIKE 'packet-%' AND state='done' ORDER BY updated DESC,id LIMIT -1 OFFSET 128)").run();
 mark('queue_admission','queue_insert_failed');
 await db.prepare("INSERT OR IGNORE INTO bridge_jobs(id,request,state,deadline,updated) SELECT ?,?,'queued',?,? WHERE (SELECT count(*) FROM bridge_jobs WHERE state!='done' AND deadline>=?)<40 AND (?=0 OR ((SELECT count(*) FROM bridge_jobs WHERE id LIKE 'packet-%')<256 AND (SELECT count(*) FROM bridge_jobs WHERE id LIKE 'packet-%' AND state!='done' AND deadline>=? AND json_extract(request,'$.arguments.from')=?)<8))")
  .bind(key,payload,time+22,time,time,Number(key.startsWith('packet-')),time,args.from??'').run();
 mark('queue_admission','queue_read_failed');
 let row=await db.prepare('SELECT request,state,result,deadline,updated FROM bridge_jobs WHERE id=?').bind(key).first();
 if(!row){mark('queue_admission','queue_busy',{queue_capacity:40});return toolError('Controller queue is busy. Retry with the same request_id.');}
 mark('queue_admission','request_identity_conflict');
 let comparison=payload;
 if(key.startsWith('packet-')&&row.state==='done'){const withoutChunk=JSON.parse(payload);delete withoutChunk.arguments.chunk;comparison=JSON.stringify(canonical(withoutChunk));}
 if((key.startsWith('invoke-')||key.startsWith('packet-'))&&JSON.stringify(canonical(JSON.parse(row.request)))!==comparison)return toolError('request_id already belongs to a different request.');
 if(row.state==='done') {
  const result=await readResult(row.result,'cached');
  if(name!=='biome_invoke'||!key.startsWith('invoke-')||!retryableJournalError(result)||row.updated>time-2)return result;
  // A transport failure is not a completed domain command. Retry only on an
  // identical caller request, with its original request_id and operation ID.
  // The controller's atomic receipt resolves even an ambiguous commit without
  // repeating its effect. Keep admission bounds and fence the previous worker.
  // CAS on the exact result makes concurrent retries share one new dispatch.
  mark('queue_admission','requeue_write_failed');
  await db.prepare("UPDATE bridge_jobs SET state='queued',result=NULL,lease=NULL,lease_until=NULL,controller=NULL,deadline=?,updated=? WHERE id=? AND state='done' AND result=? AND updated<=? AND (SELECT count(*) FROM bridge_jobs WHERE state!='done' AND deadline>=?)<40")
   .bind(time+22,time,key,row.result,time-2,time).run();
  mark('queue_admission','requeue_read_failed');
  row=await db.prepare('SELECT request,state,result,deadline,updated FROM bridge_jobs WHERE id=?').bind(key).first();
  if(row?.state==='done')return readResult(row.result,'cached');
 }
 mark('controller_wait','deadline_refresh_failed');
 if(row.deadline<time){
  const refreshed=await db.prepare("UPDATE bridge_jobs SET deadline=?,updated=? WHERE id=? AND state!='done' AND (?=0 OR ((SELECT count(*) FROM bridge_jobs WHERE state!='done' AND deadline>=?)<40 AND (SELECT count(*) FROM bridge_jobs WHERE id LIKE 'packet-%' AND state!='done' AND deadline>=? AND json_extract(request,'$.arguments.from')=?)<8))").bind(time+22,time,key,Number(key.startsWith('packet-')),time,time,args.from??'').run();
  if(key.startsWith('packet-')&&!(refreshed.meta?.changes??refreshed.changes))return toolError('Packet pending quota reached; retry later.');
 }
 for(let turn=0;turn<80;turn++){
  await wait(250);
  mark('controller_wait','result_poll_failed',{poll_count:turn+1});
  row=await db.prepare('SELECT state,result FROM bridge_jobs WHERE id=?').bind(key).first();
  if(diagnostic)diagnostic.job_state=['queued','claimed','done'].includes(row?.state)?row.state:'missing';
  if(row?.result) return readResult(row.result,'live');
 }
 mark('controller_wait','controller_reply_timeout');
 return toolError('The controller has not replied yet. Retry with the same request_id to recover the result.');
}
