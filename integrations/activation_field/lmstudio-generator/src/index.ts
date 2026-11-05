import {type PluginContext,type GeneratorController,type Chat} from '@lmstudio/sdk';
import {readFile,writeFile,mkdir,rename} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {configSchematics} from './config.js';
const root=join(homedir(),'.local/share/biome-activation-field');
export async function generate(ctl:GeneratorController,chat:Chat){
 const cfg=ctl.getPluginConfig(configSchematics);const label=cfg.get('session').trim();
 if(!label)throw Error('Choose a unique Field session name in this chat’s generator settings.');
 if(chat.hasFiles())throw Error('Activation Field currently accepts text messages only.');
 const token=(await readFile(join(root,'api-token'),'utf8')).trim();
 const headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'};
 async function api(path:string,body?:unknown,method?:string){
  const res=await fetch('http://127.0.0.1:8791'+path,{headers,method:method??(body?'POST':'GET'),body:body?JSON.stringify(body):undefined});
  const data=await res.json();if(!res.ok)throw Error(JSON.stringify(data));return data;
 }
 const dir=join(root,'plugin-sessions');await mkdir(dir,{recursive:true,mode:0o700});
 const file=join(dir,createHash('sha256').update(label).digest('hex')+'.json');
 let mapping:{session_id:string,cut:number};
 try{mapping=JSON.parse(await readFile(file,'utf8'));}
 catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;const s=await api('/sessions',{request_id:randomUUID(),expected_version:0,persist:true});mapping={session_id:s.session_id,cut:0};}
 async function save(){const tmp=file+'.'+randomUUID();await writeFile(tmp,JSON.stringify(mapping),{mode:0o600});await rename(tmp,file);}
 await save();let sid=mapping.session_id;let s=await api('/sessions/'+sid);
 const messages=chat.map(m=>({role:m.getRole(),content:m.getText()}));
 const last=messages.at(-1);const command=last?.role==='user'&&last.content.startsWith('/field ')?last.content.slice(7).trim():null;
 const rid=randomUUID();
 if(command){
  let result;
  if(command==='diagnostics')result=await api('/sessions/'+sid+'/diagnostics');
  else if(command==='save')result=await api('/sessions/'+sid+'/checkpoint',{request_id:rid,expected_version:s.version,field_only:true});
  else if(command==='fork'){
   result=await api('/sessions/'+sid+'/fork',{request_id:rid,expected_version:s.version,reset_native_state:true});
   if(result.status==='succeeded'){mapping.session_id=result.result.session_id;mapping.cut=messages.length+1;}
  }else if(command.startsWith('load ')){
   result=await api('/sessions/import',{request_id:rid,expected_version:0,checkpoint_id:command.slice(5).trim(),persist:true,reset_native_state:true});
   mapping.session_id=result.session_id;mapping.cut=messages.length+1;
  }else if(['reset ordinary','reset weights','reset all','reset traces'].includes(command)){
   const kinds:Record<string,string>={'reset ordinary':'ordinary_state','reset weights':'weights','reset all':'entire_session','reset traces':'activity_traces'};
   result=await api('/sessions/'+sid+'/reset',{request_id:rid,expected_version:s.version,kind:kinds[command]});
   if(result.status==='succeeded'&&command!=='reset traces')mapping.cut=messages.length+1;
  }else result={commands:['/field diagnostics','/field save','/field load CHECKPOINT_ID','/field fork','/field reset ordinary','/field reset weights','/field reset traces','/field reset all']};
  await save();ctl.fragmentGenerated('Field session: '+label+'\n'+JSON.stringify(result,null,2));return;
 }
 const patch={enabled:cfg.get('enabled'),read_only:cfg.get('readOnly'),eta:cfg.get('eta'),amplitude_threshold:cfg.get('threshold'),frequency_floor:cfg.get('frequencyFloor'),frequency_half_life_tokens:cfg.get('frequencyHalfLife'),baseline_pull:cfg.get('baselinePull'),commit_every_tokens:cfg.get('commitTokens'),role_scales:{user:cfg.get('userScale'),assistant:cfg.get('assistantScale'),system:cfg.get('systemScale'),tool:cfg.get('toolScale')}};
 if(Object.entries(patch).some(([k,v])=>JSON.stringify(s.config[k])!==JSON.stringify(v))){
  const r=await api('/sessions/'+sid+'/config',{request_id:rid+'-config',expected_version:s.version,config:patch},'PATCH');
  if(r.status!=='succeeded')throw Error(JSON.stringify(r));s.version=r.version;
 }
 const filtered:typeof messages=[];
 for(let i=mapping.cut;i<messages.length;i++){if(messages[i].role==='user'&&messages[i].content.startsWith('/field ')){if(messages[i+1]?.role==='assistant')i++;continue;}filtered.push(messages[i]);}
 if(ctl.abortSignal.aborted)return;
 const cancel=()=>{void api('/sessions/'+sid).then(state=>api('/sessions/'+sid+'/requests/'+rid+'/cancel',{request_id:rid+'-cancel',expected_version:state.version})).catch(()=>{});};
 ctl.abortSignal.addEventListener('abort',cancel,{once:true});
 try{
  const res=await fetch('http://127.0.0.1:8791/sessions/'+sid+'/generate',{method:'POST',headers,body:JSON.stringify({request_id:rid,expected_version:s.version,messages:filtered,max_tokens:cfg.get('maxTokens'),temperature:0,stream:true})});
  if(!res.ok)throw Error(await res.text());if(!res.body)throw Error('Missing stream');
  if(ctl.abortSignal.aborted)cancel();
  const reader=res.body.getReader();const decoder=new TextDecoder();let pending='';
  while(true){const part=await reader.read();if(part.done)break;pending+=decoder.decode(part.value,{stream:true});let split;
   while((split=pending.indexOf('\n\n'))>=0){const packet=pending.slice(0,split);pending=pending.slice(split+2);const line=packet.split('\n').find(l=>l.startsWith('data: '));if(!line)continue;
    const event=JSON.parse(line.slice(6));if(event.type==='token')ctl.fragmentGenerated(event.text);
    if(event.type==='complete'&&event.receipt.status!=='succeeded')throw Error(JSON.stringify(event.receipt));
   }
  }
 }finally{ctl.abortSignal.removeEventListener('abort',cancel);}
}
export async function main(context:PluginContext){context.withConfigSchematics(configSchematics);context.withGenerator(generate);}
