import {consoleMetrics} from './console-metrics.mjs';
// Site UI adapter. Uses existing owner/OAuth authorization and existing read paths;
// no runtime operations, data stores, grants or permissions are changed.
import {ownerPrincipal,privateHeaders} from './access.mjs';
import {bearerPrincipal} from './oauth.mjs';
import {dashboard} from './dashboard.mjs';
import {dashboardDefinition} from './dashboard-data.mjs';
import {discoverPage} from './discovery.mjs';
import {runTool} from './bridge.mjs';
const reply=(data,status=200)=>Response.json(data,{status,headers:privateHeaders});
const reads={info:'system.info',progress:'system.progress',leases:'cap.leases',streams:'stream.list',processes:'process.list',decisions:'decision.list',session:'session.state',cluster:'cluster.status',work:'work.list',knowledge:'knowledge.search',pairing:'pair.list',destinations:'destination.list',inbox:'bus.inbox',waits:'process.waits'};
const details={execution:['cap.status','operation'],process:['process.get','run'],resource:['cap.resource.status','resource'],stream:['stream.inspect','session'],decision:['decision.get','id'],document:['knowledge.get','key']};
function value(result){if(result.isError)throw Error(result.content?.find(c=>c.type==='text')?.text||'Controller read failed');return result.structuredContent??JSON.parse(result.content.find(c=>c.type==='text').text);}
export async function consoleRead(req,env,deps={ownerPrincipal,bearerPrincipal,dashboard,dashboardDefinition,discoverPage,runTool}){
 if(req.method!=='POST')return reply({error:'POST required'},405);
 const origin=new URL(req.url).origin;
 // Browser requests must remain same-origin. The enterprise server sends its
 // existing OAuth bearer without forwarding browser cookies or identity headers.
 if(['cross-site','same-site'].includes(req.headers.get('Sec-Fetch-Site'))||req.headers.get('Origin')&&req.headers.get('Origin')!==origin)return reply({error:'Same-origin request required'},403);
 const owner=await deps.bearerPrincipal(req,env)||await deps.ownerPrincipal(req,env);
 if(!owner)return reply({error:'Sign in or reconnect BiomeRTE to load live data.',connect:true},401);
 if(req.headers.get('content-type')?.split(';')[0]!=='application/json')return reply({error:'JSON required'},415);
 let b;try{const text=await req.text();if(text.length>16384)throw Error();b=JSON.parse(text);if(!b||Array.isArray(b)||typeof b!=='object'||Object.keys(b).some(k=>!['view','realm','controller','cursor','id','name','revision','request_id','topic','after'].includes(k)))throw Error();if(!['site','local'].includes(b.realm??'site')||b.controller&&!/^[a-f0-9]{64}$/.test(b.controller)||b.request_id&&(typeof b.request_id!=='string'||b.request_id.length>128))throw Error();}catch{return reply({error:'Invalid console read'},400);}
 if(b.view==='metrics'){const metricStarted=Date.now();return reply({data:await consoleMetrics(env,{owner,realm:b.realm||'site',controller:b.controller}),source:'host.terminal.run:1 · fixed read-only observation',observed_at:new Date().toISOString(),realm:b.realm||'site',duration_ms:Date.now()-metricStarted});}
 const realm=b.realm||'site',started=Date.now();let data,source;
 const internal=(path,body)=>new Request(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json'},body:JSON.stringify(body)});
 const scoped={ownerPrincipal:async()=>owner};
 if(['streams','leases'].includes(b.view)&&!b.controller){
  const controllers=value(await deps.discoverPage(env,{section:'controllers'})).controllers||[];
  const results=await Promise.all(controllers.map(async c=>{try{const records=value(await deps.runTool(env,'biome_invoke',{controller:c.controller,operation:b.view==='streams'?'stream.list':'cap.leases',arguments:{},realm,request_id:crypto.randomUUID()}));return {controller:c.controller,sessions:(Array.isArray(records)?records:records.sessions||records.streams||records.leases||records.items||[]).map(s=>({...s,controller:c.controller,controller_label:c.label}))};}catch(error){return {controller:c.controller,error:error.message,sessions:[]};}}));
  return reply({data:{[b.view==='streams'?'sessions':'leases']:results.flatMap(r=>r.sessions),coverage:results.map(({controller,error})=>({controller,error:error||null}))},source:(b.view==='streams'?'stream.list':'cap.leases')+' · registered controllers',observed_at:new Date().toISOString(),duration_ms:Date.now()-started,realm});
 }else if(['definition','summaries'].includes(b.view)){
  const args=b.view==='summaries'?{mode:'summaries',realm}:{name:b.name,revision:b.revision||'',realm};
  const response=await deps.dashboardDefinition(internal('/dashboard/definition',args),env,scoped),record=await response.json();if(!response.ok)return reply(record,response.status);data=record.result;source=b.view==='summaries'?'controller.run_summaries':'controller.process_definition';
 }else if(['controllers','contracts','offers','operations','guide'].includes(b.view)){
  if(b.cursor!==undefined&&(typeof b.cursor!=='string'||b.cursor.length>8192))return reply({error:'Invalid cursor'},400);
  data=value(await deps.discoverPage(env,{section:b.view,limit:25,...(b.cursor?{cursor:b.cursor}:{}),...(b.controller?{controller:b.controller}:{})}));source='discovery.'+b.view;
 }else{
  let operation=reads[b.view],args={};
  if(details[b.view]){if(typeof b.id!=='string'||!b.id||b.id.length>512)return reply({error:'An exact record ID is required'},400);const [op,key]=details[b.view];operation=op;args={[key]:b.id};}
  if(b.view==='health')operation='controller.diagnostics';
  if(b.view==='events'){if(typeof(b.topic??'')!=='string'||(b.topic??'').length>256||b.after!==undefined&&(!Number.isInteger(b.after)||b.after<0))return reply({error:'Invalid event query'},400);operation='bus.event_page';args={topic:b.topic||'workflow.delegation.requested',after:b.after||0,limit:25};}
  if(!operation)return reply({error:'This read is not available'},400);
  let resynchronizedFrom=null;
  for(;;){try{
   if(b.view==='health'||b.controller){data=value(await deps.runTool(env,'biome_invoke',{operation,arguments:args,realm,request_id:resynchronizedFrom===null?(b.request_id||crypto.randomUUID()):(b.request_id||crypto.randomUUID()).slice(0,118)+'-retained',...(b.controller&&b.view!=='health'?{controller:b.controller}:{})}));}
   else{const response=await deps.dashboard(internal('/dashboard/api',{id:(resynchronizedFrom===null?(b.request_id||crypto.randomUUID()):(b.request_id||crypto.randomUUID()).slice(0,118)+'-retained'),operation,arguments:args,realm,cached:false}),env,scoped),record=await response.json();if(!response.ok){const floor=operation==='bus.event_page'&&resynchronizedFrom===null&&String(record.error||'').match(/event floor (\d+)/);if(floor){resynchronizedFrom=Number(floor[1]);args={...args,after:resynchronizedFrom};continue;}return reply(record,response.status);}data=record.result;}
   break;
  }catch(error){const floor=operation==='bus.event_page'&&resynchronizedFrom===null&&error.message.match(/event floor (\d+)/);if(!floor)throw error;resynchronizedFrom=Number(floor[1]);args={...args,after:resynchronizedFrom};}}
  if(resynchronizedFrom!==null)data={...data,resynchronized_from:resynchronizedFrom,history_before_unavailable:true};
  source=operation;
 }
 return reply({data,source,observed_at:new Date().toISOString(),duration_ms:Date.now()-started,realm,controller:b.controller||null});
}
