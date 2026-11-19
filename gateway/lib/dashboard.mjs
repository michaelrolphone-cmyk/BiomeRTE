import {dashboardMappings,dashboardDefinition} from './dashboard-data.mjs';
import {ownerPrincipal, privateHeaders} from './access.mjs';
import {enqueue, hexHash} from './bridge.mjs';
import {dashboardHTML, dashboardJS, dashboardCSS, dashboardModel, dashboardDiagram, dashboardLogo} from './dashboard-assets.mjs';

const dashboardHeaders={...privateHeaders,'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",'X-Frame-Options':'DENY'};
const dashboardJSON=(value,status=200)=>Response.json(value,{status,headers:dashboardHeaders});
export async function dashboard(req,env,deps={ownerPrincipal,enqueue}) {
 const path=new URL(req.url).pathname;
 if(path==='/dashboard/mappings')return dashboardMappings(req,env);
 if(path==='/dashboard/definition')return dashboardDefinition(req,env);
 if(path==='/dashboard')return new Response(null,{status:308,headers:{Location:'/dashboard/','Cache-Control':'no-store'}});
 const api=path==='/dashboard/api';
 if(!api&&!['/dashboard/','/dashboard/ui.js','/dashboard/ui.css','/dashboard/model.mjs','/dashboard/diagram.mjs','/dashboard/logo.png'].includes(path))return dashboardJSON({error:'Not found'},404);
 if(req.method!==(api?'POST':'GET'))return new Response(null,{status:405,headers:{...dashboardHeaders,Allow:api?'POST':'GET'}});
 const owner=await deps.ownerPrincipal(req,env);
 if(!owner) {
  if(path==='/dashboard/'&&!req.headers.get('oai-authenticated-user-id'))return new Response('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BiomeRTE dashboard</title></head><body><h1>BiomeRTE dashboard</h1><p>Sign in with the controller owner account to open the dashboard.</p><p><a href="/signin-with-chatgpt?return_to=%2Fdashboard%2F" target="_top">Sign in with ChatGPT</a></p></body></html>',{headers:{...dashboardHeaders,'Content-Type':'text/html; charset=utf-8'}});
  return dashboardJSON({error:'Controller owner sign-in required'},req.headers.get('oai-authenticated-user-id')?403:401);
 }
 if(!api){
  if(path==='/dashboard/logo.png')return new Response(Uint8Array.from(atob(dashboardLogo),c=>c.charCodeAt(0)),{headers:{...dashboardHeaders,'Content-Type':'image/png'}});
  const [body,type]=path.endsWith('ui.js')?[dashboardJS,'application/javascript']:path.endsWith('ui.css')?[dashboardCSS,'text/css']:path.endsWith('model.mjs')?[dashboardModel,'application/javascript']:path.endsWith('diagram.mjs')?[dashboardDiagram,'application/javascript']:[dashboardHTML,'text/html'];
  return new Response(body,{headers:{...dashboardHeaders,'Content-Type':type+'; charset=utf-8'}});
 }
 if(req.headers.get('Origin')!==new URL(req.url).origin || ['cross-site','same-site'].includes(req.headers.get('Sec-Fetch-Site')))return dashboardJSON({error:'Same-origin request required'},403);
 if(req.headers.get('content-type')?.split(';')[0].trim()!=='application/json')return dashboardJSON({error:'Expected application/json'},415);
 let body;try{body=await req.json()}catch{return dashboardJSON({error:'Invalid JSON'},400)}
 if(!body || Array.isArray(body) || typeof body.id!=='string' || !body.id.trim() || body.id.length>128 ||
   typeof body.operation!=='string' || !/^[a-z][a-z0-9_.:-]{0,127}$/i.test(body.operation) ||
   !body.arguments || typeof body.arguments!=='object' || Array.isArray(body.arguments) ||
   !['site','local'].includes(body.realm??'site') || ('cached' in body&&typeof body.cached!=='boolean') ||
   Object.keys(body).some(k=>!['id','operation','arguments','realm','cached'].includes(k)))return dashboardJSON({error:'Invalid controller call'},400);
 const key='invoke-dashboard-'+await hexHash(owner+'\n'+(body.realm??'site')+'\n'+body.id);
 const result=await (deps.enqueue||enqueue)(env,'biome_dashboard_call',body,key);
 if(result.isError){const message=result.content?.find(x=>x.type==='text')?.text||'Controller unavailable';return dashboardJSON({error:message},/not replied|queue is busy/i.test(message)?504:502)}
 try{
  const value=JSON.parse(result.content.find(x=>x.type==='text').text);
  if(!Number.isInteger(value.status)||value.status<200||value.status>599)throw Error();
  if(value.transport) {
   const transfer=value.transport;
   if(!/^[0-9a-f]{64}$/.test(transfer.sha256)||!Array.isArray(transfer.chunks)||!transfer.chunks.length ||
      transfer.chunks.some(c=>!/^[0-9a-f]{64}$/.test(c.sha256)||!Number.isInteger(c.size)||c.size<1||c.size>131072) ||
      transfer.size!==transfer.chunks.reduce((sum,c)=>sum+c.size,0))throw Error();
   let index=0;
   const stream=new ReadableStream({async pull(controller){
    try{
     if(index===transfer.chunks.length){controller.close();return;}
     const expected=transfer.chunks[index];
     const result=await (deps.enqueue||enqueue)(env,'biome_dashboard_chunk',{sha256:transfer.sha256,index},'invoke-dashboard-chunk-'+owner+'-'+transfer.sha256+'-'+index);
     if(result.isError)throw Error('Dashboard transfer interrupted');
     const part=JSON.parse(result.content.find(x=>x.type==='text').text);
     const bytes=Uint8Array.from(atob(part.base64),c=>c.charCodeAt(0));
     const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
     if(bytes.length!==expected.size||part.size!==expected.size||hash!==expected.sha256||part.sha256!==hash)throw Error('Dashboard chunk integrity failure');
     index++;controller.enqueue(bytes);
    }catch(error){controller.error(error)}
   }});
   return new Response(stream,{status:value.status,headers:{...dashboardHeaders,'Content-Type':'application/json; charset=utf-8'}});
  }
  if(!value.body||typeof value.body!=='object')throw Error();
  return dashboardJSON(value.body,value.status);
 }catch{return dashboardJSON({error:'Invalid controller response'},502)}
}
