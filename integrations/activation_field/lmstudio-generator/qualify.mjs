// Exercises the installed generator through LM Studio, not an in-process mock.
import {LMStudioClient} from '@lmstudio/sdk';
import {readFile,writeFile} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const root=join(homedir(),'.local/share/biome-activation-field');
const apiToken=(await readFile(join(homedir(),'.local/share/biome-lmstudio/api-token'),'utf8')).trim();
const client=new LMStudioClient({apiToken});
const generator=client.plugins.createGeneratorHandle('biome/activation-field');
const label='qualification-'+randomUUID();
const token=(await readFile(join(root,'api-token'),'utf8')).trim();
const headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'};
const evidence={status:'running',label,checks:{}};
const save=()=>writeFile(join(root,'plugin-qualification.json'),JSON.stringify(evidence,null,2));
async function api(path,body,method){
 const response=await fetch('http://127.0.0.1:8791'+path,{headers,method:method??(body?'POST':'GET'),body:body?JSON.stringify(body):undefined});
 assert.ok(response.ok,await response.clone().text());return response.json();
}
function config(session,extra={}){return {fields:Object.entries({session,enabled:false,maxTokens:3,...extra}).map(([key,value])=>({key,value}))};}
async function mapping(name){return JSON.parse(await readFile(join(root,'plugin-sessions',createHash('sha256').update(name).digest('hex')+'.json'),'utf8'));}
await save();
try{
 let fragments=0;
 const first=await generator.respond([{role:'user',content:'Say hello.'}],{pluginConfig:config(label),onPredictionFragment:()=>fragments++});
 assert.ok(first.content.length>0);assert.ok(fragments>0);
 evidence.checks.actual_lmstudio_stream=true;evidence.first_response=first.content;await save();
 const a=await mapping(label),sa=await api('/sessions/'+a.session_id);
 assert.equal(sa.config.enabled,false);
 const secondLabel=label+'-second';
 const second=await generator.respond([{role:'user',content:'/field diagnostics'}],{pluginConfig:config(secondLabel)});
 assert.ok(second.content.includes('Field session: '+secondLabel));
 const b=await mapping(secondLabel);assert.notEqual(a.session_id,b.session_id);
 const sb=await api('/sessions/'+b.session_id);assert.equal(sb.committed_tokens,0);
 evidence.checks.session_switch_isolated=true;await save();
 let cancelFragments=0;const controller=new AbortController();
 try{await generator.respond([{role:'user',content:'Count from one to one hundred.'}],{pluginConfig:config(secondLabel,{maxTokens:100}),signal:controller.signal,onPredictionFragment:()=>{cancelFragments++;controller.abort();}});}catch(error){if(!controller.signal.aborted)throw error;}
 assert.ok(cancelFragments>0);
 let state=await api('/sessions/'+b.session_id);
 while(state.busy){await new Promise(resolve=>setTimeout(resolve,100));state=await api('/sessions/'+b.session_id);}
 evidence.checks.actual_lmstudio_cancellation=true;evidence.cancelled_session_version=state.version;
 const unchanged=await api('/sessions/'+a.session_id);assert.equal(unchanged.committed_tokens,sa.committed_tokens);
 evidence.checks.other_session_preserved=true;
 for(const sid of [a.session_id,b.session_id]){const s=await api('/sessions/'+sid);await api('/sessions/'+sid,{request_id:randomUUID(),expected_version:s.version},'DELETE');}
 evidence.status='passed';await save();console.log(JSON.stringify(evidence));
}catch(error){evidence.status='failed';evidence.error=String(error);await save();throw error;}
