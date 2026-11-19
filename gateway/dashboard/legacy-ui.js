import {escapeHTML as esc,shortID,friendlyIdentity,mappingFor,nameFor,mappingKey,namespaceTree,namespaceAt,descendantCount,statusName,statusTone,runTitle,overviewStats,resourcePools,graphModel,elapsedLabel,createClient} from './model.mjs';
import {diagramMarkup,wireDiagram,diagramToolbar} from './diagram.mjs';
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
 const items=(snapshot.work||[]).filter(w=>match(w.title,w.kind,w.state));
 return '<section class="panel"><div class="panel-head"><h2>Goals and work</h2>'+button('＋ Add work','new-work')+'</div>'+(items.length?'<div class="table-scroll"><table><thead><tr><th>WORK</th><th>TYPE</th><th>STATUS</th><th>NEXT ACTION</th></tr></thead><tbody>'+items.map(w=>'<tr data-open-work="'+esc(w.id)+'" tabindex="0"><td><strong>'+esc(w.title)+'</strong>'+(w.parent?'<small>'+esc((snapshot.work||[]).find(p=>p.id===w.parent)?.title||'Parent work')+'</small>':'')+'</td><td>'+esc(friendlyIdentity(w.kind))+'</td><td>'+pill(w.state)+'</td><td>'+esc(w.ready?'Ready to start':w.state==='review'?'Review the result':'View work')+'</td></tr>').join('')+'</tbody></table></div>':empty('No work yet','Add a goal, milestone, workflow, or task to organize your work.'))+'</section>';
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
async function workDetail(id){
 const w=(snapshot.work||[]).find(x=>x.id===id);if(!w)return;
 openModal(w.title,'<div class="summary-row">'+field('Type',esc(friendlyIdentity(w.kind)))+field('Status',pill(w.state))+'</div><p>'+esc(w.description||(w.ready?'This work is ready to continue.':'Review its current state and results.'))+'</p><div class="inline-actions">'+(w.ready&&w.kind==='task'?button('Run workflow','run-work',id):'')+(['open','review'].includes(w.state)?button('Review and accept','accept-work',id):'')+'</div>'+raw(w));
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
 if(action==='new-work'){openModal('Add work','<div class="form-grid"><label class="field">Type<select name="kind"><option value="goal">Goal</option><option value="milestone">Milestone</option><option value="workflow">Workflow</option><option value="task">Task</option></select></label><label class="field">Title<input name="title" required></label><label class="field">Parent<select name="parent"><option value="">No parent</option>'+(snapshot.work||[]).filter(x=>x.kind!=='task').map(x=>'<option value="'+esc(x.id)+'">'+esc(x.title)+'</option>').join('')+'</select></label></div>',async form=>{await call('work.create',{kind:form.get('kind'),title:form.get('title'),parent:form.get('parent')});await refresh();toast('Work added');},'Add work');return;}
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
