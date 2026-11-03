'use strict';
const el = id => document.getElementById(id);
let token = '', state = {}, operations = [];
const say = s => { el('status').textContent = s; };
const detail = x => { el('detail').textContent = JSON.stringify(x, null, 2); };
async function call(name, args = {}, cached = false) {
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  const id = Array.from(bytes, b => ('0' + b.toString(16)).slice(-2)).join('');
  const r = await fetch('/v1/call', {method:'POST', headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},
    body:JSON.stringify({id,cached,realm:el('realm').value,operation:name,arguments:args})});
  const data = await r.json(); if (!r.ok) throw Error(data.error || r.status);
  return data.result;
}
function action(fn) { return async e => { if(e)e.preventDefault();try {say('');await fn();}catch(err){say(err.message);} }; }
function option(s, v, label){const o=document.createElement('option');o.value=v;o.textContent=label;s.appendChild(o);}
function rows(id, items, label, buttons) {
  const box=el(id);box.textContent='';
  if (!items.length){box.textContent='None';return;}
  items.forEach(item=>{const row=document.createElement('div');row.className='row';
    const span=document.createElement('span');span.textContent=label(item);row.appendChild(span);
    for(const [title,fn] of buttons(item)){const b=document.createElement('button');b.textContent=title;b.onclick=action(fn);row.appendChild(b);}
    box.appendChild(row);});
}
function showTab(id){for(const section of document.querySelectorAll('.tab'))section.hidden=section.id!==id;
  for(const b of document.querySelectorAll('[data-tab]'))b.classList.toggle('active',b.dataset.tab===id);}
function prepare(name,args){showTab('bus');el('operation').value=name;help();el('args').value=JSON.stringify(args,null,2);}
function help(){const op=operations.find(x=>x.name===el('operation').value);if(op)el('help').textContent=(op.description||'')+' '+Object.keys(op.inputs).map(k=>k+(op.inputs[k].required?'':'?')+' ('+op.inputs[k].type+')').join(', ');}
async function refresh(){
  try {state=await call('system.snapshot');} catch(e) {if(el('realm').value!=='site')throw e;state=await call('system.snapshot',{},true);say('Showing last synchronized site view. Select Local for offline work.');} el('identity').textContent=state.site.realm+' · '+state.site.shared_backend+' · '+state.site.site;
  const parent=el('parent');parent.textContent='';option(parent,'','No parent');state.work.filter(x=>x.kind!=='task').forEach(x=>option(parent,x.id,x.kind+' · '+x.title));
  const inspect=x=>['Inspect',()=>detail(x)];
  rows('work-list',state.work,x=>x.kind+' · '+x.title+' · '+x.state,x=>[inspect(x),['Accept',()=>prepare('work.accept',{id:x.id,revision:x.revision,evidence:Object.fromEntries((x.criteria.length?x.criteria:['outcome']).map(k=>[k,'']))})]]);
  rows('def-list',state.process.definitions,x=>x.id,x=>[['Start',()=>prepare('process.start',{name:x.id,inputs:Object.fromEntries(Object.keys(x.inputs).map(k=>[k,'']))})],inspect(x)]);
  rows('run-list',state.process.runs,x=>x.name+' · '+x.state+' · '+x.steps+' steps',x=>[['Trace',async()=>detail(await call('process.get',{run:x.id}))]]);
  rows('offer-list',state.capabilities.offers,x=>x.contract+' · '+x.provider+' · '+x.state,x=>[inspect(x)]);
  rows('lease-list',state.leases.filter(x=>x.state!=='released'),x=>x.contract+' · '+x.state,x=>[inspect(x)]);
  rows('doc-list',state.documents,x=>x.id+(x.conflict?' · concurrent edits':''),x=>[['Read',async()=>detail(await call('knowledge.get',{key:x.id}))]]);
  rows('wait-list',state.waits,x=>x.name+' · '+x.question,x=>[['Answer',()=>prepare('process.answer',{wait:x.id,answer:Object.fromEntries(Object.keys(x.answer_schema).map(k=>[k,'']))})]]);
  rows('message-list',state.inbox,x=>x.from+' → '+x.to,x=>[inspect(x)]);
  rows('candidate-list',state.candidates,x=>x.label+' · '+x.state,x=>[inspect(x),['Commission',()=>prepare('pair.approve',{fingerprint:x.id,scopes:[]})]]);
}
el('login').onsubmit=action(async()=>{token=el('token').value;operations=await call('bus.discover',{},true);await refresh();el('token').value='';
  el('operation').textContent='';operations.forEach(o=>option(el('operation'),o.name,o.name));help();el('login').hidden=true;el('workspace').hidden=false;showTab('work');});
el('logout').onclick=()=>location.reload();
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
el('refresh').onclick=action(refresh);el('realm').onchange=action(refresh);el('operation').onchange=help;
el('new-work').onsubmit=action(async()=>{detail(await call('work.create',{kind:el('kind').value,title:el('title').value,parent:el('parent').value}));el('title').value='';await refresh();});
el('new-doc').onsubmit=action(async()=>{const key=el('doc-key').value,prior=state.documents.find(x=>x.id===key);
  if(prior && prior.conflict){prepare('knowledge.put',{key,parents:prior.heads,body:el('doc-body').value});say('Inspect and resolve both versions before invoking.');return;}
  detail(await call('knowledge.put',{key,body:el('doc-body').value,parents:prior?prior.heads:[]}));await refresh();});
el('demo').onclick=action(async()=>{const result=await call('demo.run');detail(result);await refresh();say('Started. Open Processes to inspect the attempt.');});
el('call').onsubmit=action(async()=>{detail(await call(el('operation').value,JSON.parse(el('args').value)));await refresh();});
