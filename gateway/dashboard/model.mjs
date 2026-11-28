export const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const shortID=id=>String(id??'').slice(0,8);
const words={agent:'Agents',agents:'Agents',chatgpt:'ChatGPT',grok:'Grok',lmstudio:'LM Studio',tools:'Tools',host:'Hosts',terminal:'Terminal',docker:'Docker',storage:'Storage',workspace:'Workspaces',git:'Source control',image:'Images',human:'People',decision:'Decisions',encrypted:'Encrypted',file:'Files',namespace:'Namespaces',rs485:'RS-485',esp32:'ESP32'};
export function friendlyIdentity(id) {
 const text=String(id??'').replace(/:\d+$/,'');
 if(!text)return 'Untitled';
 const part=text.split('.').pop();
 if(words[part.toLowerCase()])return words[part.toLowerCase()];
 if(/^[a-f0-9]{24,}$/i.test(part))return 'Provider';
 return part.replace(/([a-z])([A-Z])/g,'$1 $2').split(/[-_\s]+/).filter(Boolean).map((s,i)=>words[s.toLowerCase()]||s.charAt(0).toUpperCase()+s.slice(1)).join(' ');
}
export const mappingKey=(kind,id)=>kind+':'+id;
export function mappingFor(mappings,kind,id,source={}) {
 const found=mappings[mappingKey(kind,id)];
 return {kind,identity:id,metadata:{name:source.title||source.label||friendlyIdentity(id),summary:source.description||'',...(found?.metadata||{})}};
}
export const nameFor=(mappings,kind,id,source)=>mappingFor(mappings,kind,id,source).metadata.name;
export function namespaceTree(contracts) {
 const root={identity:'',children:new Map(),contracts:[]};
 for(const contract of contracts||[]){
  const identity=contract.id||contract.name;if(!identity)continue;
  let node=root,path=[];
  for(const part of identity.replace(/:\d+$/,'').split('.')){
   path.push(part);if(!node.children.has(part))node.children.set(part,{identity:path.join('.'),children:new Map(),contracts:[]});
   node=node.children.get(part);
  }
  node.contracts.push(contract);
 }
 return root;
}
export function namespaceAt(root,path='') {
 let node=root;for(const part of path.split('.').filter(Boolean)){node=node.children.get(part);if(!node)return null;}return node;
}
export function descendantCount(node){return node.contracts.length+[...node.children.values()].reduce((n,c)=>n+descendantCount(c),0);}
export function statusName(state){
 return ({running:'Running',open:'Ready',completed:'Completed',succeeded:'Completed',failed:'Failed',cancelled:'Cancelled',review:'Ready for review',accepted:'Accepted',ready:'Available',bound:'Allocated',queued:'Queued',fencing:'Preparing',released:'Released',waiting:'Waiting',waiting_event:'Waiting',uncertain:'Needs review',paused:'Paused'})[state]||friendlyIdentity(state||'Unknown');
}
export function statusTone(state){return ['failed','uncertain','error','blocked'].includes(state)?'danger':['waiting','waiting_event','queued','fencing','review','recovering'].includes(state)?'warning':['completed','succeeded','accepted','ready'].includes(state)?'success':'neutral';}
export function runTitle(run,snapshot,mappings){
 const custom=mappings[mappingKey('run',run.id)]?.metadata?.name;if(custom)return custom;
 const task=(snapshot.work||[]).find(x=>x.id===run.task);
 return task?.title||nameFor(mappings,'workflow',run.name);
}
export function overviewStats(snapshot) {
 const runs=snapshot.process?.runs||[],completed=runs.filter(x=>x.state==='completed').length,failed=runs.filter(x=>x.state==='failed').length;
 return {active:runs.filter(x=>['running','waiting','paused','open'].includes(x.state)).length,completed,attention:(snapshot.waits||[]).length+failed,rate:completed+failed?Math.round(100*completed/(completed+failed)):null};
}
export function resourcePools(snapshot) {
 const pools=new Map();
 for(const offer of snapshot.capabilities?.offers||[]){
  const id=offer.resource||offer.provider||offer.id;
  if(!pools.has(id))pools.set(id,{id,offers:[],contracts:new Set(),providers:new Set(),capacity:0,allocated:0,queued:0,states:new Set()});
  const pool=pools.get(id);pool.offers.push(offer);pool.contracts.add(offer.contract);pool.providers.add(offer.provider);pool.states.add(offer.state);
  if(Number.isFinite(offer.capacity))pool.capacity=Math.max(pool.capacity,offer.capacity);
 }
 for(const lease of snapshot.leases||[]){
  const pool=pools.get(lease.resource||lease.requested_resource);if(!pool)continue;
  if(lease.state==='bound')pool.allocated++;
  if(['queued','fencing'].includes(lease.state))pool.queued++;
 }
 return [...pools.values()].map(p=>({...p,contracts:[...p.contracts],providers:[...p.providers],states:[...p.states]}));
}
export function graphModel(record) {
 const graph=record?.graph||record?.definition?.graph||record||{};
 let nodes=graph.nodes||{};
 if(Array.isArray(nodes))nodes=Object.fromEntries(nodes.map((n,i)=>[n.id||'step-'+i,n]));
 const result=Object.entries(nodes).map(([id,node])=>({id,...node,type:node.type||'task'}));
 const edges=[];const seen=new Set();
 function add(from,to,label=''){if(typeof to!=='string'||!nodes[to])return;const key=from+'>'+to+':'+label;if(!seen.has(key)){seen.add(key);edges.push({from,to,label});}}
 for(const node of result){
  for(const key of ['next','then','else','error','on_error','catch','default','join']){
   const value=node[key];if(typeof value==='string')add(node.id,value,key==='next'?'':key==='then'?'Yes':key==='else'?'No':key==='error'||key==='on_error'||key==='catch'?'Recovery':key);
  }
  for(const key of ['branches','targets','outgoing']){
   const value=node[key];if(Array.isArray(value))for(const item of value)add(node.id,typeof item==='string'?item:item.target||item.next,item.label||'');
  }
  for(const item of Array.isArray(node.choices)?node.choices:[])add(node.id,item.target||item.next||item.to,item.label||'');
  for(const [index,item] of (Array.isArray(node.cases)?node.cases:[]).entries())add(node.id,item.to||item.target||item.next,item.label||'Condition '+(index+1));
  for(const target of Array.isArray(node.paths)?node.paths:[])add(node.id,typeof target==='string'?target:target.to||target.target,'Parallel path');
 }
 for(const edge of graph.edges||[])add(edge.from||edge.source,edge.to||edge.target,edge.label||'');
 return {nodes:result,edges,start:graph.start||result.find(n=>n.type==='start')?.id||result[0]?.id};
}
export function layoutGraph(graph,{direction='vertical'}={}) {
 const horizontal=direction==='horizontal',labelWidth=Math.max(40,...graph.edges.filter(e=>e.label).map(e=>String(e.label).length*6.8+24));
 const W=horizontal?132:264,H=horizontal?264:132,GAP=64,PAD=76,TRACK=horizontal?labelWidth+12:30,INSET=horizontal?labelWidth/2+12:26;
 const ids=new Set(graph.nodes.map(n=>n.id)),index=new Map(graph.nodes.map((n,i)=>[n.id,i]));
 const edges=graph.edges.filter(e=>ids.has(e.from)&&ids.has(e.to)).map((e,i)=>({...e,key:i}));
 const outgoing=new Map(graph.nodes.map(n=>[n.id,edges.filter(e=>e.from===n.id)]));
 const colour=new Map(),feedback=new Set();
 function visit(id){colour.set(id,1);for(const e of outgoing.get(id)||[]){if(colour.get(e.to)===1)feedback.add(e.key);else if(!colour.has(e.to))visit(e.to);}colour.set(id,2);}
 if(ids.has(graph.start))visit(graph.start);for(const n of graph.nodes)if(!colour.has(n.id))visit(n.id);
 const forward=edges.filter(e=>!feedback.has(e.key)),incoming=new Map(graph.nodes.map(n=>[n.id,0])),rank=new Map(graph.nodes.map(n=>[n.id,0]));
 for(const e of forward)incoming.set(e.to,incoming.get(e.to)+1);
 const queue=graph.nodes.filter(n=>!incoming.get(n.id)).map(n=>n.id);
 for(let i=0;i<queue.length;i++){const id=queue[i];for(const e of outgoing.get(id)||[]){if(feedback.has(e.key))continue;rank.set(e.to,Math.max(rank.get(e.to),rank.get(id)+1));incoming.set(e.to,incoming.get(e.to)-1);if(!incoming.get(e.to))queue.push(e.to);}}
 const levels=new Map();for(const n of graph.nodes){const r=rank.get(n.id);if(!levels.has(r))levels.set(r,[]);levels.get(r).push(n);}
 const ranks=[...levels.keys()].sort((a,b)=>a-b),order=new Map();
 function positions(){for(const r of ranks)levels.get(r).forEach((n,i)=>order.set(n.id,i));}positions();
 // Barycentric sweeps put related branches together before routing their links.
 for(let sweep=0;sweep<6;sweep++)for(const r of sweep%2?[...ranks].reverse():ranks){const neighbors=id=>forward.filter(e=>sweep%2?e.from===id:e.to===id).map(e=>order.get(sweep%2?e.to:e.from));const score=id=>{const ns=neighbors(id);return ns.length?ns.reduce((a,b)=>a+b,0)/ns.length:order.get(id);};levels.get(r).sort((a,b)=>score(a.id)-score(b.id)||index.get(a.id)-index.get(b.id));positions();}
 const slots=new Map();function reserve(g){const i=slots.get(g)||0;slots.set(g,i+1);return i;}
 for(const e of edges){const a=rank.get(e.from),b=rank.get(e.to);e.feedback=feedback.has(e.key)||b<=a;e.external=e.feedback||b>a+1;e.exitSlot=reserve(a);e.entrySlot=e.external?reserve(b-1):e.exitSlot;}
 const rowY=new Map();let y=Math.max(PAD,(slots.get(-1)||0)*TRACK+44);
 for(const r of ranks){rowY.set(r,y);y+=H+Math.max(92,(slots.get(r)||0)*TRACK+52);}
 const maxColumns=Math.max(1,...[...levels.values()].map(a=>a.length)),contentWidth=Math.max(horizontal?340:620,maxColumns*W+(maxColumns-1)*GAP);
 const external=edges.filter(e=>e.external);external.forEach((e,i)=>{e.rail=i;});
 const left=PAD,right=PAD+contentWidth,railWidth=external.length?60+external.length*26:0,width=Math.max(horizontal?480:760,right+railWidth+PAD);
 const nodes=[];for(const r of ranks){const row=levels.get(r),rowWidth=row.length*W+(row.length-1)*GAP;row.forEach((n,i)=>nodes.push({...n,x:left+(contentWidth-rowWidth)/2+i*(W+GAP),y:rowY.get(r),w:W,h:H,rank:r}));}
 const byId=new Map(nodes.map(n=>[n.id,n])),outs=new Map(),ins=new Map();
 for(const n of nodes){outs.set(n.id,edges.filter(e=>e.from===n.id).sort((a,b)=>byId.get(a.to).x-byId.get(b.to).x||a.key-b.key));ins.set(n.id,edges.filter(e=>e.to===n.id).sort((a,b)=>byId.get(a.from).x-byId.get(b.from).x||a.key-b.key));}
 const port=(n,group,e)=>n.x+n.w*(.25+.5*(group.indexOf(e)+1)/(group.length+1));
 const compact=points=>points.filter((p,i,a)=>!i||p.x!==a[i-1].x||p.y!==a[i-1].y).filter((p,i,a)=>!i||i===a.length-1||!((a[i-1].x===p.x&&p.x===a[i+1].x)||(a[i-1].y===p.y&&p.y===a[i+1].y)));
 function rounded(points){let d='M '+points[0].x+' '+points[0].y;for(let i=1;i<points.length-1;i++){const a=points[i-1],p=points[i],b=points[i+1],r=Math.min(12,Math.hypot(p.x-a.x,p.y-a.y)/2,Math.hypot(b.x-p.x,b.y-p.y)/2),before={x:p.x-Math.sign(p.x-a.x)*r,y:p.y-Math.sign(p.y-a.y)*r},after={x:p.x+Math.sign(b.x-p.x)*r,y:p.y+Math.sign(b.y-p.y)*r};d+=' L '+before.x+' '+before.y+' Q '+p.x+' '+p.y+' '+after.x+' '+after.y;}const last=points.at(-1);return d+' L '+last.x+' '+last.y;}
 const routed=edges.map(e=>{const a=byId.get(e.from),b=byId.get(e.to),sx=port(a,outs.get(a.id),e),tx=port(b,ins.get(b.id),e),exitY=a.y+H+INSET+e.exitSlot*TRACK;
  let points,labelX,labelY;if(e.external){const rail=right+44+e.rail*26,previousRank=b.rank-1,entryBase=rowY.has(previousRank)?rowY.get(previousRank)+H:18,entryY=entryBase+INSET+e.entrySlot*TRACK;points=[{x:sx,y:a.y+H},{x:sx,y:exitY},{x:rail,y:exitY},{x:rail,y:entryY},{x:tx,y:entryY},{x:tx,y:b.y}];labelX=Math.max(sx+32,(sx+rail)/2);labelY=exitY;}
  else{points=[{x:sx,y:a.y+H},{x:sx,y:exitY},{x:tx,y:exitY},{x:tx,y:b.y}];labelX=(sx+tx)/2;labelY=exitY;}
  points=compact(points);const label=e.label==='default'?'Otherwise':e.label;return {...e,label,points,path:rounded(points),labelBox:label?{x:labelX,y:labelY,w:Math.max(58,label.length*6.8+24),h:22}:null};});
 if(horizontal)return {direction,nodes:nodes.map(n=>({...n,x:n.y,y:n.x,w:n.h,h:n.w})),edges:routed.map(e=>{const points=e.points.map(p=>({x:p.y,y:p.x}));return {...e,points,path:rounded(points),labelBox:e.labelBox?{...e.labelBox,x:e.labelBox.y,y:e.labelBox.x}:null};}),width:Math.max(760,y+24),height:width};
 return {direction,nodes,edges:routed,width,height:Math.max(360,y+24)};
}

export function nodeStates(run) {
 const states={};
 for(const event of run?.trace||[]){const id=event.node||event.node_id||event.step;if(id)states[id]=['failed','error'].includes(event.state||event.status)?'failed':'completed';}
 for(const token of run?.tokens||[]){const id=token.node||token.at||token.node_id;if(id)states[id]=/wait|queue|pending|blocked/.test(String(token.state||token.phase||''))?'waiting':run.state==='failed'?'failed':'active';}
 return states;
}
export function elapsedLabel(run) {
 const start=run.started_at||run.created_at||run.started;const end=run.completed_at||run.finished_at;
 if(!start)return 'Not recorded';
 const seconds=v=>typeof v==='number'?(v>1e12?v/1000:v):Date.parse(v)/1000;
 const duration=Math.max(0,(end?seconds(end):Date.now()/1000)-seconds(start));
 if(!Number.isFinite(duration))return 'Not recorded';
 return duration<60?Math.round(duration)+' sec':duration<3600?Math.floor(duration/60)+' min':Math.floor(duration/3600)+' hr '+Math.floor(duration%3600/60)+' min';
}
export function createClient(fetcher=fetch,onStatus=()=>{},getRealm=()=> 'site') {
 return async function call(operation,args={},cached=false) {
  const id=crypto.randomUUID(),body=JSON.stringify({id,operation,arguments:args,realm:getRealm(),cached});
  for(;;){
   const response=await fetcher('/dashboard/api',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body});
   let data;try{data=await response.json()}catch{throw Error('The controller response was interrupted. Refresh to reconnect.')}
   if(response.status===504){onStatus('Waiting for controller…');await new Promise(r=>setTimeout(r,1000));continue;}
   if(response.status===401)throw Error('Your session expired. Reload to sign in again.');
   if(!response.ok)throw Error(data.error||'The controller could not complete this action.');
   return data.result;
  }
 };
}

export const workParents={goal:'',milestone:'goal',workflow:'milestone',task:'workflow'};

export function workHierarchy(items){
 const byId=new Map(items.map(item=>[item.id,item]));
 const children=new Map();
 for(const item of [...items].sort((a,b)=>
  (b.priority||0)-(a.priority||0)||(a.sequence||0)-(b.sequence||0)||a.title.localeCompare(b.title))){
  const parent=byId.has(item.parent)?item.parent:'';
  if(!children.has(parent))children.set(parent,[]);
  children.get(parent).push(item);
 }
 const rows=[];
 function visit(item,ancestors){
  const row={item,ancestors,accepted:0,total:0};
  rows.push(row);
  if(item.kind==='task'){row.total=1;row.accepted=Number(item.state==='accepted');}
  for(const child of children.get(item.id)||[]){
   const result=visit(child,[...ancestors,item]);
   row.total+=result.total;
   row.accepted+=result.accepted;
  }
  return row;
 }
 for(const root of children.get('')||[])visit(root,[]);
 return {byId,rows};
}

export function workDependencyGraph(items,focus){
 const byId=new Map(items.map(item=>[item.id,item]));
 const scope=new Set([focus]);
 for(const {item,ancestors} of workHierarchy(items).rows){
  if(ancestors.some(parent=>parent.id===focus))scope.add(item.id);
 }
 const nodes=new Set(scope),edges=[];
 for(const item of items){
  for(const [ids,label] of [[item.dependencies,'Technical'],[item.sequence_dependencies,'Preferred order']]){
   for(const from of ids||[]){
    if(!byId.has(from)||(!scope.has(item.id)&&!scope.has(from)))continue;
    nodes.add(from);
    nodes.add(item.id);
    edges.push({from,to:item.id,label});
   }
  }
 }
 return {nodes:[...nodes].map(id=>({id,name:byId.get(id).title,type:byId.get(id).kind})),edges};
}
