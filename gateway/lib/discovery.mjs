import {controllerRuntimeConfig} from './controller-runtime.mjs';
import {runTool,toolError,routedControllerCall,DEFAULT_DISCOVERY_LIMIT} from './bridge.mjs';
import {masterDiscovery} from './capability-map.mjs';
import {controllerNetworkSchema,controllerNetworkRegistration} from './controller-network.mjs';

const sections=['operations','contracts','offers','guide','controllers'];

export async function controllerCatalog(env){
 if(env.CONTROLLER_COORDINATION_MODE!=='local')throw Error('Controller catalog requires local coordination mode');
 let mapping;try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{throw Error('Invalid controller routing configuration')}
 if(!mapping||typeof mapping!=='object'||Array.isArray(mapping)||Object.keys(mapping).length>256)throw Error('Invalid controller routing configuration');
 const entries=Object.entries(mapping),owners=new Set();
 for(const [id,owner] of entries){
  if(!/^[a-f0-9]{64}$/.test(id)||typeof owner!=='string'||! /^[a-zA-Z0-9_.-]{1,128}$/.test(owner)||owners.has(owner))throw Error('Ambiguous or invalid controller routing configuration');
  owners.add(owner);
 }
 await controllerNetworkSchema(env.DB);
 const config=await env.DB.prepare('SELECT enabled,heartbeat_seconds FROM controller_config WHERE id=1').first();
 const nodes=(await env.DB.prepare('SELECT id,enabled,seen,ready,network FROM controller_nodes WHERE enabled=1').all()).results;
 const now=Date.now()/1000;
 return entries.sort(([a],[b])=>a.localeCompare(b)).flatMap(([controller,label])=>{
  const node=nodes.find(n=>n.id===label);if(!node)return [];
  return [{controller,label,default:label===env.LOCAL_CONTROLLER_ID,ready:!!(config?.enabled&&node.ready&&Number.isFinite(config.heartbeat_seconds)&&node.seen>now-config.heartbeat_seconds),
   last_seen:node.seen,network:controllerNetworkRegistration(env,node,config?.heartbeat_seconds,now,!!config?.enabled),
   next_call:routedControllerCall(controller,{operation:'system.discovery',arguments:{section:'offers'},request_id:'discovery-'+crypto.randomUUID()})}];
 });
}

// The controller owns discovery pagination. Its default response is already a
// page, not the former full snapshot; never decode it as snapshot.capabilities.
export async function discoverPage(env,args={},realm='site'){
 if(!args || typeof args!=='object' || Array.isArray(args) ||
    Object.keys(args).some(k=>!['cursor','section','controller','limit'].includes(k)) ||
    (args.section!==undefined && !sections.includes(args.section)) ||
    (args.cursor!==undefined && typeof args.cursor!=='string') ||
    !['site','local'].includes(realm))return toolError('Invalid discovery arguments.');
 if(args.limit!==undefined&&(!Number.isSafeInteger(args.limit)||args.limit<1))return toolError('Discovery limit must be a positive integer.');
 if(args.cursor?.startsWith('discovery-'))return toolError('Legacy discovery snapshot expired. Start again without cursor.');
 if(args.section==='controllers'||args.cursor?.startsWith('controllers1.')){
  try{
   if(args.controller!==undefined||args.section&&args.section!=='controllers')throw Error('Invalid controller catalog selection.');
   let after='',limit=DEFAULT_DISCOVERY_LIMIT;
   if(args.cursor!==undefined){
    if(!args.cursor.startsWith('controllers1.'))throw Error('Invalid controller catalog cursor.');
    const saved=JSON.parse(atob(args.cursor.slice('controllers1.'.length)));
    if(!/^[a-f0-9]{64}$/.test(saved.after)||!Number.isSafeInteger(saved.limit)||saved.limit<1)throw Error('Invalid controller catalog cursor.');
    after=saved.after;limit=saved.limit;
   }
   limit=args.limit??limit;
   const remaining=(await controllerCatalog(env)).filter(row=>row.controller>after);
   const controllers=remaining.slice(0,limit),complete=controllers.length===remaining.length;
   const page={section:'controllers',controllers,complete};
   if(!complete){
    page.next_cursor='controllers1.'+btoa(JSON.stringify({after:controllers[controllers.length-1].controller,limit}));
    page.next_call={operation:'discovery.page',arguments:{cursor:page.next_cursor},realm,request_id:'discovery-'+crypto.randomUUID()};
   }
   return {content:[{type:'text',text:JSON.stringify(page)}]};
  }
  catch(error){return toolError(error.message)}
 }
 let managed;try{managed=realm==='site'?await controllerRuntimeConfig(env):null}catch(error){return toolError(error.message)}
 if(!managed&&args.controller===undefined&&env.CONTROLLER_COORDINATION_MODE==='local'&&(['contracts','offers'].includes(args.section)||args.cursor?.startsWith('master1.')))return masterDiscovery(env,args,realm);
 const {controller,...inputs}=args;
 const result=await runTool(env,'biome_invoke',{
  operation:'system.discovery',arguments:inputs,realm,...(controller===undefined?{}:{controller}),
  request_id:'discovery-'+crypto.randomUUID()
 });
 let identity=controller;
 if(!identity&&env.CONTROLLER_COORDINATION_MODE==='local'){
  let mapping={};try{mapping=JSON.parse(env.CONTROLLER_NODE_MAP??'{}')}catch{}
  const matches=Object.entries(mapping).filter(([,owner])=>owner===env.LOCAL_CONTROLLER_ID);if(matches.length===1)identity=matches[0][0];
 }
 if(identity&&!result.isError&&result.content?.[0]?.type==='text'){
  try{const page=JSON.parse(result.content[0].text);page.controller=identity;if(env.CONTROLLER_COORDINATION_MODE==='local')page.controller_catalog={tool:'biome_discover',arguments:{section:'controllers'}};if(page.next_call)page.next_call.controller=identity;result.content[0]={...result.content[0],text:JSON.stringify(page)};}catch{}
 }
 return result;
}
