// One durable site runtime, independent of the two physical gateway links.
// Only owner-authenticated runTool calls may use the dedicated operator signer.
const hash=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const fault=(message,status=503)=>Object.assign(Error(message),{status});
const configured=new WeakMap();
const success=value=>({content:[{type:'text',text:JSON.stringify(value)}]});
const failure=error=>({isError:true,content:[{type:'text',text:error.message??'Controller runtime unavailable'}],
 structuredContent:{error_code:'CONTROLLER_RUNTIME_UNAVAILABLE',status:error.status??503,retry_same_id:true}});
function validDeclaration(value){
 if(!object(value)||Object.keys(value).some(k=>!['service_id','authority','controller_aliases','operator_actor'].includes(k))||
    typeof value.service_id!=='string'||!value.service_id||!hash(value.authority)||!hash(value.operator_actor)||
    !Array.isArray(value.controller_aliases)||!value.controller_aliases.length||
    value.controller_aliases.some(v=>!hash(v)||v===value.authority)||
    new Set(value.controller_aliases).size!==value.controller_aliases.length)
  throw fault('Invalid controller runtime declaration');
 return {service_id:value.service_id,authority:value.authority,
         controller_aliases:[...value.controller_aliases].sort(),operator_actor:value.operator_actor};
}
function serviceOrigin(env){
 let url;try{url=new URL(env.SERVICE_AUTHORITY_ORIGIN)}catch{throw fault('Controller runtime origin unavailable')}
 if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/'||url.search||url.hash)
  throw fault('Invalid controller runtime origin');
 return url.origin;
}
export async function controllerRuntimeConfig(env){
 const raw=env.CONTROLLER_RUNTIME_AUTHORITY??'';
 const previous=configured.get(env);
 if(previous?.raw===raw)return {...previous.value,origin:serviceOrigin(env)};
 if(!raw&&!env.CONTROLLER_RUNTIME_REQUIRED&&!previous){
  if(!env.DB)return null;
  const table=await env.DB.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='controller_runtime_scopes'").first();
  if(!table)return null;
 }
 const config=await env.DB.prepare('SELECT site FROM controller_config WHERE id=1').first();
 if(!config?.site)throw fault('Controller site is unavailable');
 await env.DB.prepare('CREATE TABLE IF NOT EXISTS controller_runtime_scopes(site TEXT PRIMARY KEY,declaration TEXT NOT NULL)').run();
 let retained=await env.DB.prepare('SELECT declaration FROM controller_runtime_scopes WHERE site=?').bind(config.site).first();
 let declared;
 try{declared=raw?validDeclaration(JSON.parse(raw)):retained?validDeclaration(JSON.parse(retained.declaration)):null}
 catch{throw fault('Invalid controller runtime declaration')}
 if(!declared){if(!raw&&!env.CONTROLLER_RUNTIME_REQUIRED)return null;throw fault('Required controller runtime has no retained declaration');}
 const encoded=JSON.stringify(declared);
 if(retained&&JSON.stringify(validDeclaration(JSON.parse(retained.declaration)))!==encoded)
  throw fault('Controller runtime custody changed; explicit migration is required',409);
 await env.DB.prepare('INSERT INTO controller_runtime_scopes(site,declaration) VALUES(?,?) ON CONFLICT(site) DO NOTHING').bind(config.site,encoded).run();
 retained=await env.DB.prepare('SELECT declaration FROM controller_runtime_scopes WHERE site=?').bind(config.site).first();
 if(!retained||retained.declaration!==encoded)throw fault('Controller runtime declaration changed concurrently',409);
 const value={...declared,site:config.site,origin:serviceOrigin(env)};
 configured.set(env,{raw,value});return value;
}
function addresses(config,controller){
 return controller===undefined||controller===config.authority||config.controller_aliases.includes(controller);
}
function asciiString(value){
 return JSON.stringify(value).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
}
function codepointOrder(a,b){
 const x=[...a],y=[...b];
 for(let i=0;i<Math.min(x.length,y.length);i++){
  const d=x[i].codePointAt(0)-y[i].codePointAt(0);if(d)return d;
 }
 return x.length-y.length;
}
// Canonicalize the JSON values that Python will parse from our wire body.
// Native provider envelopes never pass through this encoder.
export function pythonCanonical(value){
 if(value===null||typeof value==='boolean')return JSON.stringify(value);
 if(typeof value==='string')return asciiString(value);
 if(typeof value==='number'){
  if(!Number.isFinite(value))throw fault('Non-finite JSON number',400);
  const wire=JSON.stringify(value);
  if(!/[.eE]/.test(wire))return wire; // Python parses this as an integer.
  const n=Math.abs(value);
  if(n!==0&&(n<0.0001||n>=1e16)){
   const [mantissa,exponent]=value.toExponential().split('e'),e=Number(exponent);
   return mantissa+'e'+(e<0?'-':'+')+String(Math.abs(e)).padStart(2,'0');
  }
  return wire;
 }
 if(Array.isArray(value))return '['+value.map(pythonCanonical).join(',')+']';
 if(object(value))return '{'+Object.keys(value).sort(codepointOrder).map(k=>asciiString(k)+':'+pythonCanonical(value[k])).join(',')+'}';
 throw fault('Unsupported JSON value',400);
}
async function signEnvelope(env,config,envelope){
 const pem=env.CONTROLLER_RUNTIME_SIGNING_KEY;
 const match=typeof pem==='string'&&/^\s*-----BEGIN PRIVATE KEY-----([\s\S]+?)-----END PRIVATE KEY-----\s*$/.exec(pem);
 if(!match)throw fault('Controller runtime operator signer unavailable');
 let bytes;try{bytes=Uint8Array.from(atob(match[1].replace(/\s/g,'')),c=>c.charCodeAt(0))}
 catch{throw fault('Controller runtime operator signer unavailable')}
 const key=await crypto.subtle.importKey('pkcs8',bytes,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['sign']);
 const signature=await crypto.subtle.sign('RSASSA-PKCS1-v1_5',key,new TextEncoder().encode(pythonCanonical(envelope)));
 return {...envelope,auth:{actor:config.operator_actor,signature:btoa(String.fromCharCode(...new Uint8Array(signature)))}};
}
async function forward(config,raw,path='call'){
 const response=await fetch(config.origin+'/api/controller-runtimes/'+config.authority+'/'+path,{
  method:'POST',headers:{'Content-Type':'application/json'},body:raw,redirect:'error',
  signal:AbortSignal.timeout(30000)
 });
 if(![200,400,401,403,404,409,410,413,429,503].includes(response.status))
  throw fault('Controller runtime reply unavailable');
 return new Response(await response.text(),{status:response.status,headers:{
  'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
}
async function operatorCall(env,config,operation,args,id,controller){
 const envelope={site:config.site,realm:'site',operation,arguments:args,id,
                 controller:controller??config.authority};
 const signed=await signEnvelope(env,config,envelope);
 const response=await forward(config,JSON.stringify(signed));
 let body;try{body=await response.json()}catch{throw fault('Controller runtime reply unavailable')}
 if(response.status!==200)throw fault(body.error??'Controller runtime unavailable',response.status);
 if(!object(body)||!Object.hasOwn(body,'result'))throw fault('Invalid controller runtime result');
 const result=body.result;
 if(object(result)&&result.next_call)result.next_call={...result.next_call,controller:envelope.controller};
 return result;
}
export async function controllerRuntimeTool(env,name,args){
 if((args.realm??'site')!=='site'||name==='biome_invoke'&&/^(maintenance\.|controller\.)/.test(args.operation))return null;
 try{
  const config=await controllerRuntimeConfig(env);
  if(!config||!addresses(config,args.controller))return null;
  if(name==='biome_invoke')return success(await operatorCall(env,config,args.operation,args.arguments,args.request_id,args.controller));
  if(name==='biome_work_list')return success(await operatorCall(env,config,'work.list',{},'runtime-work-'+crypto.randomUUID(),args.controller));
  if(name==='biome_status'){
   const [info,progress,cluster]=await Promise.all(['system.info','system.progress','cluster.status'].map(operation=>
    operatorCall(env,config,operation,{},'runtime-status-'+crypto.randomUUID(),args.controller)));
   return success({node:config.authority,health:{realms:{site:'ready'},consumer_health:progress},
                   controller:info,cluster});
  }
  return null;
 }catch(error){return failure(error)}
}
// selected is a previously recorded dispatch origin, never a rewritten signature.
export async function controllerRuntimeNative(env,raw,selected){
 try{
  const config=await controllerRuntimeConfig(env);if(!config)return null;
  const envelope=JSON.parse(raw);
  if(!object(envelope)||envelope.realm!=='site')throw fault('Exact site envelope required',400);
  if(envelope.site!==config.site)throw fault('Wrong controller site',403);
  if(!addresses(config,envelope.controller)||!addresses(config,selected))return null;
  return await forward(config,raw);
 }catch(error){return Response.json({error:error.message??'Controller runtime unavailable',retry_same_id:true},{status:error.status??503})}
}
export async function controllerRuntimePair(env,phase,raw,selected){
 try{
  const config=await controllerRuntimeConfig(env);if(!config||!addresses(config,selected))return null;
  if(!['begin','finish'].includes(phase))throw fault('Unsupported pairing phase',400);
  return await forward(config,raw,'pair/'+phase);
 }catch(error){return Response.json({error:error.message??'Controller runtime unavailable',retry_same_id:true},{status:error.status??503})}
}
