import {runTool,limitedJSON,reply,hexHash,resolveControllerOwner} from './bridge.mjs';
import {bearerPrincipal} from './oauth.mjs';
import {controllerStatus,claimControllerJob} from './ha.mjs';
import {openGrokJob} from './grok.mjs';

const transportSleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const terminalStates=new Set(['succeeded','failed','uncertain']);
export function providerFrame(data){
 if(typeof data!=='string')throw Error('Invalid frame');
 return JSON.parse(data);
}
function toolValue(result){
 if(result.isError)throw new Error(result.content?.map(x=>x.text||'').join(' ')||'Controller unavailable');
 return result.structuredContent??JSON.parse(result.content.find(x=>x.type==='text').text);
}
async function transportInvoke(env,operation,args,request_id,controller){
 return toolValue(await runTool(env,'biome_invoke',{operation,arguments:args,request_id,realm:'site',...(controller===undefined?{}:{controller})}));
}

// Retry identity belongs to the logical exchange, including after a lost reply.
// Durable controller receipts, leases and fences remain the source of truth.
export async function capabilityExchange(env,body,active=()=>true,onState=()=>{}){
 if(!body||!['chat.private.exchange:2','image.private.exchange:1'].includes(body.contract)||
    typeof body.resource!=='string'||!body.envelope||typeof body.envelope.id!=='string'||
    !/^[A-Za-z0-9_.:-]{1,100}$/.test(body.envelope.id))throw Error('Invalid exchange');
 const controller=body.controller, id=body.envelope.id, key='exchange-'+await hexHash(id), inputs={envelope:body.envelope};
 await transportInvoke(env,'cap.submit',{contract:body.contract,resource:body.resource,operation:id,inputs},key+'-submit',controller);
 while(active()){
  let state;
  try{state=await transportInvoke(env,'cap.submission',{operation:id},crypto.randomUUID(),controller)}
  catch{await transportSleep(500);continue}
  onState(state.state);
  if(terminalStates.has(state.state)){
   return state;
  }
  await transportSleep(250);
 }
}

export async function exchangeStream(req,env,conf){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 if(!await bearerPrincipal(req,conf))return reply({error:'invalid_token'},401);
 let body;try{body=await req.json()}catch{return reply({error:'Invalid exchange'},400)}
 if(!body||!['chat.private.exchange:2','image.private.exchange:1'].includes(body.contract)||
    typeof body.resource!=='string'||!body.envelope||typeof body.envelope.id!=='string'||
    !/^[A-Za-z0-9_.:-]{1,100}$/.test(body.envelope.id))return reply({error:'Invalid exchange'},400);
 if(body.controller!==undefined){try{await resolveControllerOwner(env,body.controller)}catch(error){return reply({error:error.message},error.status??503)}}
 let active=true,timer;
 const encoder=new TextEncoder();
 const stream=new ReadableStream({
  start(controller){
   const send=value=>{if(active)try{controller.enqueue(encoder.encode(JSON.stringify(value)+'\n'))}catch{active=false}};
   send({type:'waiting'});
   timer=setInterval(()=>send({type:'heartbeat'}),10000);
   (async()=>{
    try{
     const result=await capabilityExchange(env,body,()=>active,()=>{});
     if(result)send({type:'result',status:result});
    }catch{send({type:'error',reason:'exchange_interrupted_resume_same_request'})}
    finally{clearInterval(timer);if(active){active=false;controller.close()}}
   })();
  },
  cancel(){active=false;clearInterval(timer)}
 });
 return new Response(stream,{headers:{'Content-Type':'application/x-ndjson','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
}

// A socket is an optimization, never durable authority. Any isolate/socket loss
// falls back to the same controller jobs and immutable operation identities.
export function controllerSocket(req,env,node){
 if(!node||req.headers.get('Upgrade')?.toLowerCase()!=='websocket')return reply({error:'WebSocket required'},400);
 const pair=new WebSocketPair(),client=pair[0],socket=pair[1];socket.accept();
 let open=true;const inflight=new Map();
 const stop=()=>{open=false};socket.addEventListener('close',stop);socket.addEventListener('error',stop);
 socket.addEventListener('message',event=>{
  (async()=>{
   const d=JSON.parse(event.data);
   if(d.type==='ping'){socket.send(JSON.stringify({type:'pong'}));return}
   const expected=inflight.get(d.id);
   if(d.type!=='complete'||!expected||expected!==d.lease||!Array.isArray(d.result?.content))return;
   const now=Date.now()/1000;
   const row=await env.DB.prepare("UPDATE bridge_jobs SET state='done',request=CASE WHEN id LIKE 'event-%' THEN '{}' WHEN id LIKE 'packet-%' THEN json_remove(request,'$.arguments.chunk') ELSE request END,result=?,updated=? WHERE id=? AND lease=? AND state='claimed' AND controller=? AND EXISTS(SELECT 1 FROM controller_nodes WHERE id=? AND enabled=1) RETURNING id")
    .bind(JSON.stringify(d.result),now,d.id,d.lease,node.id,node.id).first();
   if(inflight.get(d.id)===d.lease)inflight.delete(d.id);
   socket.send(JSON.stringify({type:'accepted',id:d.id,lease:d.lease,accepted:!!row}));
  })().catch(()=>{try{socket.close(1011,'Transport interrupted')}catch{};open=false});
 });
 (async()=>{
  while(open){
   const live=await env.DB.prepare('SELECT * FROM controller_nodes WHERE id=? AND enabled=1').bind(node.id).first();
   if(!live){socket.close(1008,'Credential revoked');break}
   // A lost reply or failed local dispatch must not permanently consume a
   // socket slot. Ownership lives in the durable lease, not this delivery map.
   if(inflight.size>=live.capacity){
    const claims=(await env.DB.prepare("SELECT id,lease FROM bridge_jobs WHERE controller=? AND state='claimed' AND lease_until>?").bind(node.id,Date.now()/1000).all()).results;
    const owned=new Map(claims.map(row=>[row.id,row.lease]));
    for(const [id,lease] of inflight)if(owned.get(id)!==lease)inflight.delete(id);
   }
   // Match the same resource admission policy as the HTTP inbox.
   if(inflight.size<live.capacity){
    const job=await claimControllerJob(env.DB,live,env);
    if(job.id){
     let request=JSON.parse(job.request);
     // Subscription changes retain their established event fencing path.
     if(request.name==='sealed_grok_receiver'){
      request={name:'grok_receiver_change',arguments:await openGrokJob(env,job.id,request.arguments)};
     }
     inflight.set(job.id,job.lease);socket.send(JSON.stringify({type:'job',id:job.id,lease:job.lease,request}));continue;
    }
   }
   await transportSleep(100);
  }
 })().catch(()=>{try{socket.close(1011,'Reconnect')}catch{}});
 return new Response(null,{status:101,webSocket:client});
}

// A journal version is a wake hint only. It never supplies a provider result or
// substitutes for native authorization. Local/legacy/error cases keep polling.
export async function providerJournalSignal(env,args,timeoutMs=1000){
 if(env.CONTROLLER_COORDINATION_MODE==='local'||(args?.realm??'site')!=='site')return null;
 let timer;
 try{
  return await Promise.race([
   Promise.resolve().then(()=>env.DB.prepare(`
    SELECT c.site,c.enabled,j.seq,j.hash FROM controller_config c
    LEFT JOIN controller_journal j ON j.site=c.site AND j.seq=(
     SELECT MAX(seq) FROM controller_journal WHERE site=c.site
    ) WHERE c.id=1 AND c.enabled=1
   `).first()).then(row=>{
    if(!row||row.enabled!==1||typeof row.site!=='string'||!row.site)return null;
    const seq=row.seq??0,hash=row.hash??'';
    if(!Number.isSafeInteger(seq)||seq<0||typeof hash!=='string')return null;
    return JSON.stringify([row.site,seq,hash]);
   }).catch(()=>null),
   new Promise(resolve=>{timer=setTimeout(()=>resolve(null),Math.max(0,timeoutMs));})
  ]);
 }finally{clearTimeout(timer)}
}

export async function providerInboxReply(args,{invoke,authorize,signal,active=()=>true,
 sleep=transportSleep,now=()=>performance.now(),requestId=()=>crypto.randomUUID()}){
 const polling=args?.operation==='provider.poll';
 const allowed=async()=>active()&&!!await authorize()&&active();
 const hint=async budget=>{try{const value=await signal(args,budget);return typeof value==='string'&&value?value:null}catch{return null}};
 let current=args;
 while(active()){
  // Capture BEFORE every native read: a commit while that read is executing
  // must still wake its empty reply. Capturing afterwards loses that wake.
  const before=polling?await hint(1000):null;
  if(!active()||!await allowed())return null;
  const result=await invoke(current);
  if(!active())return null;
  let empty=false;
  if(polling&&!result.isError){
   const value=toolValue(result);
   empty=!value.actions?.length&&!value.fences?.length&&!value.activations?.length&&!value.streams?.length;
  }
  if(!empty)return await allowed()?result:null;

  // This budget starts AFTER the empty reply, not before a slow native RPC.
  // Monotonic time avoids wall-clock corrections extending/shortening it.
  const started=now();
  if(before===null){
   await sleep(250);
   if(!active()||!await allowed())return null;
  }else{
   for(;;){
    const remaining=5000-(now()-started);
    if(remaining<=0)break;
    await sleep(Math.min(250,remaining));
    if(!active()||!await allowed())return null;
    const left=5000-(now()-started);
    if(left<=0)break;
    const observed=await hint(Math.min(1000,left));
    if(!active())return null;
    if(observed===null||observed!==before)break;
   }
  }
  if(!active())return null;
  current={...args,request_id:requestId()};
 }
 return null;
}

export async function providerSocket(req,env,conf){
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return reply({error:'Untrusted origin'},403);
 if(!await bearerPrincipal(req,conf))return reply({error:'invalid_token'},401);
 if(req.headers.get('Upgrade')?.toLowerCase()!=='websocket')return reply({error:'WebSocket required'},400);
 const pair=new WebSocketPair(),client=pair[0],socket=pair[1];socket.accept();
 let open=true;const pending=new Set();
 socket.addEventListener('close',()=>{open=false});socket.addEventListener('error',()=>{open=false});
 socket.addEventListener('message',event=>{
  (async()=>{
   if(!open)return;
   const principal=await bearerPrincipal(req,conf);
   if(!open)return;
   if(!principal){socket.close(1008,'Reauthorize');open=false;return}
   const d=providerFrame(event.data);
   if(d.type==='ping'){socket.send(JSON.stringify({type:'pong'}));return}
   if(d.type!=='invoke'||typeof d.id!=='string'||pending.has(d.id))throw Error('Invalid invocation');
   pending.add(d.id);
   try{
    const a=d.arguments;
    const result=a?.operation==='provider.poll'?await providerInboxReply(a,{
     active:()=>open,invoke:args=>runTool(env,'biome_invoke',args),
     signal:(args,budget)=>providerJournalSignal(env,args,budget),
     authorize:async()=>{
      if(!open)return false;
      const principal=await bearerPrincipal(req,conf);
      if(!open)return false;
      if(!principal){socket.close(1008,'Reauthorize');open=false;return false}
      return true;
     }
    }):await runTool(env,'biome_invoke',a);
    if(open&&result!==null)socket.send(JSON.stringify({type:'reply',id:d.id,result}));
   }finally{pending.delete(d.id)}
  })().catch(()=>{try{socket.close(1011,'Reconnect')}catch{};open=false});
 });
 return new Response(null,{status:101,webSocket:client});
}
