import {escapeHTML as esc,shortID,friendlyIdentity,mappingFor,nameFor,mappingKey,namespaceTree,namespaceAt,descendantCount,statusName,statusTone,runTitle,overviewStats,resourcePools,graphModel,elapsedLabel,createClient,workHierarchy,workParents,workDependencyGraph} from './model.mjs';
import {diagramMarkup,wireDiagram,diagramToolbar} from './diagram.mjs';
import {graphModel as toGraph,nodeStates as reportedNodeStates} from './model.mjs';
import {diagramMarkup as drawGraph,wireDiagram as connectDiagram,diagramToolbar as flowToolbar} from './diagram.mjs';

if(new URLSearchParams(location.search).get('console')==='enterprise'){
document.body.classList.add('enterprise-console');
document.body.innerHTML="<aside><div class=\"brand\"><div class=\"enterprise-inline-0\"><img src=\"/dashboard/logo.png\" alt=\"Biome\" width=\"38\" height=\"38\" class=\"enterprise-inline-1\">BiomeRTE</div><small>ENTERPRISE CONSOLE</small></div><div class=\"instance\"><b id=\"connection-label\">Checking connection</b><span class=\"dot\" id=\"connection-dot\"></span>Existing controller</div><nav aria-label=\"Console\"><div class=\"navlabel\">LIVE BIOMERTE</div><a href=\"/dashboard/?console=enterprise#overview\">▦ Overview</a><a href=\"/dashboard/?console=enterprise#workflows\">⑂ Workflows & BPMN</a><a href=\"/dashboard/?console=enterprise#processes\">▷ Process runs</a><a href=\"/dashboard/?console=enterprise#decisions\">◇ Human decisions</a><a href=\"/dashboard/?console=enterprise#work\">✓ Work plan</a><a href=\"/dashboard/?console=enterprise#knowledge\">▤ Knowledge</a><a href=\"/dashboard/?console=enterprise#capabilities\">⬡ Capabilities</a><a href=\"/dashboard/?console=enterprise#resources\">▥ Resources & capacity</a><a href=\"/dashboard/?console=enterprise#storage\">▤ Storage & volumes</a><a href=\"/dashboard/?console=enterprise#streams\">⇄ Streams & bandwidth</a><a href=\"/dashboard/?console=enterprise#access\">♙ Access & connections</a><a href=\"/dashboard/?console=enterprise#activity\">≡ Audit & activity</a><a href=\"/dashboard/?console=enterprise#settings\">⚙ Instance settings</a><details class=\"nav-more\"><summary>Controller diagnostics</summary><a href=\"/dashboard/?console=enterprise#controllers\">◉ Controllers</a><a href=\"/dashboard/?console=enterprise#leases\">⇄ Leases & queue</a><a href=\"/dashboard/?console=enterprise#executions\">▷ Executions</a><a href=\"/dashboard/?console=enterprise#interface\">⌘ Implemented interface</a></details><div class=\"navlabel\">DESIGN · NOT IMPLEMENTED</div><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Overview\">Overview design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Workflows\">Workflow library & BPMN design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Runs\">Run tracking design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Decisions\">Decision review design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Capabilities\">Capability catalog design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Resources\">Resource pool design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Access\">Enterprise access & roles<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Audit\">Audit trail design<small>Design preview</small></a><a href=\"https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Settings\">Operating policy settings<small>Design preview</small></a></nav><div class=\"profile\"><div class=\"avatar\">B</div><div>Instance owner<small>Controller connection</small></div></div></aside><main><header class=\"top\"><div>BiomeRTE <span class=\"enterprise-inline-2\">/</span> <b id=\"crumb\">Overview</b></div><div class=\"topright\"><input aria-label=\"Search workflows and runs\" class=\"search\" id=\"search\" placeholder=\"Search workflows, runs…\"><a href=\"/signin-with-chatgpt?return_to=%2Fdashboard%2F%3Fconsole%3Denterprise\" target=\"_top\" id=\"connect\">Connect BiomeRTE ↗</a><span>◷</span></div></header><div class=\"content\"><div class=\"eyebrow\">INSTANCE MANAGEMENT</div><div class=\"heading\"><div><h1 id=\"title\">Operations overview</h1><p id=\"subtitle\">Your processes, people, and capabilities. Working together.</p></div><div class=\"row\"><button class=\"primary\" id=\"refresh\">↻ Refresh</button></div></div><div id=\"connect-panel\" class=\"connect-panel\" hidden><div><h2>Connect your BiomeRTE controller</h2><p>Read the existing controller's live records and inspect their details.</p></div><a class=\"button primary\" href=\"/signin-with-chatgpt?return_to=%2Fdashboard%2F%3Fconsole%3Denterprise\" target=\"_top\">Connect BiomeRTE ↗</a></div><div class=\"toolbar\"><label>Controller<select id=\"controller\"><option value=\"\">Default controller</option></select></label><label>Store<select id=\"realm\"><option value=\"site\">Shared state</option><option value=\"local\">Local durable state</option></select></label><label class=\"auto\"><input type=\"checkbox\" id=\"auto\">Refresh every 15s</label><button id=\"disconnect\" hidden>Disconnect console</button></div><div id=\"status\" role=\"status\" aria-live=\"polite\"></div><div id=\"view\"></div><footer class=\"notice\"><span>BiomeRTE · Live source records</span><span id=\"last-update\">No live read yet</span></footer></div></main><div id=\"modal\"></div><div id=\"toast\" class=\"toast hidden\" role=\"status\"></div>\n<dialog id=\"detail\"><div class=\"dialog-top\"><span class=\"eyebrow\">CONTROLLER RECORD</span><button id=\"close-detail\" aria-label=\"Close details\">✕</button></div><div id=\"detail-body\"></div></dialog>";
// UI transport for the existing owner-authenticated controller dashboard.
// Existing controller owner session; discovery uses the Site's authenticated UI adapter.
const READS={info:'system.info',progress:'system.progress',leases:'cap.leases',streams:'stream.list',processes:'process.list',decisions:'decision.list',session:'session.state',cluster:'cluster.status',work:'work.list',knowledge:'knowledge.search',pairing:'pair.list',destinations:'destination.list',inbox:'bus.inbox',waits:'process.waits'};
const DETAILS={execution:['cap.status','operation'],process:['process.get','run'],resource:['cap.resource.status','resource'],stream:['stream.inspect','session'],decision:['decision.get','id'],document:['knowledge.get','key']};
const pendingReads=new Map();
function existingRead(body){
 if(READS[body.view])return {operation:READS[body.view],arguments:{}};
 if(DETAILS[body.view]){const [operation,key]=DETAILS[body.view];return {operation,arguments:{[key]:body.id}};}
 if(body.view==='events')return {operation:'bus.event_page',arguments:{topic:body.topic||'workflow.delegation.requested',after:body.after||0,limit:25}};
 throw Error('Unknown console read');
}
async function readJSON(response){
 const text=await response.text();let data;
 try{data=JSON.parse(text);}catch{const message=[401,403].includes(response.status)?'Your controller session has expired or is not authorized. Reconnect BiomeRTE to continue.':response.ok?'The controller returned an unreadable response. Retry this read.':'Controller read failed ('+response.status+'). Retry this read.';throw Object.assign(Error(message),{status:response.status||502});}
 if(!response.ok)throw Object.assign(Error(typeof data.error==='string'?data.error:data.error?.message||'Controller read failed ('+response.status+')'),{status:response.status});
 return data;
}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function ownerRead(body,fetcher=fetch){
 const realm=body.realm||'site',started=Date.now();let value,source;
 if(['definition','summaries'].includes(body.view)){
  const args=body.view==='summaries'?{mode:'summaries',realm}:{name:body.name,revision:body.revision||'',realm};
  const response=await fetcher('/dashboard/definition',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)});
  const data=await readJSON(response);if(!response.ok)throw Object.assign(Error(data.error||'Workflow definition read failed'),{status:response.status});value=data.result;source=body.view==='summaries'?'controller.run_summaries':'controller.process_definition';
 }else if(['contracts','offers','operations','controllers','guide','health','metrics','streams','leases','events'].includes(body.view)||body.controller){
  const response=await fetcher('/api/console/read',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return await readJSON(response);
 }else{
  const read=existingRead(body),identity=JSON.stringify({read,realm}),id=pendingReads.get(identity)||crypto.randomUUID();pendingReads.set(identity,id);
  // A timeout is an unfinished read, not a reason to create a different request.
  for(;;){
   const response=await fetcher('/dashboard/api',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,...read,realm,cached:false})});
   if(response.status===504){await response.text();await sleep(1000);continue;}
   const data=await readJSON(response);
   if(!response.ok)throw Object.assign(Error(data.error||'Controller read failed'),{status:response.status});
   pendingReads.delete(identity);value=data.result;source=read.operation;break;
  }
 }
 return {data:value,source,observed_at:new Date().toISOString(),duration_ms:Date.now()-started,realm,controller:body.controller||null};
}
async function consoleFetch(path,options={}){
 if(!location.pathname.startsWith('/dashboard')){
  if(path!=='/api/read')return fetch(path,options);
  const body=JSON.parse(options.body),identity=JSON.stringify(body),id=pendingReads.get(identity)||crypto.randomUUID();pendingReads.set(identity,id);
  for(;;){const response=await fetch(path,{...options,body:JSON.stringify({...body,request_id:id})});if(response.status===504){await response.text();await sleep(1000);continue;}pendingReads.delete(identity);return response;}
 }
 if(path==='/api/connection')return fetch('/api/console/connection',{credentials:'same-origin'});
 if(path==='/disconnect'){location.assign('/signout-with-chatgpt?return_to=%2Fdashboard%2F%3Fconsole%3Denterprise');return Response.json({disconnected:true});}
 if(path!=='/api/read')throw Error('Unsupported console request');
 try{return Response.json(await ownerRead(JSON.parse(options.body)));}catch(e){return Response.json({error:e.message},{status:e.status||502});}
}



const html=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function processPresentation(record,run=null){
 const graph=toGraph(record),states=reportedNodeStates(run);
 const active=(run?.tokens||[]).map(t=>t.node||t.at||t.node_id).filter(Boolean);
 return {graph,states,active,revision:run?.rev||record.id||record.rev,status:run?.state||'Published',steps:run?.steps??null};
}
function mountProcess(container,record,run,{onBack,onRefresh}={}){
 const model=processPresentation(record,run),name=run?.name||record.name||'Workflow',active=model.active.map(id=>model.graph.nodes.find(n=>n.id===id)?.name||id);
 const drawn=drawGraph(model.graph,{run,labelFor:n=>n.name||n.label||n.title||n.id});
 container.innerHTML=`<button class="link" data-back-process>← Back to ${run?'process runs':'workflow library'}</button><div class="flow-summary"><div><span>State</span><strong>${html(model.status)}</strong></div><div><span>Current activity</span><strong>${html(active.join(' · ')||(run?run.state==='completed'?'Process complete':'No active token reported':'Published definition'))}</strong></div><div><span>Recorded steps</span><strong>${html(model.steps??'—')}</strong></div><div><span>Pinned revision</span><strong class="mono">${html(model.revision)}</strong></div></div><section class="panel" data-live-diagram><div class="panelhead"><h2>${run?'Process execution':'Workflow diagram'}</h2><button data-refresh-process>↻ Refresh ${run?'progress':'definition'}</button></div>${flowToolbar.replace('<button data-fullscreen>','<button data-locate-active>Locate active node</button><button data-fullscreen>')}<div class="diagram-viewport">${drawn.html}</div><div class="nodeinspect" data-step-details><div><span class="eyebrow">${run?'CURRENT ACTIVITY':'PROCESS DEFINITION'}</span><b>${html(active.join(' · ')||name)}</b><p>Select a node to inspect its configuration and reported execution state.</p></div><span class="pill">${html(model.status)}</span></div></section><section class="panel flowmeta"><div><h3>EXECUTION</h3><p>${run?html(run.id):'Published definition'}<br>${html(model.status)}</p></div><div><h3>PROCESS CONTEXT</h3><p>${run?'Task: '+html(run.task||'Not linked'):'Inputs: '+html(Object.keys(record.graph?.inputs||{}).join(', ')||'None')}</p><p>Revision ${html(model.revision)}</p></div><div><h3>OBSERVATION</h3><p>Card colours identify step types. Status outlines and markers follow retained trace events and current execution tokens; missing history is never inferred.</p></div></section>${run?`<section class="panel"><div class="panelhead"><h2>Execution history</h2></div><div class="panel-body">${run.trace?.length?run.trace.map(t=>`<div class="metric-row"><b>${html(t.name||t.node||t.node_id||t.step||'Event')}</b><span>${html(t.state||t.status||'Recorded')}</span></div>`).join(''):'<p>Detailed step history was not retained for this run. Its current state and tokens are shown above.</p>'}</div></section>`:''}`;
 container.querySelector('[data-back-process]').onclick=onBack;
 container.querySelector('[data-refresh-process]').onclick=onRefresh;
 const diagram=container.querySelector('[data-live-diagram]');
 function selectNode(id){const n=model.graph.nodes.find(x=>x.id===id);if(!n)return;const state=model.states[id]||'No execution state retained';container.querySelector('[data-step-details]').innerHTML=`<div><span class="eyebrow">SELECTED NODE</span><b>${html(n.name||n.label||id)}</b><p>${html(n.type)} · ${html(state)}</p><p>${html(n.contract||n.operation||n.process||'')}</p><details><summary>Node configuration</summary><pre>${html(JSON.stringify(n,null,2))}</pre></details></div><span class="pill">${html(state)}</span>`;diagram.querySelectorAll('[data-node]').forEach(el=>el.classList.toggle('selected',el.dataset.node===id));}
 connectDiagram(diagram,drawn.layout,selectNode);
 const locate=diagram.querySelector('[data-locate-active]');locate.disabled=!model.active.length;locate.onclick=()=>{const id=model.active[0],node=[...diagram.querySelectorAll('[data-node]')].find(n=>n.dataset.node===id);node?.scrollIntoView({behavior:'smooth',block:'center',inline:'center'});selectNode(id);};
}

const safe=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function formatBytes(value){if(value===null||value===undefined||!Number.isFinite(Number(value)))return 'Not reported';let n=Number(value),i=0;while(Math.abs(n)>=1024&&i<5){n/=1024;i++;}return n.toLocaleString(undefined,{maximumFractionDigits:i?1:0})+' '+['B','KiB','MiB','GiB','TiB','PiB'][i];}
function capacityPools(offers,leases=[]){
 const pools=new Map();for(const o of offers){const key=[o.controller,o.provider,o.resource].join(':');if(!pools.has(key))pools.set(key,{key,id:o.resource,provider:o.provider,controller:o.controller,host:o.host||o.configuration?.host||'',capacity:null,contracts:new Set(),states:new Set(),offers:[]});const p=pools.get(key);if(Number.isFinite(o.capacity))p.capacity=p.capacity===null?o.capacity:Math.max(p.capacity,o.capacity);p.contracts.add(o.contract);p.states.add(o.state);p.offers.push(o);}
 return [...pools.values()].map(p=>{const active=leases.filter(l=>l.resource===p.id&&(!l.provider||l.provider===p.provider)&&['bound','fencing','active'].includes(l.state));return {...p,used:new Set(active.map((l,i)=>l.guard||l.reservation||l.id||'unidentified-'+i)).size,activeLeases:active.length,queued:leases.filter(l=>(l.resource||l.requested_resource)===p.id&&l.state==='queued').length};});
}
function usagePercent(used,total){return Number.isFinite(used)&&Number.isFinite(total)&&total>0?Math.max(0,Math.min(100,used/total*100)):null;}
const meter=(value,label)=>value===null?'<div class="meter-unknown">Measurement not reported</div>':'<progress max="100" value="'+value.toFixed(2)+'" aria-label="'+safe(label)+'"></progress>';
const when=t=>t?new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'Awaiting observation';
function hostCards(snapshot){const hosts=snapshot?.hosts||[];return '<div class="infrastructure-grid">'+(hosts.map(h=>{const m=h.metrics;return '<article class="hardware-card"><div class="hardware-title"><span class="device-glyph">▣</span><div><span class="eyebrow">COMPUTE HOST</span><h3>'+safe(m?.host||snapshot?.coverage?.controllers?.find(c=>c.controller===h.controller)?.label||h.resource)+'</h3></div><span class="pill '+(m?'good':'warning')+'">'+(h.refreshing?('Observation '+safe(h.state)):m?'Observed':safe(h.state))+'</span></div>'+(m?'<div class="hardware-metrics"><div><span>System memory</span><strong>'+formatBytes(m.memory_total_bytes)+'</strong></div><div><span>Processor cores</span><strong>'+safe(m.cpu_count??'—')+'</strong></div><div><span>Load · 1 minute</span><strong>'+safe(m.load_average?.[0]?.toFixed(2)??'—')+'</strong></div></div>'+(m.memory_available_bytes!==undefined?'<div class="usage-label"><span>Memory in use</span><b>'+formatBytes(m.memory_total_bytes-m.memory_available_bytes)+'</b></div>'+meter(usagePercent(m.memory_total_bytes-m.memory_available_bytes,m.memory_total_bytes),'Memory in use'):'')+'<p class="measurement-note">'+m.volumes.length+' attached filesystem volumes · Observed '+when(m.observed_at)+'</p>':'<p class="measurement-note">'+safe(h.error||'Reading mounted volumes, memory, processors and network counters through the existing terminal capability.')+'</p>')+(h.error&&m?'<p class="measurement-note warning-text">'+safe(h.error)+' · Showing previous observation</p>':'')+'</article>';}).join('')||'<div class="empty"><h3>Host observations</h3><p>Mounted volumes and network measurements appear after the authenticated read completes.</p></div>')+'</div>';}
function volumeCards(snapshot){return '<div class="volume-grid">'+(snapshot?.hosts||[]).flatMap(h=>(h.metrics?.volumes||[]).map(v=>{const pct=usagePercent(v.used_bytes,v.total_bytes),name=v.mount==='/'?'System volume':v.mount.split('/').filter(Boolean).pop()||v.device;return '<article class="volume-card"><div class="hardware-title"><span class="device-glyph storage-glyph">▤</span><div><span class="eyebrow">'+safe(h.metrics.host)+'</span><h3>'+safe(name)+'</h3></div></div><div class="volume-available"><strong>'+formatBytes(v.available_bytes)+'</strong><span>available</span></div>'+meter(pct,'Filesystem space in use')+'<div class="usage-label"><span>'+formatBytes(v.used_bytes)+' used</span><span>'+formatBytes(v.total_bytes)+' total</span></div><p class="volume-path">'+safe(v.mount)+'</p><p class="measurement-note">'+safe(v.device)+' · Observed '+when(h.metrics.observed_at)+'</p></article>';})).join('')+'</div><p class="measurement-note">Filesystem volumes are listed individually. Shared APFS/container space is not summed across volumes.</p>';}
function networkCards(snapshot){return '<div class="infrastructure-grid">'+(snapshot?.hosts||[]).flatMap(h=>(h.metrics?.network||[]).filter(n=>n.received_bytes||n.sent_bytes||n.rx_bytes_per_second||n.tx_bytes_per_second).map(n=>{const scale=Math.max(n.rx_bytes_per_second||0,n.tx_bytes_per_second||0,1);const bar=(value,name)=>value===null||value===undefined?'<div class="meter-unknown">Rate not reported</div>':'<progress class="'+name+'" max="'+scale+'" value="'+value+'" aria-label="'+(name==='network-rx'?'Received':'Sent')+' bytes per second"></progress>';return '<article class="network-card"><div class="hardware-title"><span class="device-glyph network-glyph">⇄</span><div><span class="eyebrow">'+safe(h.metrics.host)+'</span><h3>'+safe(n.interface)+'</h3></div></div><div class="transfer-row"><span class="transfer-direction">↓ Received</span><strong>'+formatBytes(n.rx_bytes_per_second)+(n.rx_bytes_per_second===null?'':'/s')+'</strong></div>'+bar(n.rx_bytes_per_second,'network-rx')+'<div class="transfer-row"><span class="transfer-direction">↑ Sent</span><strong>'+formatBytes(n.tx_bytes_per_second)+(n.tx_bytes_per_second===null?'':'/s')+'</strong></div>'+bar(n.tx_bytes_per_second,'network-tx')+'<p class="measurement-note">Measured over '+(h.metrics.sample_seconds||0).toFixed(1)+' s · Host interface traffic</p><div class="usage-label"><span>'+formatBytes(n.received_bytes)+' received total</span><span>'+formatBytes(n.sent_bytes)+' sent total</span></div><p class="measurement-note">Both bars share the larger rate in this sample as their scale.</p></article>';})).join('')+'</div>';}
function streamCards(sessions,offers=[],inspect=null){return '<div class="stream-grid">'+sessions.map(s=>{const offer=offers.find(o=>o.resource===s.resource&&o.contract===s.contract),rate=s.metrics?.bytes_per_second??s.bytes_per_second??null;return '<article class="stream-card"><div class="stream-title"><div><span class="eyebrow">'+safe(s.contract)+'</span><h3>'+safe(s.resource||s.id)+'</h3></div><span class="pill '+(s.state==='open'?'good':'info')+'">'+safe(s.state)+'</span></div><div class="stream-route"><div><span class="route-icon">▣</span><b>'+safe(offer?.host||offer?.configuration?.host||'Provider')+'</b><small>'+safe((s.provider||'').slice(0,12))+'</small></div><div class="route-connector"><span>'+ (s.direction==='bidirectional'?'↔':'→') +'</span></div><div><span class="route-icon">⬡</span><b>'+safe(s.controller_label||'Controller')+'</b><small>'+safe(s.direction?.replaceAll('_',' ')||'Direction not reported')+'</small></div></div><div class="stream-lanes">'+(s.lanes||[]).map(l=>'<span>'+safe(l)+'</span>').join('')+'</div><div class="stream-measures"><div><span>Measured bandwidth</span><b>'+ (rate===null?'Not reported':formatBytes(rate)+'/s') +'</b></div><div><span>Message rate limit</span><b>'+safe(s.limits?.messages_per_second??'Not reported')+(s.limits?.messages_per_second?' /s':'')+'</b></div><div><span>Message size limit</span><b>'+formatBytes(s.limits?.max_message_bytes)+'</b></div></div><p class="measurement-note">'+safe(s.delivery||'Delivery policy not reported')+' · '+safe(s.id)+'</p>'+(inspect?'<div class="row">'+inspect(s)+'</div>':'')+'</article>';}).join('')+'</div>';}




const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={overview:'Operations overview',workflows:'Workflow library & BPMN',controllers:'Controllers',capabilities:'Capabilities',resources:'Resources & capacity',leases:'Leases & queue',executions:'Executions',streams:'Streams & bandwidth',storage:'Storage & volumes',processes:'Process runs',decisions:'Human decisions',work:'Work plan',knowledge:'Knowledge',access:'Access & connections',activity:'Audit & activity',settings:'Instance settings',interface:'Implemented interface'};
const subtitles={storage:'Mounted volumes, available space and installed memory, measured on your connected hosts.',overview:'What is connected, what is available, and what currently needs attention.',controllers:'Controller reachability and advertised host state, read directly from the gateway.',capabilities:'Discover implemented contracts, their inputs and outputs, and the providers offering them.',resources:'Physical and logical resources grouped by their advertised identity.',leases:'Reservations reported by the selected controller and store.',executions:'Current outstanding commands and agent activity. Pending is not proof of progress.',streams:'Actual stream routes, declared delivery limits and measured host network traffic.',processes:'Existing process runs, with the state and history the controller retains.',decisions:'Existing decision records. Responses remain in the authenticated decision application.',interface:'The operations and caller guidance currently exposed by BiomeRTE.'};
const state={connected:false,cache:new Map(),busy:new Set(),generation:0,namespace:'all',records:[],route:'overview',connection:null};
Object.assign(subtitles,{workflows:'Published process definitions, revision identities, inputs, outputs and execution records.',work:'Goals, milestones, workflows and tasks recorded in BiomeRTE.',knowledge:'Saved documents and their immutable revision heads.',access:'Enrolled providers, delivery destinations and authenticated application connections.',activity:'Retained controller events and current inbox messages.',settings:'Current instance identity, coordination configuration and session state.'});
const readKey=v=>['controllers','health','contracts','offers','operations','guide'].includes(v)?'global:'+v:[$('controller').value,$('realm').value,v].join(':');
const cached=v=>state.cache.get(readKey(v));
const data=v=>cached(v)?.data;
const time=t=>t?new Date(t).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'Never';
const stamp=v=>typeof v==='number'?new Date(v<1e12?v*1000:v).toLocaleString():v?new Date(v).toLocaleString():'Not reported';
const short=v=>v?String(v).length>22?String(v).slice(0,12)+'…'+String(v).slice(-6):v:'Not reported';
const list=(value,keys=[])=>Array.isArray(value)?value:keys.map(k=>value?.[k]).find(Array.isArray)||[];
const pill=s=>`<span class="pill ${['ready','bound','succeeded','completed','available','online','done'].includes(s)?'good':['queued','waiting','claimed','fencing','pending','uncertain','degraded'].includes(s)?'warning':['failed','error','offline','rejected','blocked'].includes(s)?'bad':'info'}">${esc(s??'Not reported')}</span>`;
const empty=(title,text)=>`<div class="empty"><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
const raw=v=>`<details><summary>Exact controller record</summary><pre>${esc(JSON.stringify(v,null,2))}</pre></details>`;
const kv=(k,v)=>`<div class="kv"><span>${esc(k)}</span><div>${v}</div></div>`;
const stat=(label,value,note)=>`<div class="stat"><label>${esc(label)}</label><strong>${esc(value??'—')}</strong><small>${esc(note)}</small></div>`;
const match=v=>!$('search').value||JSON.stringify(v).toLowerCase().includes($('search').value.toLowerCase());
function recordButton(label,value,kind='record',id=''){const index=state.records.push({label,value,kind,id})-1;return `<button class="link" data-record="${index}">${esc(label)}</button>`;}
function table(heads,rows,none='No records reported for this controller and store.'){return rows.length?`<div class="table-scroll"><table><thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c??'—'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:empty('No records reported',none);}
function panel(title,v,body,extra=''){
 const c=cached(v),stale=c?.error;
 return `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2>${state.busy.has(readKey(v))?pill('Loading'):stale?pill('stale'):c?.observed_at?pill('Live read'):pill('Not loaded')}</div>${stale?`<div class="error-box"><b>${c.data?'Refresh failed · showing earlier data':'Read unavailable'}</b><p>${esc(c.error)}</p></div>`:''}${!c?.data&&!state.connected?empty('Connect to load this view','No data is substituted when the controller is disconnected.'):!c?.data&&state.busy.has(readKey(v))?empty('Loading live records…','Reading '+v+' from the controller.'):body}${extra}<div class="source ${stale?'source-stale':''}"><span>${esc(c?.source||v)}${!['controllers','health','contracts','offers','operations','guide'].includes(v)?' · '+esc($('realm').value)+' · '+esc(controllerName($('controller').value)):''}</span><span>${c?.observed_at?'Observed '+time(c.observed_at)+(c.duration_ms===undefined?'':' · '+c.duration_ms+' ms'):'Awaiting live read'}</span></div></section>`;
}
function controllerName(id){return (data('controllers')?.controllers||[]).find(c=>c.controller===id)?.label||(!id?'default controller':short(id));}
function health(){return data('health')||{};}
function controllerRows(){const nodes=health().cluster?.nodes||[];return nodes.filter(match).map(n=>[recordButton(n.host||n.id,n),esc(n.id),pill(n.ready?'ready':'offline'),esc(n.capacity??'Not reported'),esc(n.load??'Not reported'),esc(stamp(n.seen))]);}
function overview(){
 const h=health(),nodes=h.cluster?.nodes,groups=h.active_queue?.groups||[],offers=data('offers'),contracts=data('contracts'),out=data('progress');
 const active=h.active_queue?.active_jobs;
 return `<div class="stats">${stat('Controllers',nodes?.filter(n=>n.ready).length,nodes?nodes.length+' advertised · ready heartbeat':'Waiting for gateway diagnostics')}${stat('Capability contracts',contracts?.section_total,contracts?'Aggregated inventory · '+list(contracts,['items']).length+' loaded':'Waiting for discovery')}${stat('Implementation offers',offers?.section_total,offers?'Registered offers, not execution proof':'Waiting for discovery')}${stat('Active gateway jobs',active,h.active_queue?'Includes worker inbox polls':'Waiting for gateway diagnostics')}</div><div class="section-intro"><h2>Infrastructure at a glance</h2><a href="#resources">View capacities →</a></div>${hostCards(data('metrics'))}<div class="section-intro"><h2>Stream activity</h2><a href="#streams">View flows & bandwidth →</a></div>${streamCards(list(data('streams'),['sessions','streams','items']),inventory(),s=>recordButton('Inspect stream',s,'stream',s.id))}<div class="section-intro"><h2>Controller operations</h2></div><div class="grid"><div>${panel('Controller hosts','health',table(['Host','Controller','Heartbeat','Capacity','Load','Last seen'],controllerRows()))}${panel('Gateway dispatch queue','health',table(['Operation','Controller','State','Count','State age'],groups.filter(match).map(g=>[esc(g.operation),esc(g.controller||'Automatic'),pill(g.state),esc(g.count),esc(g.oldest_state_age_seconds)+' s']),'The gateway reported no active dispatch groups.'),h.active_queue?`<div class="panel-body"><p class="small">${esc(h.active_queue.active_basis)}. Age measures ${esc(h.active_queue.age_basis)}; these are gateway jobs, including provider inbox polls, rather than completed application commands.</p>${h.active_queue.truncated?'<p>Partial queue summary reported by gateway.</p>':''}</div>`:'')}</div><div>${panel('Current execution activity','progress',`<div class="panel-body">${kv('State',pill(out?.state))}<p class="small">${esc(out?.meaning||'Outstanding commands from the selected controller store.')}</p>${list(out,['outstanding']).length?list(out,['outstanding']).slice(0,8).map(x=>`<div class="metric-row">${recordButton(x.operation||x.id||x.contract,x,'execution',x.operation||x.id)}${pill(x.state)}</div>`).join(''):empty('No outstanding commands reported','This does not establish that other controllers or stores are idle.')}</div>`)}${panel('Coordination mode','health',`<div class="panel-body">${kv('Cluster enabled',esc(h.cluster?.enabled===undefined?'Not reported':String(h.cluster.enabled)))}${kv('Primary controller',esc(h.cluster?.primary||'Not reported'))}${kv('Heartbeat interval',h.cluster?.heartbeat_seconds===undefined?'Not reported':esc(h.cluster.heartbeat_seconds)+' seconds')}<p class="small">Configuration and reachability are separate from successful failover or workload execution.</p></div>`)}${panel('Store identity','info',`<div class="panel-body">${Object.entries(data('info')||{}).map(([k,v])=>kv(k,esc(typeof v==='object'?JSON.stringify(v):v))).join('')}</div>`)}</div></div>`;
}
function controllers(){const d=data('controllers');return panel('Routable controllers','controllers',table(['Controller','Reachability','Default','Identity'],list(d,['controllers']).filter(match).map(c=>[recordButton(c.label||short(c.controller),c),pill(c.ready?'ready':'offline'),c.default?'Yes':'—',`<span class="mono">${esc(c.controller)}</span>`])))+panel('Advertised host health','health',table(['Host','Controller','Heartbeat','Capacity','Load','Last seen'],controllerRows()));}
function more(v){const c=data(v);return c?.next_cursor?`<div class="panel-body"><button data-more="${v}" ${state.busy.has(readKey(v))?'disabled':''}>Load next 25</button><span class="small muted"> ${list(c,['items']).length} of ${esc(c.section_total??'?')} loaded. Filters apply to loaded records.</span></div>`:'';}
function caps(){const d=data('contracts'),all=list(d,['items']),ns=[...new Set(all.map(x=>(x.name||x.id||'').split('.')[0]))].sort();return `<div class="namespace"><button data-namespace="all" class="${state.namespace==='all'?'selected':''}">All namespaces</button>${ns.map(x=>`<button data-namespace="${esc(x)}" class="${state.namespace===x?'selected':''}">${esc(x)}</button>`).join('')}</div>`+panel('Implemented capability contracts','contracts',table(['Contract','Implementations','Controllers','Availability'],all.filter(match).filter(c=>state.namespace==='all'||(c.name||c.id||'').split('.')[0]===state.namespace).map(c=>{const impl=c.implementations||[];return [recordButton(c.id||c.name,c,'capability'),`<small>${esc(c.description||'')}</small>`+esc(impl.length)+' registered',esc([...new Set(impl.map(x=>controllerName(x.controller)))].join(', '))||'Not reported',impl.length?impl.map(i=>pill(i.state)).join(' '):pill('No offer')];})),more('contracts'));}
function resourceGroups(offers){const groups=new Map();for(const o of offers){const id=o.resource||o.id;if(!groups.has(id))groups.set(id,{id,offers:[],providers:new Set(),controllers:new Set(),hosts:new Set()});const g=groups.get(id);g.offers.push(o);g.providers.add(o.provider);for(const c of o.controllers||[o.controller].filter(Boolean))g.controllers.add(c);if(o.configuration?.host||o.host)g.hosts.add(o.configuration?.host||o.host);}return [...groups.values()];}
function inventory(){return (data('metrics')?.resources||list(data('offers'),['items'])).filter(o=>!$('controller').value||o.controller===$('controller').value||(o.controllers||[]).includes($('controller').value));}
function hostSection(){return panel('Connected hosts','metrics',`<div class="panel-body">${hostCards(data('metrics'))}</div>`);}
function storageView(){return hostSection()+panel('Attached volumes','metrics',`<div class="panel-body">${volumeCards(data('metrics'))}</div>`);}
function capacityCards(){const leases=list(data('leases'),['leases','items']),pools=capacityPools(inventory(),leases).filter(match);return `<div class="infrastructure-grid">${pools.map(p=>{const reported=!!data('leases')&&!(data('leases')?.coverage||[]).some(c=>c.controller===p.controller&&c.error);return `<article class="capacity-card"><div class="hardware-title"><span class="device-glyph">⬡</span><div><span class="eyebrow">${esc(p.host||controllerName(p.controller))}</span><h3>${esc(p.id)}</h3></div></div><div class="capacity-number"><strong>${esc(p.capacity??'—')}</strong><span>advertised slots</span></div><p class="small">${p.contracts.size} contracts · ${[...p.states].map(pill).join(' ')}</p>${p.capacity&&reported?`<progress max="${p.capacity}" value="${Math.min(p.used,p.capacity)}" aria-label="Reported capacity reservations"></progress>`:''}<div class="usage-label"><span>${reported?p.used:'—'} reservations · ${reported?p.activeLeases:'—'} active leases</span><span>${reported?p.queued:'—'} queued</span></div>${recordButton('Inspect resource', {resource:p.id,capacity:p.capacity,offers:p.offers,provider:p.provider,controller:p.controller,providers:[p.provider],controllers:[p.controller],hosts:[p.host].filter(Boolean)},'resource',p.id)}</article>`;}).join('')}</div>`;}
function resources(){return hostSection()+panel('Resource capacity','metrics',`<div class="panel-body"><p class="measurement-note">Each pool is counted once per resource and provider. Active lease counts are scoped to the selected controller and store; capacity is never added across its contracts.</p>${capacityCards()}</div>`)+panel('Mounted storage','metrics',`<div class="panel-body">${volumeCards(data('metrics'))}</div>`);}

function leases(){return panel('Current reservations','leases',table(['Lease','Contract / resource','State','Provider','Task'],list(data('leases'),['leases','items']).filter(match).map(l=>[recordButton(short(l.id||l.lease),l),esc(l.contract)+'<small>'+esc(l.resource||l.requested_resource)+'</small>',pill(l.state),esc(short(l.provider)),esc(l.task)]),'The selected controller and store reported no reservations.'))+panel('Gateway queue, all controllers','health',table(['Operation','State','Count','Oldest state age'],(health().active_queue?.groups||[]).filter(match).map(g=>[esc(g.operation),pill(g.state),esc(g.count),esc(g.oldest_state_age_seconds)+' s'])));}
function executions(){const p=data('progress');return panel('Outstanding commands','progress',`<div class="panel-body"><p class="small">${esc(p?.meaning||'Only currently outstanding activity is reported here. Inspect a known command ID for its retained result.')}</p><form id="lookup"><label>Command ID <input name="id" required placeholder="Exact operation ID"></label> <button>Read command status</button></form></div>`+table(['Command','State','Provider','Contract / resource'],list(p,['outstanding']).filter(match).map(x=>[recordButton(x.operation||x.id||'Command',x,'execution',x.operation||x.id),pill(x.state),esc(short(x.provider)),esc(x.contract||x.resource)])))+panel('Agent activity','progress',table(['Agent','State','Activity'],list(p,['assistants']).filter(match).map(a=>[recordButton(a.id||a.name||a.provider||'Agent',a),pill(a.state||a.status),esc(a.current||a.task||a.activity||'Inspect details')])));}
function streams(){const sessions=list(data('streams'),['sessions','streams','items']).filter(match);return panel('Stream flows','streams',`<div class="panel-body">${streamCards(sessions,inventory(),s=>recordButton('Inspect stream',s,'stream',s.id))}${sessions.length?'':empty('No registered streams reported','Registered controller sessions appear here; transports outside this registry are not inferred.')}<p class="measurement-note">Per-stream bandwidth is shown when reported by the stream record. Declared rate and message-size limits are not measured throughput.</p></div>`)+panel('Network throughput','metrics',`<div class="panel-body">${networkCards(data('metrics'))}<p class="measurement-note">Measured host interface traffic includes all applications on that interface. It is not attributed to individual streams.</p></div>`);}

function processes(){const summaries=data('summaries')?.runs||{},filter=state.runFilter||'all';const rows=list(data('processes'),['runs','items']).filter(match).filter(r=>filter==='all'||filter==='running'&&['running','waiting','open','paused'].includes(r.state)||filter==='attention'&&['failed','uncertain','waiting'].includes(r.state)||filter==='completed'&&r.state==='completed');return panel('Process runs','processes',`<div class="tabs">${[['all','All runs'],['running','Running'],['attention','Needs attention'],['completed','Completed']].map(([id,label])=>`<button data-run-filter="${id}" class="${filter===id?'sel':''}">${label}</button>`).join('')}</div>`+table(['Process / run','Status','Current activity / progression','Recorded steps'],rows.map(r=>{const summary=summaries[r.id]||{},nodes=summary.current_nodes||[];return [recordButton(r.name||r.id,r,'process',r.id||r.run)+'<small>'+esc(r.id||r.run)+'</small>',pill(r.state||r.status),nodes.length?nodes.map(n=>'<b>'+esc(n.name||n.label||n.id)+'</b><small>'+esc(n.state)+'</small>').join(''):(r.state==='completed'?'Process complete':'Inspect execution diagram →'),esc(r.steps??'Not reported')];}),'No process runs were reported.'))+panel('Execution summaries','summaries',`<div class="panel-body"><p class="small">${Object.keys(summaries).length} run summaries loaded. Select a run to view its pinned workflow graph, active nodes and retained history.</p></div>`);}

function workflows(){const definitions=list(data('processes'),['definitions']);return panel('Published workflows','processes',`<div class="panel-body cards">${definitions.filter(match).map(d=>`<article class="card"><span class="pill">PUBLISHED PROCESS</span><h2>${esc(d.name||d.id)}</h2><p>Revision <span class="mono">${esc(short(d.rev||d.revision))}</span></p><p>${Object.keys(d.inputs||{}).length} inputs · ${Object.keys(d.outputs||{}).length} outputs</p><div class="row">${recordButton('Inspect workflow →',d,'workflow',d.name||d.id)}</div></article>`).join('')||empty('No definitions reported','Select the controller and store that own your published processes.')}</div>`)+`<a class="design-link" href="https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Workflows">Original workflow library and interactive BPMN design → <span class="pill amber">Not implemented · Design preview</span></a>`;}
function workView(){
 const {byId,rows}=workHierarchy(list(data('work'),['work','items']));
 return panel('Work plan','work',table(['Work','State','Task progress','Prerequisites'],rows
  .filter(({item,ancestors})=>match([item,...ancestors]))
  .map(({item:w,ancestors,accepted,total})=>[
   recordButton(w.title||w.id,w)+'<small>'+esc([...ancestors.map(p=>p.title),friendlyIdentity(w.kind)].join(' › '))+'</small>',
   pill(w.blocked_by?.length&&w.state==='open'?'blocked':w.state),
   total?esc(accepted+' / '+total+' accepted'):'',
   workLinks(w,byId,(dep)=>recordButton(dep.title||dep.id,dep))
  ])));
}
function knowledgeView(){return panel('Saved knowledge','knowledge',table(['Document','Revision heads','Conflict'],list(data('knowledge'),['documents','items']).filter(match).map(d=>[recordButton(d.title||d.id,d,'document',d.id),`<span class="mono">${esc((d.heads||[]).map(short).join(', '))}</span>`,pill(d.conflict?'conflict':'No conflict reported')])));}
function accessView(){return panel('Enrolled provider identities','pairing',table(['Provider','Enrollment','Identity'],list(data('pairing'),['pairs','items']).filter(match).map(p=>[recordButton(p.label||p.id,p),pill(p.state),`<span class="mono">${esc(p.id)}</span>`])))+panel('Delivery destinations','destinations',table(['Destination','Kind','Inbox','State'],list(data('destinations'),['destinations','items']).filter(match).map(d=>[recordButton(d.id,d),esc(d.kind),esc(d.inbox),pill(d.state)])))+`<section class="panel"><div class="panelhead"><h2>Application connections</h2></div><div class="panel-body"><p>Review the controller's existing OAuth grants and their scopes.</p><a href="https://biomerte-controller.cantikash.chatgpt.site/oauth/connections" target="_blank" rel="noopener">Manage controller application connections ↗</a></div></section><a class="design-link" href="https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Access">Enterprise identity, role and governance design → <span class="pill amber">Not implemented</span></a>`;}
function activityView(){const events=data('events');return panel('Retained controller events','events',`<div class="panel-body"><form id="event-query"><label>Topic <input name="topic" value="${esc(state.eventTopic||'workflow.delegation.requested')}" required></label> <button>Read events</button></form><p class="small">Showing one exact event topic. The controller's retained history determines available events.</p>${events?.history_before_unavailable?'<p class="measurement-note">The retention window moved. Showing currently retained events; earlier history is no longer available.</p>':''}</div>`+table(['Sequence / event','Time','State'],list(events,['events','items']).filter(match).map(e=>[recordButton(e.topic||e.type||String(e.seq||e.sequence),e),esc(stamp(e.created_at||e.created||e.time)),pill(e.state||e.status||'Recorded')])))+panel('Inbox messages','inbox',table(['Message','To / correlation','State'],list(data('inbox'),['messages','items']).filter(match).map(m=>[recordButton(m.id||m.type||'Message',m),esc(m.to||m.correlation||m.address),pill(m.state||m.status||'Recorded')])))+`<a class="design-link" href="https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Audit">Original audit trail and export design → <span class="pill amber">Not implemented</span></a>`;}
function settingsView(){return panel('Instance identity','info',`<div class="panel-body">${fields(data('info'))}</div>`)+panel('Coordination configuration','cluster',`<div class="panel-body">${fields(data('cluster'))}${raw(data('cluster'))}</div>`)+panel('Current coordination session','session',`<div class="panel-body">${fields(data('session'))}${raw(data('session'))}</div>`)+`<a class="design-link" href="https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Settings">Original operating policy settings → <span class="pill amber">Not implemented</span></a>`;}
function decisions(){return panel('Decision records','decisions',table(['Question / decision','State','Requested','Workflow'],list(data('decisions'),['decisions','requests','items']).filter(match).map(d=>[recordButton(d.question||d.title||d.id,d,'decision',d.id),pill(d.state||d.status),esc(stamp(d.created_at||d.created)),esc(d.workflow_instance_id||d.workflow||'Not reported')]),'No decision records were reported by the selected controller and store.'));}
function interfaceView(){const ops=list(data('operations'),['items']);return panel('Controller operation contracts','operations',table(['Operation','Access','Description'],ops.filter(match).map(o=>[recordButton(o.name,o),pill(o.write?'Mutating operation':'Read operation'),esc(o.description||'No description published')])),more('operations'))+panel('Caller guidance','guide',`<div class="panel-body">${list(data('guide'),['items']).map(g=>Object.entries(g).map(([k,v])=>kv(k,esc(typeof v==='object'?JSON.stringify(v):v))).join('')).join('')}</div>`);}
function render(){if(state.process)return;state.records=[];$('title').textContent=labels[state.route];$('crumb').textContent=labels[state.route];$('subtitle').textContent=subtitles[state.route];document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',(a.pathname==='/'||a.pathname==='/dashboard/')&&a.hash==='#'+state.route));const views={overview,workflows,controllers,capabilities:caps,resources,storage:storageView,leases,executions,streams,processes,decisions,work:workView,knowledge:knowledgeView,access:accessView,activity:activityView,settings:settingsView,interface:interfaceView};$('view').innerHTML=views[state.route]();const lookup=$('lookup');if(lookup)lookup.onsubmit=e=>{e.preventDefault();inspect({label:'Command status',kind:'execution',id:new FormData(lookup).get('id')});};const events=$('event-query');if(events)events.onsubmit=e=>{e.preventDefault();state.eventTopic=new FormData(events).get('topic');read('events');};}
async function read(v,{cursor}={}){
 const key=readKey(v),prev=state.cache.get(key);if(state.busy.has(key))return;
 state.busy.add(key);render();
 try{const global=['controllers','health','contracts','offers','operations','guide'].includes(v),r=await consoleFetch('/api/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({view:v,realm:$('realm').value,...(!global&&$('controller').value?{controller:$('controller').value}:{}),...(v==='events'?{topic:state.eventTopic||'workflow.delegation.requested'}:{}),...(cursor?{cursor}:{})})});const d=await readJSON(r);if(!r.ok){if(r.status===401){state.connected=false;connectionUI();}throw Error(d.error||'Read failed');}
 if(cursor&&prev?.data?.items)d.data.items=[...prev.data.items,...d.data.items];
 state.cache.set(key,d);if(v==='metrics'&&d.data?.hosts?.some(h=>h.refreshing)){clearTimeout(state.metricsTimer);state.metricsTimer=setTimeout(()=>{if(state.connected&&!document.hidden&&['overview','resources','streams','storage'].includes(state.route))read('metrics');},3000);}if(v==='controllers')updateControllerOptions();$('last-update').textContent='Last response '+time(d.observed_at);
 }catch(e){if([401,403].includes(e.status)){state.connected=false;connectionUI();}state.cache.set(key,{...prev,error:e.message});}finally{state.busy.delete(key);render();}
}
function updateControllerOptions(){const value=$('controller').value;const c=data('controllers')?.controllers||[];$('controller').innerHTML='<option value="">Default controller</option>'+c.map(x=>`<option value="${esc(x.controller)}">${esc(x.label||short(x.controller))}${x.default?' · default':''}${x.ready?'':' · unavailable'}</option>`).join('');$('controller').value=c.some(x=>x.controller===value)?value:'';}
const required={overview:['controllers','health','info','progress','contracts','offers','metrics','streams'],workflows:['processes'],controllers:['controllers','health'],capabilities:['controllers','contracts'],resources:['controllers','offers','leases','health','metrics'],leases:['leases','health'],executions:['progress'],streams:['controllers','streams','metrics','offers'],storage:['controllers','metrics'],processes:['processes','summaries'],decisions:['decisions'],work:['work'],knowledge:['knowledge'],access:['pairing','destinations'],activity:['events','inbox'],settings:['info','cluster','session'],interface:['operations','guide']};
async function refresh(force=true){if(state.process){await openProcess(state.process);return;}if(!state.connected){$('status').textContent='Connect BiomeRTE to load current records.';return;}const generation=++state.generation;$('refresh').disabled=true;$('status').textContent='Reading existing controller interfaces…';await Promise.allSettled(required[state.route].filter(v=>force||!cached(v)||(v==='metrics'&&cached(v)?.data?.hosts?.some(h=>h.refreshing))).map(v=>read(v)));if(generation!==state.generation)return;$('refresh').disabled=false;const failures=required[state.route].filter(v=>cached(v)?.error);$('status').classList.toggle('error',!!failures.length);$('status').textContent=failures.length?'Some sources could not refresh. Each affected panel shows its error and last successful observation.':'Loaded live controller responses. Timestamps mark when each read completed.';}
function connectionUI(){$('connect-panel').hidden=state.connected;document.querySelectorAll('a[href^="/connect"]').forEach(a=>a.href='/connect?return_to='+encodeURIComponent('/'+location.hash));$('connect').textContent=state.connected?'Reconnect controller ↗':'Connect BiomeRTE ↗';$('connection-label').textContent=state.connected?'Controller authorized':'Controller not connected';$('connection-dot').classList.toggle('online',state.connected);if(location.pathname.startsWith('/dashboard')){$('connect').textContent=state.connected?'Controller session':'Sign in to reconnect';$('disconnect').textContent='Sign out';}$('disconnect').hidden=!state.connected;}
function fields(value){return Object.entries(value||{}).filter(([,v])=>typeof v!=='object'||v===null).map(([k,v])=>kv(k,esc(v??'Not reported'))).join('');}
async function inspect(rec){
 if(['workflow','process'].includes(rec.kind)){await openProcess(rec);return;}

 const dialog=$('detail'),body=$('detail-body');body.innerHTML=`<h2>${esc(rec.label)}</h2>${rec.value?fields(rec.value):'<p>Reading current status…</p>'}${raw(rec.value||{})}`;dialog.showModal();
 if(rec.kind==='workflow'){const w=rec.value;body.innerHTML=`<div class="eyebrow">PUBLISHED PROCESS DEFINITION</div><h2>${esc(w.name||w.id)}</h2>${fields({revision:w.rev||w.revision})}<h3>Input contract</h3><pre>${esc(JSON.stringify(w.inputs||{},null,2))}</pre><h3>Output contract</h3><pre>${esc(JSON.stringify(w.outputs||{},null,2))}</pre><div class="row"><a class="button primary" href="https://biomerte-controller.cantikash.chatgpt.site/dashboard/#workflows/${encodeURIComponent(w.name||w.id)}" target="_blank" rel="noopener">Open implemented workflow diagram ↗</a><a class="button" href="https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Workflows">Original interactive BPMN design</a></div>${raw(w)}`;return;}
 if(rec.kind==='capability'){
 const c=rec.value;body.innerHTML=`<h2>${esc(c.id||c.name)}</h2><p>${esc(c.description)}</p>${kv('Execution',esc(c.synchronous?'Synchronous':'Asynchronous'))}<h3>Available implementations</h3>${table(['Resource / host','Controller','State','Capacity'],(c.implementations||[]).map(i=>[esc(i.resource)+'<small>'+esc(i.host||'Host not advertised')+'</small>',esc(controllerName(i.controller)),pill(i.state),esc(i.capacity??'Not reported')]))}<h3>Input contract</h3><pre>${esc(JSON.stringify(c.inputs,null,2))}</pre><h3>Output contract</h3><pre>${esc(JSON.stringify(c.outputs,null,2))}</pre><h3>Usage</h3><p>${esc(c.usage?.before||c.usage?.instructions||'No usage guidance published.')}</p>${raw(c)}`;return;}
 if(rec.kind==='resource'){
 body.innerHTML=`<h2>${esc(rec.label==='Inspect resource'?rec.id:rec.label)}</h2><p>Advertised resource, grouped across the loaded implementation offers.</p>${fields({resource:rec.id,providers:rec.value.providers.join(', '),hosts:rec.value.hosts.join(', '),controllers:rec.value.controllers.map(controllerName).join(', ')})}${table(['Contract','State','Capacity','Service'],rec.value.offers.map(o=>[esc(o.contract),pill(o.state),esc(o.capacity??'Not reported'),esc(o.service)]))}${raw(rec.value)}`;return;
 }
 if(['execution','process','stream','decision','document'].includes(rec.kind)&&rec.id){try{const r=await consoleFetch('/api/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({view:rec.kind,id:rec.id,controller:$('controller').value||rec.value?.controller||undefined,realm:$('realm').value})});const d=await readJSON(r);if(!r.ok)throw Error(d.error);if(!dialog.open)return;body.innerHTML=`<h2>${esc(rec.label)}</h2><p class="small">${esc(d.source)} · observed ${time(d.observed_at)}</p>${fields(d.data)}${rec.kind==='process'?'<div class="row"><a class="button primary" href="https://biomerte-controller.cantikash.chatgpt.site/dashboard/#runs/'+encodeURIComponent(rec.id)+'" target="_blank" rel="noopener">Open implemented run diagram ↗</a><a class="button" href="https://biomerte-enterprise-console-concept.cantikash.chatgpt.site/design/#Runs">Original run tracking design</a></div>':''}${rec.kind==='process'&&d.data.trace?.length?'<h3>Retained execution history</h3>'+table(['Step','State'],d.data.trace.map(t=>[esc(t.name||t.node||t.step),pill(t.state||t.status)])):''}${raw(d.data)}`;}catch(e){body.innerHTML+=`<div class="error-box">${esc(e.message)}</div>`;}}
}
async function processRead(body){const response=await consoleFetch('/api/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,realm:$('realm').value,...($('controller').value?{controller:$('controller').value}:{})})});const value=await readJSON(response);if(!response.ok)throw Error(value.error||'Process read failed');return value;}
async function openProcess(rec){
 state.process=rec;const selected=rec;history.replaceState(null,'','#'+(rec.kind==='process'?'processes':'workflows')+'/'+encodeURIComponent(rec.id));
 $('title').textContent=rec.kind==='process'?'Process execution':'Workflow diagram';$('subtitle').textContent=rec.label;
 $('view').innerHTML='<section class="panel"><div class="panel-body"><h2>Loading workflow and execution state…</h2><p>The diagram uses the revision pinned to this run.</p></div></section>';
 try{const runResponse=rec.kind==='process'?await processRead({view:'process',id:rec.id}):null,run=runResponse?.data;
 const w=rec.value||{},name=run?.name||w.name||w.id||rec.id,revision=run?.rev||w.rev||w.revision;
 const definition=await processRead({view:'definition',name,revision});if(state.process!==selected)return;
 $('title').textContent=name;$('crumb').textContent=(run?'Process runs / ':'Workflows / ')+name;
 mountProcess($('view'),definition.data,run,{onBack:()=>{state.process=null;history.replaceState(null,'','#'+state.route);render();refresh(false);},onRefresh:()=>openProcess(rec)});
 $('last-update').textContent='Definition and state observed '+time(definition.observed_at);
 }catch(error){if([401,403].includes(error.status)){state.connected=false;connectionUI();}if(state.process!==selected)return;$('view').innerHTML='<button class="link" id="process-back">← Back</button><section class="panel"><div class="error-box"><h2>Process view could not load</h2><p>'+esc(error.message)+'</p><button id="process-retry">Retry read</button></div></section>';$('process-back').onclick=()=>{state.process=null;history.replaceState(null,'','#'+state.route);render();refresh(false);};$('process-retry').onclick=()=>openProcess(rec);}
}
$('close-detail').onclick=()=>$('detail').close();$('detail').onclick=e=>{if(e.target===$('detail')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}};
document.addEventListener('click',e=>{const rec=e.target.closest('[data-record]');if(rec)inspect(state.records[+rec.dataset.record]);const more=e.target.closest('[data-more]');if(more)read(more.dataset.more,{cursor:data(more.dataset.more)?.next_cursor});const runFilter=e.target.closest('[data-run-filter]');if(runFilter){state.runFilter=runFilter.dataset.runFilter;render();}const ns=e.target.closest('[data-namespace]');if(ns){state.namespace=ns.dataset.namespace;render();}});
function route(){state.process=null;const [view,...parts]=location.hash.slice(1).split('/');state.route=labels[view]?view:'overview';state.namespace='all';$('search').value='';render();const id=decodeURIComponent(parts.join('/'));if(id&&['processes','workflows'].includes(state.route)){openProcess({kind:state.route==='processes'?'process':'workflow',id,label:id});return;}refresh(false);}

window.addEventListener('hashchange',route);$('refresh').onclick=()=>refresh();$('search').oninput=render;$('controller').onchange=()=>{state.process=null;render();refresh(false);};$('realm').onchange=()=>{state.process=null;render();refresh(false);};
$('disconnect').onclick=async()=>{const r=await consoleFetch('/disconnect',{method:'POST'});if(r.ok){state.connected=false;state.cache.clear();connectionUI();render();}};
setInterval(()=>{if($('auto').checked&&state.connected&&!document.hidden&&!state.busy.size&&!$('detail').open)refresh();},15000);
$('view').innerHTML='<section class="panel"><div class="panel-body"><h2>Opening your workspace</h2><p class="measurement-note">Checking the existing controller connection…</p></div></section>';
try{const r=await consoleFetch('/api/connection');state.connection=await readJSON(r);state.connected=state.connection.connected;connectionUI();route();}catch(e){$('status').textContent=e.message;connectionUI();render();}

}else{


const el=id=>document.getElementById(id);
let snapshot={},mappings={},operations=[],summaries={},connected=false,connecting=false,selectedNode='',renderGeneration=0,filter='all';
const definitionCache=new Map(),runCache=new Map();
const say=(text,error=false)=>{el('status').textContent=text;el('status').classList.toggle('error',error);};
const toast=text=>{el('toast').textContent=text;el('toast').hidden=false;setTimeout(()=>el('toast').hidden=true,5000);};
const call=createClient(fetch,say,()=>el('realm').value);
const n=(kind,id,source={})=>nameFor(mappings,kind,id,source);
const m=(kind,id,source={})=>mappingFor(mappings,kind,id,source).metadata;
const pill=state=>'<span class="pill '+statusTone(state)+'">'+esc(statusName(state))+'</span>';
const raw=value=>'<details><summary>Advanced details</summary><pre>'+esc(JSON.stringify(value,null,2))+'</pre></details>';
const empty=(title,text)=>'<div class="empty"><h2>'+esc(title)+'</h2><p>'+esc(text)+'</p></div>';
const button=(label,action,id='',cls='')=>'<button class="'+cls+'" data-action="'+action+'" data-id="'+esc(id)+'">'+esc(label)+'</button>';
const field=(label,value)=>'<div class="summary-item"><span>'+esc(label)+'</span><strong>'+value+'</strong></div>';
function route(){const parts=location.hash.replace(/^#\/?/,'').split('/');return {view:parts[0]||'overview',id:decodeURIComponent(parts.slice(1).join('/'))};}
function go(view,id=''){location.hash=view+(id?'/'+encodeURIComponent(id):'');}
const query=()=>el('search').value.trim().toLowerCase();
const match=(...values)=>!query()||values.some(x=>String(x??'').toLowerCase().includes(query()));
function nodeLabel(node,workflow,revision='') {
 const stepMetadata=m('workflow',workflow).steps||{};
 const override=stepMetadata[revision+':'+node.id]?.name||stepMetadata[node.id]?.name;
 if(override)return override;
 if(node.name||node.label||node.title)return node.name||node.label||node.title;
 if(node.type==='start')return 'Start';
 if(node.type==='end')return 'Finish';
 if(['if','choose','choice','exclusive'].includes(node.type))return 'Choose a path';
 if(['parallel','fork'].includes(node.type))return 'Run in parallel';
 if(node.type==='join')return 'Continue together';
 if(node.type==='wait'||node.type==='human')return 'Wait for a decision';
 if(node.process)return n('workflow',node.process);
 if(node.contract)return n('capability',node.contract);
 if(node.operation)return n('operation',node.operation);
 if(node.action)return friendlyIdentity(node.action);
 return 'Step '+String(node.id||'').replace(/^n/,'');
}
function runInfo(run){return {...run,...(summaries[run.id]||{})};}
function currentActivity(run){
 const info=runInfo(run),nodes=info.current_nodes||[];
 if(nodes.length)return nodes.map(node=>nodeLabel(node,run.name,run.rev)).join(' · ');
 return run.state==='completed'?'Process complete':run.state==='failed'?'Review failure':run.state==='cancelled'?'Cancelled':'View progress';
}
async function request(path,body=null){
 const res=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
 const data=await res.json();if(!res.ok)throw Error(data.error||'This request could not be completed.');return data;
}
async function readMappings(){const data=await request('/dashboard/mappings?realm='+el('realm').value);return Object.fromEntries((data.mappings||[]).map(x=>[mappingKey(x.kind,x.identity),x]));}
async function readSummaries(){try{const data=await request('/dashboard/definition',{mode:'summaries',realm:el('realm').value});summaries=data.result?.runs||{};}catch(error){summaries={};say('Workspace loaded. Process details: '+error.message,true);}}
async function refresh(){
 say('Loading workspace…');
 let data;
 try{data=await call('system.snapshot');}catch(error){if(el('realm').value!=='site')throw error;data=await call('system.snapshot',{},true);say('Showing the last synchronized shared workspace.');}
 snapshot=data;
 try{mappings=await readMappings();}catch(error){say('Workspace loaded. Saved names are temporarily unavailable.',true);}
 await readSummaries();
 const instance=n('instance',snapshot.site?.site,{title:'My Biome'});
 el('instance-name').textContent=instance;el('top-instance').textContent=instance;
 el('instance-state').textContent=el('realm').value==='site'?'Shared workspace':'Local workspace';
 el('decision-count').textContent=(snapshot.waits||[]).length;el('decision-count').hidden=!(snapshot.waits||[]).length;
 el('updated-at').textContent='Updated '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
 await render();
}
async function connectDashboard(event){
 event?.preventDefault();if(connecting)return;connecting=true;
 el('connect').disabled=true;el('connect').textContent='Connecting…';say('Connecting to controller…');
 try{
  operations=await call('bus.discover',{},true);await refresh();connected=true;
  el('login').hidden=true;el('workspace').hidden=false;await render();say('Connected');
 }catch(error){say('Connection failed: '+error.message,true);el('login').hidden=false;el('connection-title').textContent='Connection needs attention';}
 finally{connecting=false;el('connect').disabled=false;el('connect').textContent='Connect';}
}
function workflowCards(definitions) {
 return '<div class="card-grid">'+definitions.map(d=>{const meta=m('workflow',d.id);const count=(snapshot.process?.runs||[]).filter(r=>r.name===d.id).length;return '<article class="catalog-card"><span class="eyebrow">WORKFLOW</span><h2>'+esc(meta.name)+'</h2><p>'+esc(meta.summary||'Open the process diagram, review its steps, or start a run.')+'</p><div class="card-bottom"><span class="subtle">'+count+' recorded '+(count===1?'run':'runs')+'</span>'+button('Open workflow →','workflow',d.id)+'</div></article>';}).join('')+'</div>';
}
function runTable(runs,{compact=false}={}) {
 if(!runs.length)return empty('No matching runs','Runs appear here when a workflow is started.');
 return '<div class="table-scroll"><table><thead><tr><th>PROCESS / RUN</th><th>STATUS</th><th>CURRENT ACTIVITY</th>'+(!compact?'<th>ELAPSED</th>':'')+'</tr></thead><tbody>'+runs.map(run=>'<tr data-run="'+esc(run.id)+'" tabindex="0" role="button" aria-label="View '+esc(runTitle(run,snapshot,mappings))+'"><td><strong>'+esc(runTitle(run,snapshot,mappings))+'</strong><small>Run '+esc(shortID(run.id))+'</small></td><td>'+pill(run.state)+'</td><td>'+esc(currentActivity(run))+'</td>'+(!compact?'<td class="muted">'+esc(elapsedLabel(runInfo(run)))+' →</td>':'')+'</tr>').join('')+'</tbody></table></div>';
}
function runFilters(){return '<div class="filters">'+[['all','All runs'],['running','Running'],['attention','Needs attention'],['completed','Completed']].map(([key,label])=>'<button data-filter="'+key+'" class="'+(filter===key?'active':'')+'">'+label+'</button>').join('')+'</div>';}
function overview(){
 const stats=overviewStats(snapshot),runs=[...(snapshot.process?.runs||[])].reverse();
 return '<div class="stats">'+[
 ['Active runs',stats.active,'Currently running or waiting'],['Completed',stats.completed,'In recorded run history'],['Needs attention',stats.attention,'Decisions waiting and failed runs'],['Completion rate',stats.rate===null?'—':stats.rate+'%','Completed ÷ completed and failed runs']].map(([label,value,note])=>'<article class="stat"><span class="stat-label">'+label+'</span><strong class="stat-value">'+value+'</strong><small>'+note+'</small></article>').join('')+'</div>'+
 '<div class="overview-grid"><div><section class="panel"><div class="panel-head"><h2>Process runs</h2>'+button('View all runs →','runs')+'</div>'+runTable(runs.slice(0,8),{compact:true})+'</section><section class="panel"><div class="panel-head"><h2>Workflow library</h2>'+button('View workflows →','workflows')+'</div><div class="panel-body">'+workflowCards((snapshot.process?.definitions||[]).slice(0,3))+'</div></section></div><div><section class="panel"><div class="panel-head"><h2>Needs your attention</h2><span class="pill warning">'+stats.attention+'</span></div>'+attentionItems(5)+'</section><section class="panel"><div class="panel-head"><h2>Available capacity</h2>'+button('View resources →','resources')+'</div><div class="panel-body">'+resourcePools(snapshot).slice(0,4).map(pool=>'<div class="provider-row"><div><strong>'+esc(n('resource',pool.id,{title:friendlyIdentity(pool.offers[0]?.service||pool.id)}))+'</strong><small>'+pool.allocated+' allocated'+(pool.capacity?' · '+pool.capacity+' slots':'')+'</small></div>'+pill(pool.states.includes('ready')?'ready':'unknown')+'</div>').join('')+'</div></section></div></div>';
}
function attentionItems(limit=null){
 const waits=snapshot.waits||[],failed=(snapshot.process?.runs||[]).filter(r=>r.state==='failed');
 const rows=[...waits.map(w=>({title:decisionTitle(w),text:w.question||'Your response is needed to continue.',kind:'Waiting for decision',action:'decision',id:w.id})),...failed.map(r=>({title:runTitle(r,snapshot,mappings),text:'This run stopped with a failure. Open it to review the result and its recorded steps.',kind:'Needs review',action:'run',id:r.id}))];
 if(!rows.length)return empty('Nothing needs your attention','Decisions and failed runs will appear here.');
 return (limit?rows.slice(0,limit):rows).map(x=>'<article class="attention-item"><span class="pill warning">'+esc(x.kind)+'</span><h3>'+esc(x.title)+'</h3><p>'+esc(x.text)+'</p>'+button(x.action==='decision'?'Review decision →':'View run →',x.action,x.id)+'</article>').join('');
}
function decisionTitle(wait){return String(wait.title||wait.name||wait.question||'Decision needed').split(/\s+/).slice(0,10).join(' ');}
function capabilities(path=''){
 const root=namespaceTree(snapshot.capabilities?.contracts),node=namespaceAt(root,path);
 if(!node)return empty('Namespace not found','Choose Capabilities to return to the namespace tree.');
 const parts=path.split('.').filter(Boolean);let prefix='';
 const crumbs='<div class="breadcrumbs">'+button('Capabilities','namespace','')+parts.map(p=>{prefix+=(prefix?'.':'')+p;return '<span>›</span>'+button(n('namespace',prefix),'namespace',prefix);}).join('')+'</div>';
 const children=[...node.children.values()].sort((a,b)=>n('namespace',a.identity).localeCompare(n('namespace',b.identity)));
 return crumbs+'<div class="namespace-heading"><div><h2>'+esc(path?n('namespace',path):'Capability namespaces')+'</h2>'+(path?'<p class="identity-line">'+esc(path)+'</p>':'<p class="muted small">Browse the capabilities available to your workflows and agents.</p>')+'</div>'+(path?button('Edit name','rename-namespace',path):'')+'</div><div class="card-grid">'+children.filter(child=>match(n('namespace',child.identity),child.identity)).map(child=>{
  const isLeaf=!child.children.size&&child.contracts.length===1,contract=child.contracts[0],kind=isLeaf?'capability':'namespace',id=isLeaf?contract.id:child.identity;
  const meta=m(kind,id,isLeaf?contract:{});const offers=isLeaf?(snapshot.capabilities?.offers||[]).filter(o=>o.contract===contract.id):[];
  return '<article class="catalog-card"><span class="eyebrow">'+(isLeaf?'CAPABILITY':'NAMESPACE')+'</span><h2>'+esc(meta.name)+'</h2><span class="identity">'+esc(id)+'</span><p>'+esc(meta.summary||(isLeaf?'View its purpose, inputs, outputs, and available providers.':descendantCount(child)+' capabilities in this namespace.'))+'</p><div class="card-bottom"><span class="subtle">'+(isLeaf?offers.length+' providers':child.children.size+' groups')+'</span>'+button('Open →',isLeaf?'capability':'namespace',id)+'</div></article>';
 }).join('')+node.contracts.filter(c=>match(n('capability',c.id,c),c.id)).map(c=>'<article class="catalog-card"><span class="eyebrow">EXECUTABLE CAPABILITY</span><h2>'+esc(n('capability',c.id,c))+'</h2><span class="identity">'+esc(c.id)+'</span><p>'+esc(m('capability',c.id,c).summary)+'</p><div class="card-bottom">'+button('View capability →','capability',c.id)+'</div></article>').join('')+'</div>';
}
function capabilityDetail(id){
 const contract=(snapshot.capabilities?.contracts||[]).find(c=>c.id===id);if(!contract)return empty('Capability not found','Refresh to read the latest capability registry.');
 const offers=(snapshot.capabilities?.offers||[]).filter(o=>o.contract===id),meta=m('capability',id,contract);
 const typeNames={str:'Text',obj:'Structured data',list:'List',int:'Whole number',num:'Number',bool:'Yes or no',any:'Any value'};
 const pathParts=id.replace(/:\d+$/,'').split('.').slice(0,-1);let prefix='';
 const capabilityCrumbs='<div class="breadcrumbs">'+button('Capabilities','namespace','')+pathParts.map(part=>{prefix+=(prefix?'.':'')+part;return '<span>›</span>'+button(n('namespace',prefix),'namespace',prefix);}).join('')+'<span>›</span>'+esc(meta.name)+'</div>';
 const fields=(schema)=>Object.entries(schema||{}).map(([key,value])=>'<tr><td><strong>'+esc(friendlyIdentity(key))+'</strong><small>'+esc(key)+'</small></td><td>'+esc(typeNames[typeof value==='string'?value:value.type]||'Value')+'</td><td>'+esc(typeof value==='object'&&value.required?'Required':'Optional')+'</td></tr>').join('');
 return capabilityCrumbs+'<section class="panel"><div class="panel-head"><h2>'+esc(meta.name)+'</h2>'+button('Edit name','rename-capability',id)+'</div><div class="panel-body"><p>'+esc(meta.summary||contract.description||'No description has been added.')+'</p><p class="identity-line">'+esc(id)+'</p></div></section><div class="overview-grid"><div><section class="panel"><div class="panel-head"><h2>Inputs</h2></div><table><thead><tr><th>FIELD</th><th>VALUE</th><th>REQUIRED</th></tr></thead><tbody>'+fields(contract.inputs)+'</tbody></table></section><section class="panel"><div class="panel-head"><h2>Outputs</h2></div><table><thead><tr><th>FIELD</th><th>VALUE</th><th>REQUIRED</th></tr></thead><tbody>'+fields(contract.outputs)+'</tbody></table></section></div><section class="panel"><div class="panel-head"><h2>Providers</h2><span class="subtle">'+offers.length+' registered</span></div><div class="panel-body">'+(offers.length?offers.map(o=>'<div class="provider-row"><div><strong>'+esc(n('resource',o.resource,{title:friendlyIdentity(o.service||'Provider')}))+'</strong><small>'+esc(o.capacity!==undefined?o.capacity+' concurrent slots':'Capacity not reported')+'</small></div>'+pill(o.state)+'</div>').join(''):empty('No providers','No provider currently advertises this capability.'))+raw({contract,offers})+'</div></section></div>';
}
function resources(){
 const pools=resourcePools(snapshot).filter(p=>match(n('resource',p.id),p.id,...p.contracts));
 return '<p class="muted small">Capacity and availability reported by registered providers.</p><div class="card-grid">'+pools.map(pool=>{const meta=m('resource',pool.id,{title:friendlyIdentity(pool.offers[0]?.service||pool.id)}),use=pool.capacity?Math.min(100,100*pool.allocated/pool.capacity):0;return '<article class="catalog-card"><span class="eyebrow">RESOURCE POOL</span><h2>'+esc(meta.name)+'</h2><p>'+esc(meta.summary||pool.contracts.length+' supported capabilities')+'</p><p>'+pool.allocated+' allocated'+(pool.capacity?' of '+pool.capacity+' slots':'')+' · '+pool.queued+' queued</p><div class="capacity"><span data-capacity="'+use+'"></span></div><div class="card-bottom">'+pill(pool.states.includes('ready')?'ready':'unknown')+button('View details →','resource',pool.id)+'</div></article>';}).join('')+'</div>';
}
function workView(){
 const {byId,rows}=workHierarchy(snapshot.work||[]);
 const visible=rows.filter(({item:w,ancestors})=>match(w.title,w.kind,w.state,...ancestors.map(p=>p.title)));
 const link=dep=>button(dep.title,'inspect-work',dep.id);
 return '<section class="panel"><div class="panel-head"><h2>Goals and work</h2>'+button('Add work','new-work')+'</div>'+
  (visible.length?'<div class="table-scroll"><table><thead><tr><th>Work</th><th>Status</th><th>Task progress</th><th>Prerequisites</th></tr></thead><tbody>'+
  visible.map(({item:w,ancestors,accepted,total})=>'<tr><td>'+link(w)+
   '<small>'+esc([...ancestors.map(p=>p.title),friendlyIdentity(w.kind)].join(' › '))+'</small></td><td>'+
   pill(w.blocked_by?.length&&w.state==='open'?'blocked':w.state)+'</td><td>'+
   (w.kind!=='task'&&total?esc(accepted+' / '+total+' accepted'):'')+'</td><td>'+
   workLinks(w,byId,link)+'</td></tr>').join('')+'</tbody></table></div>':
   empty('No matching work','Add a goal, milestone, workflow, or task to organize your work.'))+'</section>';
}
function knowledgeView(){
 return '<section class="panel"><div class="panel-head"><h2>Saved knowledge</h2>'+button('＋ Add document','new-document')+'</div><div class="panel-body"><div class="card-grid">'+(snapshot.documents||[]).filter(d=>match(n('document',d.id),d.id)).map(d=>'<article class="catalog-card"><span class="eyebrow">DOCUMENT</span><h2>'+esc(n('document',d.id))+'</h2><p>'+esc(d.conflict?'Multiple versions need review.':'Saved in this workspace.')+'</p><div class="card-bottom">'+pill(d.conflict?'review':'ready')+button('Read →','document',d.id)+'</div></article>').join('')+'</div>'+(!(snapshot.documents||[]).length?empty('No documents yet','Save process results and reusable knowledge here.'):'')+'</div></section>';
}
function activityView(){
 const messages=[...(snapshot.inbox||[])].reverse();
 return '<section class="panel"><div class="panel-head"><h2>Recorded activity</h2><span class="subtle">Controller messages</span></div><div class="panel-body">'+(messages.length?'<ul class="history">'+messages.filter(x=>match(x.type,x.topic,x.from,x.to)).map(x=>'<li><strong>'+esc(n('event',x.type||x.topic||'Message'))+'</strong><small>'+esc(x.summary||'Recorded controller message')+'</small>'+raw(x)+'</li>').join('')+'</ul>':empty('No messages','Recorded controller messages will appear here.'))+'</div></section><p class="muted small">This view shows recorded messages. It is not a complete security audit log.</p>';
}
function settingsView(){
 const title=n('instance',snapshot.site?.site,{title:'My Biome'});
 return '<section class="panel"><div class="panel-head"><h2>Your instance</h2></div><div class="panel-body"><div class="provider-row"><div><strong>'+esc(title)+'</strong><small>Name shown throughout the console</small></div>'+button('Edit name','rename-instance',snapshot.site?.site)+'</div><div class="provider-row"><div><strong>Access & connections</strong><small>Review connected applications using your authenticated owner account.</small></div><a class="text-link" href="/oauth/connections">Manage connections →</a></div></div></section><section class="panel"><div class="panel-head"><h2>Names and descriptions</h2></div><div class="panel-body"><p class="muted small">Readable names are stored as metadata on each mapping. The workflow, capability, namespace, resource, and step identities stay unchanged.</p><div class="table-scroll"><table><thead><tr><th>NAME</th><th>TYPE</th><th></th></tr></thead><tbody>'+Object.values(mappings).filter(x=>match(x.metadata.name,x.identity)).map(x=>'<tr><td><strong>'+esc(x.metadata.name)+'</strong><small>'+esc(x.identity)+'</small></td><td>'+esc(friendlyIdentity(x.kind))+'</td><td>'+button('Edit','edit-mapping',x.kind+'|'+x.identity)+'</td></tr>').join('')+'</tbody></table></div></div></section>';
}
function advancedView(){
 return '<section class="panel"><div class="panel-head"><h2>Service console</h2></div><div class="panel-body"><p class="muted small">Direct controller operations for advanced administration.</p><form id="service-form" class="form-grid"><label class="field">Operation<select id="service-operation">'+operations.map(o=>'<option value="'+esc(o.name)+'">'+esc(o.name)+'</option>').join('')+'</select></label><p id="service-help" class="muted small"></p><label class="field">Arguments (JSON)<textarea id="service-arguments">{}</textarea></label><div class="inline-actions"><button class="primary" type="submit">Run operation</button>'+button('Controller status','controller')+button('Run demonstration','demo')+'</div></form><div id="service-result"></div></div></section><section class="panel"><div class="panel-head"><h2>Enrollment requests</h2></div><div class="panel-body">'+((snapshot.candidates||[]).map(c=>'<div class="provider-row"><div><strong>'+esc(c.label||'New provider')+'</strong><small>'+esc(statusName(c.state))+'</small></div>'+button('Review','enrollment',c.id)+'</div>').join('')||'<p class="muted">No enrollment requests.</p>')+raw(snapshot.site)+'</div></section>';
}
async function loadDefinition(name,revision=''){
 const key=el('realm').value+':'+name+':'+revision;
 if(!definitionCache.has(key)){const data=await request('/dashboard/definition',{name,revision,realm:el('realm').value});definitionCache.set(key,data.result);}
 return definitionCache.get(key);
}
async function processPage(view,id,generation){
 const run=view==='runs'?(runCache.get(id)||await call('process.get',{run:id})):null;if(run)runCache.set(id,run);
 const name=run?run.name:id,summary=(snapshot.process?.definitions||[]).find(d=>d.id===name);
 const record=await loadDefinition(name,run?.rev||summary?.rev||'');if(generation!==renderGeneration)return;
 const graph=graphModel(record);const title=run?runTitle(run,snapshot,mappings):n('workflow',name);
 el('page-title').textContent=title;el('page-subtitle').textContent=run?'Follow the active steps and understand what happens next.':m('workflow',name).summary||'Review the process diagram and its steps.';
 const states=run?.tokens||[],active=states.map(t=>t.node||t.at||t.node_id).filter(Boolean);
 const diagram=diagramMarkup(graph,{run,labelFor:node=>nodeLabel(node,name,record.id||record.rev),selected:selectedNode});
 el('view').innerHTML='<div class="breadcrumbs">'+button(view==='runs'?'Process runs':'Workflows',view)+'<span>›</span>'+esc(title)+'</div><div class="summary-row">'+
  field('Status',run?pill(run.state):pill('ready'))+field(run?'Current activity':'Inputs',esc(run?currentActivity(run):Object.keys(summary?.inputs||record.graph?.inputs||{}).map(friendlyIdentity).join(', ')||'No inputs'))+
  field(run?'Recorded steps':'Revision',esc(run?run.steps:shortID(record.id||record.rev||summary?.rev)))+
  '<div class="summary-item"><span>Actions</span><div class="inline-actions">'+(run?button('Refresh progress','refresh-run',id)+(['running','waiting','open','paused'].includes(run.state)?button('Cancel run','cancel-run',id):''):button('Start a run','start-workflow',name))+button('Edit name',run?'rename-run':'rename-workflow',id)+'</div></div></div>'+
  '<div class="split"><section class="diagram-panel" id="process-diagram">'+diagramToolbar+'<div class="diagram-viewport">'+diagram.html+'</div></section><section class="panel step-panel"><div class="panel-head"><h2>Step details</h2></div><div class="panel-body" id="step-details"><p>Select a step in the diagram to view its purpose and settings.</p>'+(active.length?'<p><strong>'+active.length+' active '+(active.length===1?'step':'steps')+'</strong></p>':'')+'</div></section></div>'+
  (run?'<section class="panel run-history"><div class="panel-head"><h2>Run history</h2></div><div class="panel-body">'+(run.trace?.length?'<ul class="history">'+run.trace.map(t=>'<li><strong>'+esc(nodeLabel({id:t.node||t.step||t.node_id,name:t.name,operation:t.operation,type:t.type},name))+'</strong><small>'+esc(t.status||t.state||'Recorded step')+'</small>'+raw(t)+'</li>').join('')+'</ul>':'<p class="muted small">Detailed step history was not retained for this run. Active steps above come from its current execution state.</p>')+(run.children?.length?'<h3>Related runs</h3><div class="inline-actions">'+run.children.map(child=>button('Open related run','run',typeof child==='string'?child:child.id)).join('')+'</div>':'')+raw(run)+'</div></section>':raw(record));
 const select=id=>{
  selectedNode=id;const node=graph.nodes.find(n=>n.id===id);if(!node)return;
  for(const g of el('process-diagram').querySelectorAll('[data-node]'))g.classList.toggle('selected',g.dataset.node===id);
  const meta=m('workflow',name).steps?.[(record.id||record.rev)+':'+id]||m('workflow',name).steps?.[id]||{};
  el('step-details').innerHTML='<h3>'+esc(nodeLabel(node,name,record.id||record.rev))+'</h3><p>'+esc(meta.summary||'This step follows the published workflow definition.')+'</p><dl class="field-list"><div><dt>Step type</dt><dd>'+esc(friendlyIdentity(node.type))+'</dd></div>'+(node.contract?'<div><dt>Capability</dt><dd>'+esc(n('capability',node.contract))+'</dd></div>':'')+(node.process?'<div><dt>Workflow</dt><dd>'+esc(n('workflow',node.process))+'</dd></div>':'')+'</dl>'+button('Edit step name','rename-step',name+'|'+(record.id||record.rev)+'|'+id)+raw(node);
 };
 wireDiagram(el('process-diagram'),diagram.layout,select);if(active[0]||selectedNode)select(selectedNode||active[0]);
}
const pageInfo={overview:['Operations overview','Your processes, people, and capabilities. Working together.'],workflows:['Workflow library','The processes that run your instance.'],runs:['Process runs','See what is running, waiting, completed, or needs attention.'],decisions:['Human decisions','Clear questions and responses that keep work moving.'],work:['Work plan','Organize goals, milestones, workflows, and tasks.'],knowledge:['Knowledge','Saved results and reusable documents.'],capabilities:['Capabilities','Browse capabilities by namespace and purpose.'],resources:['Resources & capacity','Understand availability and where work can run.'],activity:['Activity','Recorded controller messages and process activity.'],settings:['Settings','Manage names, descriptions, and your instance.'],advanced:['Advanced tools','Controller operations, enrollment, and diagnostics.']};
async function render(){
 const {view,id}=route(),generation=++renderGeneration,[title,subtitle]=pageInfo[view]||pageInfo.overview;
 el('page-title').textContent=title;el('page-subtitle').textContent=subtitle;el('crumb').textContent=title;
 el('eyebrow').textContent=['capabilities','resources','settings','advanced'].includes(view)?'INSTANCE MANAGEMENT':'WORKSPACE';
 document.querySelectorAll('nav [data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===(view==='capability'?'capabilities':view)));
 el('search').disabled=!!id&&!['capabilities'].includes(view);
 el('view').innerHTML='<div class="empty"><span class="spinner"></span><p>Loading…</p></div>';
 try{
  if((view==='workflows'||view==='runs')&&id){await processPage(view,id,generation);return;}
  if(view==='capability'){el('page-title').textContent=n('capability',id);el('page-subtitle').textContent='Purpose, inputs, outputs, and available providers.';el('crumb').textContent='Capabilities';el('view').innerHTML=capabilityDetail(id);return;}
  let content='';
  if(view==='overview')content=overview();
  else if(view==='workflows')content=workflowCards((snapshot.process?.definitions||[]).filter(d=>match(n('workflow',d.id),d.id,m('workflow',d.id).summary)));
  else if(view==='runs'){let runs=[...(snapshot.process?.runs||[])].reverse().filter(r=>match(runTitle(r,snapshot,mappings),r.name,statusName(r.state),r.id));if(filter==='running')runs=runs.filter(r=>['running','waiting','open'].includes(r.state));if(filter==='completed')runs=runs.filter(r=>r.state==='completed');if(filter==='attention')runs=runs.filter(r=>['failed','uncertain','waiting'].includes(r.state));content='<section class="panel">'+runFilters()+runTable(runs)+'</section>';}
  else if(view==='decisions')content='<section class="panel">'+attentionItems()+'</section>';
  else if(view==='capabilities')content=capabilities(id);
  else if(view==='resources')content=resources();
  else if(view==='work')content=workView();
  else if(view==='knowledge')content=knowledgeView();
  else if(view==='activity')content=activityView();
  else if(view==='settings')content=settingsView();
  else if(view==='advanced')content=advancedView();
  else content=overview();
  el('view').innerHTML=content;
  el('view').querySelectorAll('[data-capacity]').forEach(bar=>{bar.style.width=bar.dataset.capacity+'%';});
  if(view==='advanced'){serviceHelp();el('service-operation').onchange=serviceHelp;el('service-form').onsubmit=async event=>{event.preventDefault();await action(async()=>{const value=await call(el('service-operation').value,JSON.parse(el('service-arguments').value));el('service-result').innerHTML='<h3>Result</h3><pre>'+esc(JSON.stringify(value,null,2))+'</pre>';toast('Operation completed');})();};}
 }catch(error){el('view').innerHTML=empty('This view could not load',error.message)+button('Try again','retry-view');say(error.message,true);}
}
function serviceHelp(){const op=operations.find(x=>x.name===el('service-operation').value);el('service-help').textContent=op?.description||'Use the operation’s input fields and exact identities.';}
function action(fn){return async(event)=>{event?.preventDefault();try{await fn(event)}catch(error){say(error.message,true);toast(error.message);}};}
function openModal(title,body,submit=null,label='Save'){
 el('modal-title').textContent=title;el('modal-body').innerHTML=body;el('modal-error').textContent='';el('modal-submit').textContent=label;el('modal-submit').hidden=!submit;el('modal-cancel').textContent=submit?'Cancel':'Close';
 el('modal-form').onsubmit=async event=>{event.preventDefault();if(!submit)return;el('modal-submit').disabled=true;try{await submit(new FormData(el('modal-form')));el('modal').close();}catch(error){el('modal-error').textContent=error.message;}finally{el('modal-submit').disabled=false;}};
 if(!el('modal').open)el('modal').showModal();
}
async function editMapping(kind,id){
 const run=(snapshot.process?.runs||[]).find(r=>r.id===id);
 const pool=resourcePools(snapshot).find(p=>p.id===id);
 const fallback=kind==='instance'?'My Biome':kind==='run'&&run?runTitle(run,snapshot,mappings):kind==='resource'&&pool?friendlyIdentity(pool.offers[0]?.service||id):undefined;
 const meta=m(kind,id,{title:fallback});
 openModal('Edit '+friendlyIdentity(kind)+' name','<div class="form-grid"><label class="field">Name<input name="name" maxlength="80" required value="'+esc(meta.name)+'"></label><label class="field">Short description<textarea name="summary" maxlength="240">'+esc(meta.summary||'')+'</textarea></label><p class="subtle">This changes the display metadata. The underlying identity stays unchanged.</p><details><summary>Identity</summary><code>'+esc(id)+'</code></details></div>',async form=>{
  const metadata={...meta,name:form.get('name').trim(),summary:form.get('summary').trim()};
  await request('/dashboard/mappings',{realm:el('realm').value,kind,identity:id,metadata});
  mappings=await readMappings();const instance=n('instance',snapshot.site?.site,{title:'My Biome'});el('instance-name').textContent=instance;el('top-instance').textContent=instance;await render();toast('Display name saved');
 });
}
function schemaForm(schema,defaults={}){
 return '<div class="form-grid">'+Object.entries(schema||{}).map(([key,def])=>{
  const type=Array.isArray(def)?'choice':typeof def==='object'?def.type:String(def).replace(/\?$/,'');
  const required=typeof def==='object'?!!def.required:typeof def==='string'?!def.endsWith('?'):true;
  const value=defaults[key]!==undefined?defaults[key]:(type==='obj'?{}:type==='list'?[]:'');
  const serialized=typeof value==='object'?JSON.stringify(value,null,2):String(value);
  const attr=' name="'+esc(key)+'" data-value-type="'+esc(type)+'"'+(required?' required':'');
  const input=type==='choice'?'<select'+attr+'>'+def.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join('')+'</select>':type==='bool'?'<select'+attr+'><option value="true">Yes</option><option value="false">No</option></select>':['int','num'].includes(type)?'<input type="number" step="'+(type==='int'?'1':'any')+'"'+attr+' value="'+esc(serialized)+'">':'<textarea'+attr+'>'+esc(serialized)+'</textarea>';
  return '<label class="field">'+esc(friendlyIdentity(key))+(required?'':' <span class="subtle">(optional)</span>')+input+(['obj','list','any'].includes(type)?'<small>Structured value (JSON)</small>':'')+'</label>';
 }).join('')+'</div>';
}
function schemaValues(){
 const result={};for(const input of el('modal-body').querySelectorAll('[data-value-type]')){
  const type=input.dataset.valueType;if(!input.value&&!input.required)continue;
  result[input.name]=['obj','list','any','bool'].includes(type)?JSON.parse(input.value):['int','num'].includes(type)?Number(input.value):input.value;
 }return result;
}
function startWorkflow(name,task=''){
 const def=(snapshot.process?.definitions||[]).find(d=>d.id===name);if(!def)throw Error('Choose an available workflow.');
 openModal('Start '+n('workflow',name),schemaForm(def.inputs),async()=>{const run=await call('process.start',{name,inputs:schemaValues(),task});await refresh();go('runs',run.id);toast('Process started');},'Start process');
}
function chooseWorkflow(task=''){
 const defs=snapshot.process?.definitions||[];if(!defs.length){openModal('No workflows available','<p>Publish a workflow before starting a process.</p>');return;}
 openModal('Start a process','<label class="field">Workflow<select name="workflow">'+defs.map(d=>'<option value="'+esc(d.id)+'">'+esc(n('workflow',d.id))+'</option>').join('')+'</select></label>',async form=>{const name=form.get('workflow');el('modal').close();setTimeout(()=>startWorkflow(name,task),0);},'Continue');
 // Keep the input form open across the dialog's close/continue transition.
 const base=el('modal-form').onsubmit;el('modal-form').onsubmit=base;
}
function editWork(item){
 const items=snapshot.work||[];
 const kinds=Object.keys(workParents);
 const opts=(values,selected)=>values.map(([id,title])=>
  '<option value="'+esc(id)+'"'+(selected.includes(id)?' selected':'')+'>'+esc(title)+'</option>').join('');
 const dependencies=items.filter(w=>w.id!==item?.id).map(w=>[w.id,w.title+' · '+friendlyIdentity(w.kind)]);
 const kindField=item?'':'<label class="field">Type<select name="kind">'+opts(kinds.map(k=>[k,friendlyIdentity(k)]),['task'])+'</select></label>';
 const depField=(name,label)=>'<label class="field">'+label+'<select name="'+name+'" multiple>'+
  opts(dependencies,item?.[name]||[])+'</select></label>';
 openModal(item?'Edit work':'Add work','<div class="form-grid">'+kindField+
  '<label class="field">Title<input name="title" maxlength="500" required value="'+esc(item?.title)+'"></label>'+
  '<label class="field">Parent<select name="parent"></select></label>'+
  '<label class="field">Priority<input name="priority" type="number" required value="'+(item?.priority??0)+'"></label>'+
  '<label class="field">Order<input name="sequence" type="number" required value="'+(item?.sequence??0)+'"></label>'+
  depField('dependencies','Technical prerequisites')+depField('sequence_dependencies','Preferred predecessors')+'</div>',
  async form=>{
   const changes={title:form.get('title'),parent:form.get('parent'),priority:Number(form.get('priority')),
    sequence:Number(form.get('sequence')),dependencies:form.getAll('dependencies'),
    sequence_dependencies:form.getAll('sequence_dependencies')};
   if(item)await call('work.update',{id:item.id,revision:item.revision,changes});
   else await call('work.create',{kind:form.get('kind'),...changes});
   await refresh();
   toast(item?'Work saved':'Work added');
  });
 const form=el('modal-form');
 const kind=form.elements.namedItem('kind');
 const parent=form.elements.namedItem('parent');
 const fill=selected=>{parent.innerHTML=opts([['','Unassigned'],...items
  .filter(w=>w.kind===workParents[item?.kind||kind.value]).map(w=>[w.id,w.title])],[selected]);};
 fill(item?.parent||'');
 if(kind)kind.onchange=()=>fill('');
}

function showWorkMap(id){
 const items=snapshot.work||[];
 const work=items.find(item=>item.id===id);
 if(!work)return;
 const graph=workDependencyGraph(items,id);
 const diagram=diagramMarkup(graph,{selected:id,statusLabel:'Planning item'});
 openModal('Dependency map · '+work.title,
  '<div id="work-dependency-map">'+diagramToolbar+'<div class="diagram-viewport">'+diagram.html+'</div></div>');
 wireDiagram(el('work-dependency-map'),diagram.layout,workDetail);
}
async function workDetail(id){
 const w=(snapshot.work||[]).find(x=>x.id===id);if(!w)return;
 openModal(w.title,'<div class="summary-row">'+field('Type',esc(friendlyIdentity(w.kind)))+field('Status',pill(w.state))+'</div><p>'+esc(w.description||(w.ready?'This work is ready to continue.':'Review its current state and results.'))+'</p><div class="inline-actions">'+button('Edit work','edit-work',id)+button('Dependency map','map-work',id)+(w.ready&&w.kind==='task'?button('Run workflow','run-work',id):'')+(['open','review'].includes(w.state)?button('Review and accept','accept-work',id):'')+'</div>'+raw(w));
}
async function acceptWork(id){
 const w=(snapshot.work||[]).find(x=>x.id===id);const schema=Object.fromEntries((w.criteria?.length?w.criteria:['outcome']).map(key=>[key,'str']));
 openModal('Accept '+w.title,schemaForm(schema),async()=>{await call('work.accept',{id,revision:w.revision,evidence:schemaValues()});await refresh();toast('Work accepted');},'Accept work');
}
async function documentDetail(id){
 const value=await call('knowledge.get',{key:id});
 const content=(value.revisions||[]).map(rev=>'<section><p class="subtle">Saved version '+esc(shortID(rev.id||rev.revision||''))+'</p><pre>'+esc(typeof rev.body==='string'?rev.body:JSON.stringify(rev.body,null,2))+'</pre></section>').join('');
 openModal(n('document',id),content+'<div class="inline-actions">'+button('Edit document','edit-document',id)+button('Edit name','rename-document',id)+'</div>'+raw(value));
}
async function editDocument(id=''){
 const value=id?await call('knowledge.get',{key:id}):null,prior=(snapshot.documents||[]).find(x=>x.id===id);
 if(prior?.conflict){go('advanced');el('modal').close();setTimeout(()=>{el('service-operation').value='knowledge.put';el('service-arguments').value=JSON.stringify({key:id,parents:prior.heads,body:''},null,2);serviceHelp();say('Review all saved versions, then provide the resolved document in Advanced tools.');},0);return;}
 const body=value?.revisions?.[0]?.body||'';
 openModal(id?'Edit document':'Add document','<div class="form-grid"><label class="field">Document key<input name="key" required value="'+esc(id)+'"'+(id?' readonly':'')+'></label><label class="field">Content<textarea name="body" rows="10">'+esc(typeof body==='string'?body:JSON.stringify(body,null,2))+'</textarea></label></div>',async form=>{await call('knowledge.put',{key:form.get('key'),body:form.get('body'),parents:prior?.heads||[]});await refresh();toast('Document saved');});
}
el('view').onclick=action(async event=>{
 const filterButton=event.target.closest('[data-filter]');if(filterButton){filter=filterButton.dataset.filter;await render();return;}
 const runRow=event.target.closest('[data-run]');if(runRow){selectedNode='';go('runs',runRow.dataset.run);return;}
 const workRow=event.target.closest('[data-open-work]');if(workRow){await workDetail(workRow.dataset.openWork);return;}
 const target=event.target.closest('[data-action]');if(target)await handle(target.dataset.action,target.dataset.id||'');
});
el('view').onkeydown=event=>{if(event.key==='Enter'&&(event.target.hasAttribute('data-run')||event.target.hasAttribute('data-open-work')))event.target.click();};
el('modal-body').onclick=action(async event=>{const target=event.target.closest('[data-action]');if(target)await handle(target.dataset.action,target.dataset.id||'');});
async function handle(action,id){
 if(['overview','workflows','runs','resources'].includes(action)){go(action);return;}
 if(action==='workflow'){selectedNode='';go('workflows',id);return;}
 if(action==='run'){selectedNode='';go('runs',id);return;}
 if(action==='namespace'){go('capabilities',id);return;}
 if(action==='capability'){go('capability',id);return;}
 if(action==='rename-step'){await editStepName(id);return;}
 if(action.startsWith('rename-')){await editMapping(action.slice(7),id);return;}
 if(action==='edit-mapping'){const separator=id.indexOf('|');await editMapping(id.slice(0,separator),id.slice(separator+1));return;}
 if(action==='retry-view'){await render();return;}
 if(action==='refresh-run'){runCache.delete(id);await refresh();return;}
 if(action==='start-workflow'){startWorkflow(id);return;}
 if(action==='cancel-run'){openModal('Cancel this run?','<p>The run will stop and release its held resources.</p>',async()=>{await call('process.cancel',{run:id});runCache.delete(id);await refresh();toast('Run cancelled');},'Cancel run');return;}
 if(action==='decision'){const wait=(snapshot.waits||[]).find(w=>w.id===id);openModal(decisionTitle(wait),'<p>'+esc(wait.question||wait.name)+'</p>'+schemaForm(wait.answer_schema),async()=>{await call('process.answer',{wait:id,answer:schemaValues()});await refresh();toast('Response submitted');},'Submit response');return;}
 if(action==='resource'){const pool=resourcePools(snapshot).find(p=>p.id===id);openModal(n('resource',id,{title:friendlyIdentity(pool.offers[0]?.service)}),'<div class="summary-row">'+field('Allocated',pool.allocated)+field('Capacity',pool.capacity||'Not reported')+field('Queued',pool.queued)+'</div>'+button('Edit name','rename-resource',id)+raw(pool));return;}
 if(action==='new-work'||action==='edit-work'){editWork(action==='edit-work'?snapshot.work.find(w=>w.id===id):null);return;}
 if(action==='inspect-work'){await workDetail(id);return;}
 if(action==='map-work'){showWorkMap(id);return;}
 if(action==='run-work'){chooseWorkflow(id);return;}
 if(action==='accept-work'){await acceptWork(id);return;}
 if(action==='document'){await documentDetail(id);return;}
 if(action==='edit-document'||action==='new-document'){await editDocument(id);return;}
 if(action==='controller'){const value=await call('cluster.status',{},true);openModal('Controller status','<div class="summary-row">'+field('Active controller',esc(friendlyIdentity(value.primary||'Unknown')))+field('Cluster',pill(value.enabled?'ready':'unknown'))+'</div>'+raw(value));return;}
 if(action==='demo'){const result=await call('demo.run');await refresh();openModal('Demonstration started','<p>The result is available for review in the work plan.</p>'+raw(result));return;}
 if(action==='enrollment'){const candidate=(snapshot.candidates||[]).find(x=>x.id===id);openModal(candidate.label||'Review enrollment','<p>Review this provider and its requested access before commissioning it.</p>'+raw(candidate),async()=>{el('modal').close();go('advanced');setTimeout(()=>{el('service-operation').value='pair.approve';el('service-arguments').value=JSON.stringify({fingerprint:id,scopes:[]},null,2);serviceHelp();},0);},'Prepare commissioning');return;}
}
el('login').onsubmit=connectDashboard;
el('connect').disabled=false;
el('refresh').onclick=action(async()=>{el('refresh').disabled=true;try{runCache.clear();await refresh();say('Connected');}finally{el('refresh').disabled=false;}});
el('realm').onchange=action(async()=>{definitionCache.clear();runCache.clear();await refresh();say('Connected');});
el('start-process').onclick=()=>chooseWorkflow();
el('search').oninput=()=>{if(connected)render();};
el('modal-close').onclick=()=>el('modal').close();el('modal-cancel').onclick=()=>el('modal').close();
document.querySelectorAll('nav [data-view]').forEach(b=>b.onclick=()=>{filter='all';selectedNode='';el('search').value='';go(b.dataset.view);});
window.addEventListener('hashchange',()=>{if(connected)render();});
connectDashboard();

async function editStepName(identity){
 const [workflow,revision,id]=identity.split('|');
 const metadata=m('workflow',workflow),record=await loadDefinition(workflow,revision),node=graphModel(record).nodes.find(n=>n.id===id);
 const key=revision+':'+id,current=metadata.steps?.[key]||metadata.steps?.[id]||{};
 openModal('Edit step name','<div class="form-grid"><label class="field">Step name<input name="name" maxlength="80" required value="'+esc(current.name||nodeLabel(node,workflow,revision))+'"></label><label class="field">Purpose<textarea name="summary" maxlength="240">'+esc(current.summary||'')+'</textarea></label><p class="subtle">This name is attached to the step mapping in this workflow revision.</p></div>',async form=>{
  const changed={...metadata,steps:{...(metadata.steps||{}),[key]:{name:form.get('name').trim(),summary:form.get('summary').trim()}}};
  await request('/dashboard/mappings',{realm:el('realm').value,kind:'workflow',identity:workflow,metadata:changed});
  mappings=await readMappings();await render();toast('Step name saved');
 });
}

}

function workLinks(item,byId,link){
 const groups=[['Technical',item.dependencies||[],item.blocked_by],
  ['Preferred order',item.sequence_dependencies||[],item.sequence_pending]];
 return groups.filter(([,ids])=>ids.length).map(([label,ids,pending])=>
  '<div><small>'+label+'</small>'+ids.map(id=>{
   const dep=byId.get(id);
   return (dep?link(dep):esc(id))+(pending?.includes(id)?' <small>Pending</small>':'');
  }).join(' ')+'</div>').join('');
}
