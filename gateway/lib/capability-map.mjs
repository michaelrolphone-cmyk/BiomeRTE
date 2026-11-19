import {runTool,toolError,hexHash,DEFAULT_DISCOVERY_LIMIT} from './bridge.mjs';
import {controllerCatalog} from './discovery.mjs';
import {providerTopologySchema} from './provider-topology.mjs';

const mapResult=v=>({content:[{type:'text',text:JSON.stringify(v)}]});
const mapValue=r=>r.structuredContent??JSON.parse(r.content.find(c=>c.type==='text').text);
const mapHost=v=>typeof v==='string'?v.toLowerCase().replace(/\.$/,''):'';
const mapCanonical=v=>Array.isArray(v)?v.map(mapCanonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,mapCanonical(v[k])])):v;
const mapFingerprint=c=>JSON.stringify(mapCanonical({id:c.id,inputs:c.inputs,outputs:c.outputs,synchronous:c.synchronous}));
const mapFault=message=>Object.assign(Error(message),{status:409});

export function mergeCapabilityCatalog(sources,owners=[]){
 const contracts=new Map(),offers=new Map(),conflicts=[];
 const authority=new Map(owners.map(p=>[p.provider,p.controller]));
 for(const source of sources){
  if(!source.catalog)continue;
  for(const c of source.catalog.contracts??[]){
   const prior=contracts.get(c.id);
   if(prior&&mapFingerprint(prior)!==mapFingerprint(c)){if(!conflicts.includes(c.id))conflicts.push(c.id);continue;}
   if(!prior)contracts.set(c.id,{...c,implementations:[],controllers:[]});
   const current=contracts.get(c.id);if(!current.controllers.includes(source.controller))current.controllers.push(source.controller);
  }
  for(const o of source.catalog.offers??[]){
   if(authority.has(o.provider)&&authority.get(o.provider)!==source.controller)continue;
   const key=o.id,prior=offers.get(key);
   if(prior){
    const controllers=prior.controllers.includes(source.controller)?prior.controllers:[...prior.controllers,source.controller];
    // Unmigrated providers historically belong to the configured default.
    // A current server-assigned provider owner always takes precedence.
    if(!authority.has(o.provider)&&source.default)offers.set(key,{...o,controller:source.controller,controllers});
    else prior.controllers=controllers;
    continue;
   }
   offers.set(key,{...o,controller:source.controller,controllers:[source.controller]});
  }
 }
 for(const o of offers.values()){
  const c=contracts.get(o.contract);if(!c)continue;
  const p=owners.find(p=>p.provider===o.provider);
  c.implementations.push({offer:o.id,provider:o.provider,resource:o.resource,
   host:o.configuration?.execution_host??o.configuration?.host??p?.host??'',
   controller:o.controller,controllers:o.controllers,state:o.state,capacity:o.capacity,
   stream:o.configuration?.stream??null});
 }
 for(const c of contracts.values()){
  c.implementations.sort((a,b)=>a.provider.localeCompare(b.provider)||a.resource.localeCompare(b.resource));
  c.conflict=conflicts.includes(c.id);
  c.placement={default:'Omit placement and resource for automatic eligible implementation selection.',
   constraints:'Optional per-request placement {provider,host,resource}; all supplied constraints must match.',
   affinity:'A particular workspace, storage volume or attached device requires resource affinity.'};
 }
 return {contracts:[...contracts.values()].sort((a,b)=>a.id.localeCompare(b.id)),
  offers:[...offers.values()].sort((a,b)=>a.id.localeCompare(b.id)),conflicts};
}

export async function federatedCatalog(env,snapshot=crypto.randomUUID(),contract='',read=runTool,realm='site'){
 const controllers=await controllerCatalog(env);
 await providerTopologySchema(env.DB);
 const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
 const owners=(await env.DB.prepare('SELECT provider,controller,host FROM provider_placements WHERE site=?').bind(config.site).all()).results;
 const sources=await Promise.all(controllers.map(async c=>{
  if(!c.ready)return {...c,error:'Controller unavailable'};
  try{
   const result=await read(env,'biome_invoke',{controller:c.controller,operation:'cap.discover',
    arguments:contract?{contract}:{},request_id:'master-'+snapshot+'-'+c.controller,realm});
   if(result.isError)return {...c,error:result.content?.find(v=>v.type==='text')?.text??'Discovery failed'};
   const catalog=mapValue(result);
   if(!Array.isArray(catalog.contracts)||!Array.isArray(catalog.offers))throw Error('Invalid native catalog');
   return {...c,catalog};
  }catch(error){return {...c,error:error.message}}
 }));
 const merged=mergeCapabilityCatalog(sources,owners);
 return {...merged,scope:'system',snapshot,site:config.site,
  coverage:{complete:sources.every(s=>!s.error)&&!merged.conflicts.length,
   controllers:sources.map(({controller,label,ready,error})=>({controller,label,ready,...(error?{error}:{})}))}};
}

export async function masterDiscovery(env,args={},realm='site',read=runTool){
 let state;
 try{
  if(args.cursor){
   if(typeof args.cursor!=='string'||!args.cursor.startsWith('master1.'))throw Error('Invalid master catalog cursor');
   state=JSON.parse(atob(args.cursor.slice(8)));
   if(typeof state.snapshot!=='string'||!/^[a-f0-9-]{36}$/.test(state.snapshot)||
      !['contracts','offers'].includes(state.section)||!Number.isSafeInteger(state.offset)||state.offset<0||
      args.section&&args.section!==state.section)throw Error('Invalid master catalog cursor');
  }else state={snapshot:crypto.randomUUID(),section:args.section??'contracts',offset:0};
  const limit=args.limit??state.limit??DEFAULT_DISCOVERY_LIMIT;
  if(!Number.isSafeInteger(limit)||limit<1)throw Error('Discovery limit must be a positive integer.');
  const catalog=await federatedCatalog(env,state.snapshot,'',read,realm);
  const rows=catalog[state.section];
  if(state.offset>rows.length)throw Error('Master catalog cursor no longer exists');
  const items=rows.slice(state.offset,state.offset+limit),offset=state.offset+items.length;
  const next=offset<rows.length?'master1.'+btoa(JSON.stringify({...state,offset,limit})):null;
  const page={scope:'system',section:state.section,items,section_total:rows.length,
   complete:!next&&catalog.coverage.complete,coverage:catalog.coverage,conflicts:catalog.conflicts,
   consistency:'Per-controller retained read snapshots; unavailable sources remain explicit.',next_cursor:next};
  if(next)page.next_call={operation:'discovery.page',arguments:{cursor:next},realm,request_id:'master-page-'+state.snapshot+'-'+offset};
  return mapResult(page);
 }catch(error){return toolError(error.message)}
}

export async function routeCapabilityInvocation(env,args,read=runTool){
 if(env.CONTROLLER_COORDINATION_MODE!=='local'||args.controller!==undefined||args.provider_context!==undefined||
    !['cap.acquire','cap.submit','cap.prepare','cap.run_async'].includes(args.operation)||typeof args.arguments?.contract!=='string')return args;
 await providerTopologySchema(env.DB);
 const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
 const requestKey=await hexHash((args.realm??'site')+'\n'+args.request_id);
 await env.DB.prepare('CREATE TABLE IF NOT EXISTS capability_request_bindings(site TEXT,id TEXT,digest TEXT,selection TEXT,expires REAL,PRIMARY KEY(site,id))').run();
 const fingerprint=await hexHash(JSON.stringify(mapCanonical({operation:args.operation,arguments:args.arguments,realm:args.realm??'site'})));
 const bound=await env.DB.prepare('SELECT digest,selection,expires FROM capability_request_bindings WHERE site=? AND id=?').bind(config.site,requestKey).first();
 const resolved=selection=>({...args,controller:selection.controller,arguments:{...args.arguments,resource:selection.resource,placement:{...(args.arguments.placement??{}),provider:selection.provider}}});
 if(bound){
  if(bound.digest!==fingerprint)throw mapFault('request_id already belongs to a different capability request');
  if(bound.expires>Date.now()/1000)return resolved(JSON.parse(bound.selection));
 }
 const catalog=await federatedCatalog(env,requestKey.slice(0,32),args.arguments.contract,read,args.realm??'site');
 if(catalog.conflicts.includes(args.arguments.contract))throw mapFault('Capability contract has conflicting definitions across controllers');
 const p=args.arguments.placement??{},a=args.arguments;
 if(!p||typeof p!=='object'||Array.isArray(p)||Object.keys(p).some(k=>!['provider','host','resource'].includes(k))||
    Object.values(p).some(v=>typeof v!=='string'||!v))throw mapFault('Invalid request placement');
 if(a.resource&&p.resource&&a.resource!==p.resource)throw mapFault('Conflicting placement resource');
 const resource=a.resource||p.resource;
 const c=catalog.contracts.find(c=>c.id===a.contract);
 const matches=(c?.implementations??[]).filter(o=>o.state==='ready'&&(!resource||o.resource===resource)&&
  (!p.provider||o.provider===p.provider)&&(!p.host||mapHost(o.host)===mapHost(p.host)));
 // Native completion evidence takes precedence over mutable catalog placement.
 // A lost gateway locator must never cause an existing command to be replayed elsewhere.
 if(args.operation==='cap.submit'){
  const observed=await Promise.all(catalog.coverage.controllers.map(async owner=>{
   if(!owner.ready)return {error:'Controller unavailable'};
   try{const r=await read(env,'biome_invoke',{controller:owner.controller,operation:'cap.status',
    arguments:{operation:a.operation,recovery:true},realm:args.realm??'site',request_id:'locate-'+requestKey.slice(0,32)+'-'+owner.controller});
    if(r.isError)return {error:'Cannot establish previous execution ownership'};
    const value=mapValue(r);return value.state==='missing_unknown'?{}:{controller:owner.controller,value,result:r};
   }catch{return {error:'Cannot establish previous execution ownership'}}
  }));
  const existing=observed.filter(v=>v.controller);
  if(existing.length>1)throw mapFault('Conflicting command custody requires reconciliation');
  if(existing.length===1){
   const old=existing[0],v=old.value;
   if(v.contract!==a.contract||JSON.stringify(mapCanonical(v.inputs))!==JSON.stringify(mapCanonical(a.inputs))||(v.destination??'')!==(a.destination??''))
    throw mapFault('Operation identity conflict');
   if(resource&&resource!==v.resource||p.provider&&p.provider!==v.provider)throw mapFault('Existing operation has a different execution target');
   const target=catalog.contracts.find(c=>c.id===v.contract)?.implementations.find(o=>o.provider===v.provider&&o.resource===v.resource);
   if(p.host&&mapHost(target?.host??v.result?.evidence?.host)!==mapHost(p.host))throw mapFault('Cannot establish matching host for existing operation');
   return {...args,controller:old.controller,_capability_result:old.result};
  }
  if(observed.some(v=>v.error))throw mapFault('Cannot rule out an earlier command on an unavailable controller; reconcile before dispatch');
 }
 if(!matches.length)throw mapFault('No matching capability implementation is currently available; no request was dispatched');
 const owners=[...new Set(matches.map(m=>m.controller))];
 const capacity=await Promise.all(owners.map(async controller=>{
  try{const r=await read(env,'biome_invoke',{controller,operation:'cap.leases',arguments:{},realm:args.realm??'site',request_id:'capacity-'+requestKey.slice(0,32)+'-'+controller});
   const value=r.isError?null:mapValue(r);return {controller,leases:Array.isArray(value)?value:null};
  }catch{return {controller,leases:null}}
 }));
 const available=matches.filter(o=>{
  if(o.stream?.enabled||!o.capacity)return true;
  const leases=capacity.find(c=>c.controller===o.controller)?.leases;
  return !leases||leases.filter(l=>l.resource===o.resource&&['bound','fencing'].includes(l.state)&&!l.shared_parent).length<o.capacity;
 });
 const options=available.length?available:matches;
 const defaults=new Set(catalog.coverage.controllers.filter(c=>c.label===env.LOCAL_CONTROLLER_ID).map(c=>c.controller));
 options.sort((a,b)=>Number(defaults.has(b.controller))-Number(defaults.has(a.controller))||a.controller.localeCompare(b.controller)||a.provider.localeCompare(b.provider)||a.resource.localeCompare(b.resource));
 const target=options[0],selection={controller:target.controller,resource:target.resource,provider:target.provider};
 // Keep only current, expiring resolution state. This is not execution history.
 // Native custody is consulted before a command can be dispatched after a lost locator.
 await env.DB.prepare('DELETE FROM capability_request_bindings WHERE rowid IN (SELECT rowid FROM capability_request_bindings WHERE expires<? LIMIT 128)').bind(Date.now()/1000).run();
 await env.DB.prepare('INSERT INTO capability_request_bindings VALUES(?,?,?,?,?) ON CONFLICT(site,id) DO NOTHING')
  .bind(config.site,requestKey,fingerprint,JSON.stringify(selection),Date.now()/1000+172800).run();
 const saved=await env.DB.prepare('SELECT digest,selection FROM capability_request_bindings WHERE site=? AND id=?').bind(config.site,requestKey).first();
 if(!saved||saved.digest!==fingerprint)throw mapFault('Conflicting capability request identity');
 return resolved(JSON.parse(saved.selection));
}
