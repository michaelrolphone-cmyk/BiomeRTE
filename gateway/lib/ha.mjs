import {controllerNetworkSchema,controllerNetworkUpdate,controllerNetworkRegistration} from './controller-network.mjs';
import {retireLegacyJournal} from './journal-retirement.mjs';
import {ownershipTransfer,ownershipBootstrapAuthorized} from './ownership-transfer.mjs';
import {coordinationSession} from './coordination-session.mjs';
import {readCheckpoint,buildCheckpoint,checkpointSchema,checkpointMaintenance,readCheckpointObjects,releaseCheckpoint,renewCheckpoint,checkpointStatus,recordJournalRejection} from './journal-checkpoint.mjs';
import {storedJournal,journalText,archiveJournalBatch,journalEntryLimit,journalAdmission} from './journal-storage.mjs';
import {expireUncommittedJournalUpload} from './journal-upload.mjs';
import {reply,limitedJSON,hexHash,resolveControllerOwner,enqueue} from './bridge.mjs';
import {sealGrokJob,openGrokJob} from './grok.mjs';

export async function controllerRoutingState(db,env={}) {
 await controllerNetworkSchema(db);
 const [config,nodeRows]=await Promise.all([db.prepare('SELECT * FROM controller_config WHERE id=1').first(),db.prepare('SELECT id,priority,capacity,enabled,seen,ready,position,load,host,network FROM controller_nodes ORDER BY priority,id').all()]);
 if(!config)return {enabled:false};
 const now=Date.now()/1000;
 const nodes=nodeRows.results.map(node=>({...node,network:controllerNetworkRegistration(env,node,config.heartbeat_seconds,now,!!config.enabled)}));
 const live=nodes.filter(n=>n.enabled&&n.ready&&n.seen>now-config.heartbeat_seconds);
 const primary=env.CONTROLLER_COORDINATION_MODE==='local'
  ?(live.some(n=>n.id===env.LOCAL_CONTROLLER_ID)?env.LOCAL_CONTROLLER_ID:null)
  :(live[0]?.id??null);
 return {...config,enabled:!!config.enabled,primary,nodes};
}
export async function controllerStatus(db,env={}) {
 const state=await controllerRoutingState(db,env);
 if(!state.nodes)return state;
 if(env.CONTROLLER_COORDINATION_MODE==='local')return {...state,head:{seq:0,hash:'',mode:'local'},checkpoint:null};
 const [head,checkpoint]=await Promise.all([journalHead(db,state.site),checkpointStatus(db,state.site)]);
 return {...state,head,checkpoint};
}
async function journalHead(db,site) {
 return await db.prepare('SELECT seq,hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1').bind(site).first()??{seq:0,hash:''};
}
export async function controllerDevice(req,env,path,node,legacy) {
 const db=env.DB,now=Date.now()/1000;
 let d;
 if(path==='/bridge/ha/append'||path==='/bridge/ha/session/append'){
  try{d=await limitedJSON(req,journalEntryLimit(env)*2+65536);}catch{return reply({error:'Journal request exceeds bounded transaction admission or is invalid'},413);}
 }else d=await limitedJSON(req,1500000);
 if(path==='/bridge/ha/configure'){
  if(!legacy)return reply({error:'Commissioning credential required'},403);
  if(typeof d.site!=='string'||!Array.isArray(d.nodes)||!d.nodes.length)return reply({error:'Invalid configuration'},400);
  const prior=await db.prepare('SELECT * FROM controller_config WHERE id=1').first();
  if(prior&&prior.site!==d.site)return reply({error:'Site identity cannot change'},409);
  for(const n of d.nodes){
   if(!/^[a-zA-Z0-9_.-]{1,128}$/.test(n.id)||! /^[0-9a-f]{64}$/.test(n.token_hash)||!Number.isInteger(n.priority)||!Number.isInteger(n.capacity)||n.capacity<1)return reply({error:'Invalid controller'},400);
  }
  await db.prepare('INSERT OR IGNORE INTO controller_config(id,site) VALUES(1,?)').bind(d.site).run();
  for(const n of d.nodes)await db.prepare('INSERT INTO controller_nodes(id,token_hash,priority,capacity) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,priority=excluded.priority,capacity=excluded.capacity').bind(n.id,n.token_hash,n.priority,n.capacity).run();
  if(typeof d.enabled==='boolean')await db.prepare('UPDATE controller_config SET enabled=? WHERE id=1').bind(Number(d.enabled)).run();
  return reply(await controllerStatus(db,env));
 }
 if(!node)return reply({error:'Registered controller required'},403);
 const config=await db.prepare('SELECT * FROM controller_config WHERE id=1').first();
 if(!config)return reply({error:'Cluster not configured'},409);
 if(path.startsWith('/bridge/ha/ownership/')){
  if(env.CONTROLLER_COORDINATION_MODE!=='local')return reply({error:'Ownership control requires local SQLite mode'},409);
  try{
   const operation=path.slice('/bridge/ha/ownership/'.length);
   const bootstrapAuthorized=operation==='bootstrap'&&await ownershipBootstrapAuthorized(env,d);
   return reply(await ownershipTransfer(env,config.site,node,operation,d,undefined,{bootstrapAuthorized}));
  }catch(error){if(error.status)return reply({error:error.message},error.status);throw error;}
 }
 if(path.startsWith('/bridge/ha/retirement/')){try{return reply(await retireLegacyJournal(env,config.site,node,path.slice('/bridge/ha/retirement/'.length),d))}catch(error){if(error.status)return reply({error:error.message},error.status);throw error;}}
 if(path==='/bridge/ha/packet')return relaySessionPacket(env,node,d);
 if(path.startsWith('/bridge/ha/session/')){
  try{
   let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{throw Object.assign(Error('Invalid controller routing configuration'),{status:503})}
   const identities=Object.entries(mapping??{}).filter(([identity,transport])=>/^[a-f0-9]{64}$/.test(identity)&&transport===node.id);
   if(identities.length!==1)throw Object.assign(Error('Session transport requires one configured application identity'),{status:409});
   const operation=path.slice('/bridge/ha/session/'.length);
   if(operation==='open'&&Array.isArray(d.participants))for(const participant of d.participants)await resolveControllerOwner(env,participant);
   return reply(await coordinationSession(db,config.site,identities[0][0],operation,d,undefined,{verifiedParticipants:true,maxActive:Number(env.COORDINATION_MAX_ACTIVE_SESSIONS??8),maxReservedBytes:Number(env.COORDINATION_MAX_SITE_BYTES??67108864),maxSeconds:Number(env.COORDINATION_MAX_SESSION_SECONDS??86400),closerLeaseSeconds:Number(env.COORDINATION_CLOSER_LEASE_SECONDS??60)}));
  }
  catch(error){if(error.status)return reply({error:error.message},error.status);throw error;}
 }
 if(path==='/bridge/ha/heartbeat'){
  await db.prepare("DELETE FROM bridge_jobs WHERE id LIKE 'packet-%' AND updated<?").bind(now-3600).run();
  if(!Number.isInteger(d.position)||d.position<0||typeof d.ready!=='boolean'||!Number.isFinite(d.load)||typeof d.host!=='string')return reply({error:'Invalid heartbeat'},400);
  let network;
  try{network=controllerNetworkUpdate(req,d,now);}catch(error){return reply({error:error.message},error.status??400);}
  await controllerNetworkSchema(db);
  await db.prepare('UPDATE controller_nodes SET seen=?,ready=?,position=?,load=?,host=?,network='+network.expression+' WHERE id=? AND seen<=?')
   .bind(now,Number(d.ready),d.position,d.load,d.host.slice(0,160),...network.parameters,node.id,now).run();
  return reply(await controllerStatus(db,env));
 }
 if(env.CONTROLLER_COORDINATION_MODE==='local'&&(path.startsWith('/bridge/ha/checkpoint')||['/bridge/ha/read','/bridge/ha/append'].includes(path)))return reply({error:'Permanent shared journal disabled in local mode'},410);
 if(path==='/bridge/ha/checkpoint'){
  if(!Number.isSafeInteger(d.max_seq)||d.max_seq<0)return reply({error:'Invalid checkpoint bound'},400);
  return reply(await readCheckpoint(env,config.site,d.max_seq,node.id));
 }
 if(path==='/bridge/ha/checkpoint/renew')return reply(await renewCheckpoint(env,config.site,d.lease,node.id));
 if(path==='/bridge/ha/checkpoint/release')return reply(await releaseCheckpoint(env,config.site,d.lease,node.id));
 if(path==='/bridge/ha/checkpoint/objects')return reply(await readCheckpointObjects(env,config.site,d.digests,d.lease,node.id));
 if(path==='/bridge/ha/checkpoint/maintain')return reply(await checkpointMaintenance(env,config.site));
 if(path==='/bridge/ha/checkpoint/build')return reply(await buildCheckpoint(env,config.site));
 if(path==='/bridge/ha/status')return reply(await controllerStatus(db,env));
 if(path==='/bridge/ha/read'){
  if(d.key==='head'){
   const head=await journalHead(db,config.site);await checkpointSchema(db);
   const checkpoints=await db.prepare('SELECT current,previous FROM controller_checkpoints WHERE site=?').bind(config.site).first();
   return reply({value:head.seq?head:null,version:String(head.seq),checkpoint:checkpoints?.current?{...JSON.parse(checkpoints.current),recovery_seq:checkpoints.previous?JSON.parse(checkpoints.previous).seq:JSON.parse(checkpoints.current).seq}:null});
  }
  if(!/^log\/\d{20}$/.test(d.key))return reply({error:'Invalid journal key'},400);
  const row=await db.prepare('SELECT entry,hash FROM controller_journal WHERE site=? AND seq=?').bind(config.site,Number(d.key.slice(4))).first();
  return reply({encoded:await journalText(env,config.site,row)});
 }
 if(path==='/bridge/ha/append'){
  if(typeof d.entry!=='string'||!Number.isInteger(d.expected)||d.expected<0)return reply({error:'Invalid journal entry'},400);
  try{journalAdmission(env,d.entry);}catch(error){if(error.status===413)await recordJournalRejection(env,config.site,new TextEncoder().encode(d.entry).length);return reply({error:error.message},error.status??500);}
  const entry=JSON.parse(d.entry),hash=await hexHash(d.entry);
  if(entry.seq!==d.expected+1||typeof entry.prev!=='string'||!Array.isArray(entry.changes))return reply({error:'Invalid journal position'},400);
  const conflict=()=>reply({error:'Shared head advanced. Retry the same request ID.'},409);
  // Resolve a lost ACK or already-stale proposal before reserving/uploading.
  // The CAS below remains authoritative if the head advances after this read.
  const existing=await db.prepare('SELECT seq,hash FROM controller_journal WHERE site=? AND seq=?').bind(config.site,entry.seq).first();
  if(existing)return existing.hash===hash?reply(existing):conflict();
  const head=await journalHead(db,config.site);
  if(head.seq!==d.expected||head.hash!==entry.prev)return conflict();
  const stored=await storedJournal(env,config.site,d.entry,hash);
  await db.prepare("INSERT OR IGNORE INTO controller_journal(site,seq,hash,entry) SELECT ?,?,?,? WHERE COALESCE((SELECT MAX(seq) FROM controller_journal WHERE site=?),0)=? AND COALESCE((SELECT hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1),'')=?")
   .bind(config.site,entry.seq,hash,stored,config.site,d.expected,config.site,entry.prev).run();
  const row=await db.prepare('SELECT seq,hash,entry FROM controller_journal WHERE site=? AND seq=?').bind(config.site,entry.seq).first();
  if(row?.entry!==stored)await expireUncommittedJournalUpload(env,config.site,stored,hash);
  return row?.hash===hash?reply({seq:row.seq,hash:row.hash}):conflict();
 }
 if(path==='/bridge/ha/events/seed'){
  if(!legacy||config.enabled||!Array.isArray(d.body))return reply({error:'Offline commissioning only'},409);
  const sealed=JSON.stringify(await sealGrokJob(env,'controller-events',d.body));
  const row=await db.prepare('UPDATE controller_events SET body=? WHERE id=1 AND body IS NULL RETURNING version').bind(sealed).first();
  return reply({seeded:!!row},row?200:409);
 }
 if(path==='/bridge/ha/events/claim'){
  const status=await controllerStatus(db,env);
  if(status.primary!==node.id)return reply({acquired:false});
  const row=await db.prepare('UPDATE controller_events SET owner=?,fence=fence+1,until=? WHERE id=1 AND until<? RETURNING fence,body,version').bind(node.id,now+30,now).first();
  if(!row)return reply({acquired:false});
  return reply({...row,acquired:true,body:row.body?await openGrokJob(env,'controller-events',JSON.parse(row.body)):null});
 }
 if(path==='/bridge/ha/events/renew'){
  const row=await db.prepare('UPDATE controller_events SET until=? WHERE id=1 AND owner=? AND fence=? AND until>? RETURNING fence').bind(now+30,node.id,d.fence,now).first();
  return reply({renewed:!!row},row?200:409);
 }
 if(path==='/bridge/ha/events/commit'){
  if(!Array.isArray(d.body))return reply({error:'Expected event rows'},400);
  const sealed=JSON.stringify(await sealGrokJob(env,'controller-events',d.body));
  const row=await db.prepare('UPDATE controller_events SET body=?,version=version+1,until=0 WHERE id=1 AND owner=? AND fence=? AND until>? RETURNING version').bind(sealed,node.id,d.fence,now).first();
  return reply(row??{error:'Event ownership expired'},row?200:409);
 }
 return reply({error:'Unknown HA operation'},404);
}

export async function claimControllerJob(db,node,env={}) {
 if(env.CONTROLLER_COORDINATION_MODE==='local'){
  if(!node)return {idle:true};
  // Local controllers own independent databases. No primary/overflow decision
  // is needed, and admission must inspect live capacity atomically with claim.
  const now=Date.now()/1000,lease=crypto.randomUUID();
  const job=await db.prepare(`UPDATE bridge_jobs SET state='claimed',lease=?,lease_until=?,updated=?,controller=?
   WHERE id=(SELECT j.id FROM bridge_jobs j
    JOIN controller_nodes own ON own.id=? JOIN controller_config c ON c.id=1
    LEFT JOIN controller_nodes prior ON prior.id=j.controller
    WHERE c.enabled=1 AND own.enabled=1 AND own.ready=1 AND own.seen>=?-c.heartbeat_seconds
    AND (SELECT COUNT(*) FROM bridge_jobs used WHERE used.controller=own.id AND used.state='claimed' AND used.lease_until>?)<own.capacity
    AND j.deadline>=? AND (j.state='queued' OR (j.state='claimed' AND (j.lease_until<? OR prior.seen<?-c.heartbeat_seconds OR prior.ready=0)))
    AND json_extract(j.request,'$.arguments.controller_owner')=own.id
    AND j.updated<=? ORDER BY j.updated,j.id LIMIT 1) RETURNING id,request`)
   .bind(lease,now+35,now,node.id,node.id,now,now,now,now,now,now).first();
  return job?{...job,lease,primary:node.id}:{idle:true,primary:node.id};
 }
 const state=await controllerRoutingState(db,env),now=Date.now()/1000;
 if(env.CONTROLLER_COORDINATION_MODE==='local'){
  if(!state.enabled||!node)return {idle:true};
  state.primary=node.id;
 }
 if(!state.enabled)return {legacy:true};
 if(!node||!node.enabled||!node.ready||node.seen<now-state.heartbeat_seconds)return {idle:true,primary:state.primary};
 const primary=state.nodes.find(n=>n.id===state.primary);
 const used=await db.prepare("SELECT COUNT(*) AS n FROM bridge_jobs WHERE controller=? AND state='claimed' AND lease_until>?").bind(node.id,now).first();
 if(used.n>=node.capacity)return {idle:true,primary:state.primary};
 const preferred=env.CONTROLLER_COORDINATION_MODE==='local'?node.id:state.nodes[0]?.id;
 const primaryBusy=primary?.id===node.id?used:primary?await db.prepare("SELECT COUNT(*) AS n FROM bridge_jobs WHERE controller=? AND state='claimed' AND lease_until>?").bind(primary.id,now).first():{n:0};
 const overflow=node.id!==state.primary;
 const cutoff=overflow&&primaryBusy.n<primary.capacity&&primary.load<1?now-state.overflow_seconds:now;
 const lease=crypto.randomUUID();
 const job=await db.prepare(`UPDATE bridge_jobs SET state='claimed',lease=?,lease_until=?,updated=?,controller=?
  WHERE id=(SELECT j.id FROM bridge_jobs j LEFT JOIN controller_nodes n ON n.id=j.controller
  WHERE j.deadline>=? AND (j.state='queued' OR (j.state='claimed' AND (j.lease_until<? OR n.seen<? OR n.ready=0)))
  AND (json_extract(j.request,'$.arguments.controller_owner') IS NULL OR json_extract(j.request,'$.arguments.controller_owner')=?)
  AND (?!='local' OR json_extract(j.request,'$.arguments.controller_owner')=?)
  AND j.updated<=? AND (COALESCE(json_extract(j.request,'$.arguments.realm'),'site')!='local' OR ?=?)
  AND (json_extract(j.request,'$.name') NOT IN ('sealed_grok_receiver','mcp_event_change') OR ?=?)
  ORDER BY j.updated,j.id LIMIT 1) RETURNING id,request`)
  .bind(lease,now+35,now,node.id,now,now,now-state.heartbeat_seconds,node.id,env.CONTROLLER_COORDINATION_MODE??'legacy',node.id,cutoff,node.id,preferred,node.id,state.primary).first();
 return job?{...job,lease,primary:state.primary}:{idle:true,primary:state.primary};
}

// Bounded authenticated transport only; receivers verify complete signed packets.
export async function relaySessionPacket(env,node,d){
 try{
  if(!d||typeof d!=='object'||Object.keys(d).some(k=>!['session','to','transfer','index','total','bytes','chunk'].includes(k))||typeof d.session!=='string'||!/^[A-Za-z0-9_.:-]{1,128}$/.test(d.session)||typeof d.transfer!=='string'||!/^[a-f0-9]{64}$/.test(d.transfer)||!Number.isSafeInteger(d.bytes)||d.bytes<1||d.bytes>67108864||!Number.isInteger(d.total)||d.total!==Math.ceil(d.bytes/49152)||!Number.isInteger(d.index)||d.index<0||d.index>=d.total||typeof d.chunk!=='string'||d.chunk.length>65536||!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(d.chunk))return reply({error:'Invalid bounded packet chunk'},400);
  let raw;try{raw=atob(d.chunk)}catch{return reply({error:'Invalid packet encoding'},400)}
  if(btoa(raw)!==d.chunk||raw.length!==Math.min(49152,d.bytes-d.index*49152))return reply({error:'Packet chunk size mismatch'},400);
  let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{return reply({error:'Invalid controller mapping'},503)}
  const sources=Object.entries(mapping??{}).filter(([identity,transport])=>/^[a-f0-9]{64}$/.test(identity)&&transport===node.id);
  if(sources.length!==1)return reply({error:'Sender requires one configured application identity'},409);
  await resolveControllerOwner(env,d.to);
  const from=sources[0][0],args={...d,from,controller:d.to,chunk_digest:await hexHash(d.chunk)};
  const key='packet-'+await hexHash(JSON.stringify([from,d.to,d.session,d.transfer,d.index]));
  const result=await enqueue(env,'biome_session_packet',args,key);
  if(result.isError){
   const rejection=result.structuredContent?.error;
   if(rejection&&[400,401,403,404,409,410,413,422].includes(rejection.status))return reply({error:typeof rejection.message==='string'?rejection.message.slice(0,4096):'Packet rejected by recipient',retryable:false},rejection.status);
   return reply({error:result.content?.[0]?.text??'Packet delivery unresolved',retryable:true},503);
  }
  let receipt;try{receipt=result.structuredContent??JSON.parse(result.content[0].text)}catch{return reply({error:'Invalid packet receipt',retryable:true},503)}
  if(!receipt||receipt.accepted!==true||receipt.transfer!==d.transfer||receipt.index!==d.index||typeof receipt.complete!=='boolean'||JSON.stringify(receipt).length>65536)return reply({error:'Packet receipt does not match transfer',retryable:true},503);
  return reply({result:receipt});
 }catch(error){if(error.status)return reply({error:error.message},error.status);throw error;}
}
