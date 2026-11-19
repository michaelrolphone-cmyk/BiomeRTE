// Current provider placement and expiring dispatch locators; never a journal.
const initialized=new WeakSet();
const hash = v => typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
const object = v => v&&typeof v==='object'&&!Array.isArray(v);
const fail=(message,status=409)=>Object.assign(Error(message),{status});
const host=v=>v.toLowerCase().replace(/\.$/,'').replace(/\.local$/,'');
export function validateProviderContext(c){
 if(!object(c)||Object.keys(c).some(k=>!['provider','host','addresses'].includes(k))||!hash(c.provider)||typeof c.host!=='string'||!/^[A-Za-z0-9_.-]{1,160}$/.test(c.host)||c.addresses!==undefined&&(!Array.isArray(c.addresses)||c.addresses.length>16||c.addresses.some(v=>typeof v!=='string'||v.length>80)))throw fail('Invalid provider topology',400);
 return c;
}
export function nearestController(nodes,topology,now,heartbeat){
 const ready=nodes.filter(n=>n.enabled&&n.ready&&n.seen>now-heartbeat);
 if(!ready.length)throw fail('No available controller for provider registration',503);
 const distance=n=>host(n.host??'')===host(topology.host)?0:2;
 ready.sort((a,b)=>distance(a)-distance(b)||(a.load??0)-(b.load??0)||(a.priority??0)-(b.priority??0)||a.id.localeCompare(b.id));
 return {node:ready[0],distance:distance(ready[0]),basis:distance(ready[0])===0?'same_host':'available_controller_unknown_distance'};
}
export async function providerTopologySchema(db){
 if(initialized.has(db))return;
 for(const sql of [
 'CREATE TABLE IF NOT EXISTS provider_placements(site TEXT,provider TEXT,controller TEXT,host TEXT,addresses TEXT,epoch INTEGER NOT NULL,updated REAL,PRIMARY KEY(site,provider))',
 'CREATE TABLE IF NOT EXISTS provider_capabilities(site TEXT,provider TEXT,contract TEXT,resource TEXT,body TEXT,PRIMARY KEY(site,provider,contract,resource))',
 'CREATE INDEX IF NOT EXISTS provider_capability_resource ON provider_capabilities(site,resource,contract)',
 'CREATE TABLE IF NOT EXISTS provider_dispatch_routes(site TEXT,kind TEXT,id TEXT,controller TEXT,expires REAL,PRIMARY KEY(site,kind,id))',
 'CREATE INDEX IF NOT EXISTS provider_dispatch_expiry ON provider_dispatch_routes(expires)'
 ])await db.prepare(sql).run();
 initialized.add(db);
}
async function inventory(env){
 const config=await env.DB.prepare('SELECT site,heartbeat_seconds FROM controller_config WHERE id=1').first();
 if(!config)throw fail('Controller site is not configured',503);
 let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{throw fail('Invalid controller mapping',503)}
 const nodes=(await env.DB.prepare('SELECT id,enabled,ready,seen,host,load,priority FROM controller_nodes').all()).results;
 return {...config,mapping,nodes};
}
// Dispatch custody predates and survives mutable provider attachment. A locator
// is only a transport hint; the selected native owner still checks every grant.
export async function dispatchController(env,operation,args){
 if(env.CONTROLLER_COORDINATION_MODE!=='local'||['cap.offer','provider.poll'].includes(operation))return;
 await providerTopologySchema(env.DB);
 const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
 if(!config)throw fail('Controller site is not configured',503);
 for(const [kind,id] of [['operation',args?.operation],['lease',args?.lease]]){
  if(typeof id!=='string'||!id)continue;
  const row=await env.DB.prepare('SELECT controller FROM provider_dispatch_routes WHERE site=? AND kind=? AND id=? AND expires>?').bind(config.site,kind,id,Date.now()/1000).first();
  if(row)return row.controller;
 }
}
export async function routeProviderInvocation(env,args,bootstrap,retainedContext=false){
 if(env.CONTROLLER_COORDINATION_MODE!=='local')return {args};
 const context=args.provider_context;
 if(context!==undefined)validateProviderContext(context);
 if(context&&args.arguments.provider!==undefined&&args.arguments.provider!==context.provider)throw fail('Provider context identity mismatch',403);
 if(!context&&!args.operation.startsWith('cap.')&&!args.operation.startsWith('provider.')&&args.operation!=='agent.progress')return {args};
 await providerTopologySchema(env.DB);const db=env.DB,config=await inventory(env),now=Date.now()/1000;
 await db.prepare('DELETE FROM provider_dispatch_routes WHERE rowid IN (SELECT rowid FROM provider_dispatch_routes WHERE expires<? LIMIT 128)').bind(now).run();
 // Fresh registration data changes locators, never controller/epoch or a
 // pinned dispatch owner. Retained fallback context is not a new observation.
 const refreshContext=async()=>{if(context&&!retainedContext)await db.prepare('UPDATE provider_placements SET host=?,addresses=?,updated=? WHERE site=? AND provider=? AND (updated IS NULL OR updated<=?)')
  .bind(context.host,JSON.stringify(context.addresses??[]),now,config.site,context.provider,now).run();};
 const dispatched=await dispatchController(env,args.operation,args.arguments);
 if(dispatched){
  if(args.controller!==undefined&&args.controller!==dispatched)throw fail('Command belongs to a different dispatch controller');
  await refreshContext();
  const routed={...args,controller:dispatched};delete routed.provider_context;
  return {args:routed,site:config.site};
 }
 if(args.controller!==undefined&&!context)return {args,site:config.site};
 if(context){
  if(args.controller!==undefined)throw fail('Provider placement is chosen by the gateway',400);
  if(args.arguments.provider!==undefined&&args.arguments.provider!==context.provider)throw fail('Provider context identity mismatch',403);
  await refreshContext();
  let current=await db.prepare('SELECT * FROM provider_placements WHERE site=? AND provider=?').bind(config.site,context.provider).first();
  const alive=current&&config.nodes.some(n=>config.mapping[current.controller]===n.id&&n.enabled&&n.ready&&n.seen>now-config.heartbeat_seconds);
  let selected,basis='existing_attachment';
  if(args.operation==='cap.offer'||!alive){
   const near=nearestController(config.nodes,context,now,config.heartbeat_seconds);
   selected=Object.keys(config.mapping).find(id=>config.mapping[id]===near.node.id);basis=near.basis;
   if(!selected)throw fail('Available controller has no application identity',503);
   if(!current||current.controller!==selected){
    const previous=current,epoch=(current?.epoch??0)+1;
    await db.prepare('INSERT INTO provider_placements VALUES(?,?,?,?,?,?,?) ON CONFLICT(site,provider) DO UPDATE SET controller=excluded.controller,host=excluded.host,addresses=excluded.addresses,epoch=excluded.epoch,updated=excluded.updated').bind(config.site,context.provider,selected,context.host,JSON.stringify(context.addresses??[]),epoch,now).run();
    current={controller:selected,epoch};
    // Re-advertise the current manifest after an unavailable controller. This
    // copies registration only, never claims, effects, grants or old results.
    if(previous&&args.operation!=='cap.offer'&&bootstrap){
     const offers=(await db.prepare('SELECT body FROM provider_capabilities WHERE site=? AND provider=? ORDER BY contract,resource').bind(config.site,context.provider).all()).results;
     for(let i=0;i<offers.length;i++)await bootstrap(selected,JSON.parse(offers[i].body),'provider-reattach-'+context.provider+'-'+epoch+'-'+i);
    }
   }else selected=current.controller;
  }else selected=current.controller;
  const routed={...args,controller:selected};delete routed.provider_context;
  return {args:routed,placement:{controller:selected,provider:context.provider,epoch:current.epoch,basis},site:config.site};
 }
 const a=args.arguments??{};let selected;
 // Existing pollers already carry their provider identity. Once registered,
 // they follow the gateway's assignment without selecting a host themselves.
 if(hash(a.provider)){
  const known=await db.prepare('SELECT host,addresses FROM provider_placements WHERE site=? AND provider=?').bind(config.site,a.provider).first();
  if(known)return routeProviderInvocation(env,{...args,provider_context:{provider:a.provider,host:known.host,addresses:JSON.parse(known.addresses)}},bootstrap,true);
 }
 if(a.resource&&args.operation.startsWith('cap.')){
  const rows=(await db.prepare('SELECT DISTINCT p.controller FROM provider_capabilities c JOIN provider_placements p ON p.site=c.site AND p.provider=c.provider WHERE c.site=? AND c.resource=?').bind(config.site,a.resource).all()).results;
  if(rows.length===1)selected=rows[0].controller;
  else if(rows.length>1)throw fail('Resource has conflicting provider controllers');
 }
 if(!selected){
  const kind=a.lease?'lease':a.operation?'operation':null,id=a.lease??a.operation;
  if(kind)selected=(await db.prepare('SELECT controller FROM provider_dispatch_routes WHERE site=? AND kind=? AND id=? AND expires>?').bind(config.site,kind,id,now).first())?.controller;
 }
 return {args:selected?{...args,controller:selected}:args,site:config.site};
}
export async function rememberProviderInvocation(env,original,routed,result){
 if(env.CONTROLLER_COORDINATION_MODE!=='local'||result.isError)return;
 const controller=routed.args.controller;if(!controller||!routed.site)return;
 const db=env.DB,site=routed.site??(await inventory(env)).site;
 let value=result.structuredContent;
 if(value==null){try{value=JSON.parse(result.content.find(c=>c.type==='text').text)}catch{return}}
 if(original.operation==='cap.offer'&&routed.placement){
  const a=original.arguments;
  await db.prepare('INSERT INTO provider_capabilities VALUES(?,?,?,?,?) ON CONFLICT(site,provider,contract,resource) DO UPDATE SET body=excluded.body').bind(site,a.provider,a.contract,a.resource,JSON.stringify(a)).run();
 }
 const routes=[];
 if(original.operation==='cap.acquire'&&value?.id)routes.push(['lease',value.id]);
 if(['cap.invoke','cap.submit','cap.submission','cap.status'].includes(original.operation)&&original.arguments.operation&&value?.id===original.arguments.operation&&value.state!=='missing_unknown')routes.push(['operation',original.arguments.operation]);
 if(original.arguments.lease)routes.push(['lease',original.arguments.lease]);
 if(original.operation==='provider.poll'){
  for(const a of value?.actions??[]){if(a.id)routes.push(['operation',a.id]);if(a.lease)routes.push(['lease',a.lease]);}
  for(const l of value?.fences??[])if(l.id)routes.push(['lease',l.id]);
 }
 await providerTopologySchema(db);
 for(const [kind,id]of routes){
  await db.prepare('INSERT INTO provider_dispatch_routes VALUES(?,?,?,?,?) ON CONFLICT(site,kind,id) DO NOTHING').bind(site,kind,id,controller,Date.now()/1000+172800).run();
  const saved=await db.prepare('SELECT controller FROM provider_dispatch_routes WHERE site=? AND kind=? AND id=? AND expires>?').bind(site,kind,id,Date.now()/1000).first();
  if(saved?.controller!==controller)throw fail('Conflicting dispatch controller');
 }
}
