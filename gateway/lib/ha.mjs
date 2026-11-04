import {reply,limitedJSON,hexHash} from './bridge.mjs';
import {sealGrokJob,openGrokJob} from './grok.mjs';

export async function controllerStatus(db) {
 const config=await db.prepare('SELECT * FROM controller_config WHERE id=1').first();
 if(!config)return {enabled:false};
 const nodes=(await db.prepare('SELECT id,priority,capacity,enabled,seen,ready,position,load,host FROM controller_nodes ORDER BY priority,id').all()).results;
 const now=Date.now()/1000;
 const live=nodes.filter(n=>n.enabled&&n.ready&&n.seen>now-config.heartbeat_seconds);
 const head=await journalHead(db,config.site);
 return {...config,enabled:!!config.enabled,primary:live[0]?.id??null,nodes,head};
}
async function journalHead(db,site) {
 return await db.prepare('SELECT seq,hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1').bind(site).first()??{seq:0,hash:''};
}
export async function controllerDevice(req,env,path,node,legacy) {
 const db=env.DB,now=Date.now()/1000;
 const d=await limitedJSON(req,1500000);
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
  return reply(await controllerStatus(db));
 }
 if(!node)return reply({error:'Registered controller required'},403);
 const config=await db.prepare('SELECT * FROM controller_config WHERE id=1').first();
 if(!config)return reply({error:'Cluster not configured'},409);
 if(path==='/bridge/ha/heartbeat'){
  if(!Number.isInteger(d.position)||d.position<0||typeof d.ready!=='boolean'||!Number.isFinite(d.load)||typeof d.host!=='string')return reply({error:'Invalid heartbeat'},400);
  await db.prepare('UPDATE controller_nodes SET seen=?,ready=?,position=?,load=?,host=? WHERE id=?').bind(now,Number(d.ready),d.position,d.load,d.host.slice(0,160),node.id).run();
  return reply(await controllerStatus(db));
 }
 if(path==='/bridge/ha/status')return reply(await controllerStatus(db));
 if(path==='/bridge/ha/read'){
  if(d.key==='head'){const head=await journalHead(db,config.site);return reply({value:head.seq?head:null,version:String(head.seq)});}
  if(!/^log\/\d{20}$/.test(d.key))return reply({error:'Invalid journal key'},400);
  const row=await db.prepare('SELECT entry FROM controller_journal WHERE site=? AND seq=?').bind(config.site,Number(d.key.slice(4))).first();
  return reply({encoded:row?.entry??null});
 }
 if(path==='/bridge/ha/append'){
  if(typeof d.entry!=='string'||d.entry.length>900000||!Number.isInteger(d.expected)||d.expected<0)return reply({error:'Invalid journal entry'},400);
  const entry=JSON.parse(d.entry),hash=await hexHash(d.entry);
  if(entry.seq!==d.expected+1||typeof entry.prev!=='string'||!Array.isArray(entry.changes))return reply({error:'Invalid journal position'},400);
  await db.prepare("INSERT OR IGNORE INTO controller_journal(site,seq,hash,entry) SELECT ?,?,?,? WHERE COALESCE((SELECT MAX(seq) FROM controller_journal WHERE site=?),0)=? AND COALESCE((SELECT hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1),'')=?")
   .bind(config.site,entry.seq,hash,d.entry,config.site,d.expected,config.site,entry.prev).run();
  const row=await db.prepare('SELECT seq,hash FROM controller_journal WHERE site=? AND seq=?').bind(config.site,entry.seq).first();
  return row?.hash===hash?reply(row):reply({error:'Shared head advanced. Retry the same request ID.'},409);
 }
 if(path==='/bridge/ha/events/seed'){
  if(!legacy||config.enabled||!Array.isArray(d.body))return reply({error:'Offline commissioning only'},409);
  const sealed=JSON.stringify(await sealGrokJob(env,'controller-events',d.body));
  const row=await db.prepare('UPDATE controller_events SET body=? WHERE id=1 AND body IS NULL RETURNING version').bind(sealed).first();
  return reply({seeded:!!row},row?200:409);
 }
 if(path==='/bridge/ha/events/claim'){
  const status=await controllerStatus(db);
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

export async function claimControllerJob(db,node) {
 const state=await controllerStatus(db),now=Date.now()/1000;
 if(!state.enabled)return {legacy:true};
 if(!node||!node.enabled||!node.ready||node.seen<now-state.heartbeat_seconds)return {idle:true,primary:state.primary};
 const primary=state.nodes.find(n=>n.id===state.primary);
 const used=await db.prepare("SELECT COUNT(*) AS n FROM bridge_jobs WHERE controller=? AND state='claimed' AND lease_until>?").bind(node.id,now).first();
 if(used.n>=node.capacity)return {idle:true,primary:state.primary};
 const preferred=state.nodes[0]?.id;
 const primaryBusy=primary?await db.prepare("SELECT COUNT(*) AS n FROM bridge_jobs WHERE controller=? AND state='claimed' AND lease_until>?").bind(primary.id,now).first():{n:0};
 const overflow=node.id!==state.primary;
 const cutoff=overflow&&primaryBusy.n<primary.capacity&&primary.load<1?now-state.overflow_seconds:now;
 const lease=crypto.randomUUID();
 const job=await db.prepare(`UPDATE bridge_jobs SET state='claimed',lease=?,lease_until=?,updated=?,controller=?
  WHERE id=(SELECT j.id FROM bridge_jobs j LEFT JOIN controller_nodes n ON n.id=j.controller
  WHERE j.deadline>=? AND (j.state='queued' OR (j.state='claimed' AND (j.lease_until<? OR n.seen<? OR n.ready=0)))
  AND j.updated<=? AND (COALESCE(json_extract(j.request,'$.arguments.realm'),'site')!='local' OR ?=?)
  AND (json_extract(j.request,'$.name') NOT IN ('sealed_grok_receiver','mcp_event_change') OR ?=?)
  ORDER BY j.updated,j.id LIMIT 1) RETURNING id,request`)
  .bind(lease,now+35,now,node.id,now,now,now-state.heartbeat_seconds,cutoff,node.id,preferred,node.id,state.primary).first();
 return job?{...job,lease,primary:state.primary}:{idle:true,primary:state.primary};
}
