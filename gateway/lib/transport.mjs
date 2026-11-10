import {runTool,limitedJSON,reply,hexHash} from './bridge.mjs';
import {bearerPrincipal} from './oauth.mjs';
import {controllerStatus,claimControllerJob} from './ha.mjs';
import {openGrokJob} from './grok.mjs';

const transportSleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const terminalStates=new Set(['succeeded','failed','uncertain','abandoned']);
function toolValue(result){
 if(result.isError)throw new Error(result.content?.map(x=>x.text||'').join(' ')||'Controller unavailable');
 return result.structuredContent??JSON.parse(result.content.find(x=>x.type==='text').text);
}
async function transportInvoke(env,operation,args,request_id){
 return toolValue(await runTool(env,'biome_invoke',{operation,arguments:args,request_id,realm:'site'}));
}

// Retry identity belongs to the logical exchange, including after a lost reply.
// Durable controller receipts, leases and fences remain the source of truth.
export async function capabilityExchange(env,body,active=()=>true,onState=()=>{}){
 if(!body||!['chat.private.exchange:2','image.private.exchange:1'].includes(body.contract)||
    typeof body.resource!=='string'||!body.envelope||typeof body.envelope.id!=='string'||
    !/^[A-Za-z0-9_.:-]{1,100}$/.test(body.envelope.id))throw Error('Invalid exchange');
 const id=body.envelope.id, key='exchange-'+await hexHash(id), inputs={envelope:body.envelope};
 await transportInvoke(env,'cap.submit',{contract:body.contract,resource:body.resource,operation:id,inputs},key+'-submit');
 while(active()){
  let state;
  try{state=await transportInvoke(env,'cap.submission',{operation:id},crypto.randomUUID())}
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
 let body;try{body=await limitedJSON(req,96000)}catch{return reply({error:'Invalid exchange'},400)}
 if(!body||!['chat.private.exchange:2','image.private.exchange:1'].includes(body.contract)||
    typeof body.resource!=='string'||!body.envelope||typeof body.envelope.id!=='string'||
    !/^[A-Za-z0-9_.:-]{1,100}$/.test(body.envelope.id))return reply({error:'Invalid exchange'},400);
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
   const row=await env.DB.prepare("UPDATE bridge_jobs SET state='done',request=CASE WHEN id LIKE 'event-%' THEN '{}' ELSE request END,result=?,updated=? WHERE id=? AND lease=? AND state='claimed' AND controller=? AND EXISTS(SELECT 1 FROM controller_nodes WHERE id=? AND enabled=1) RETURNING id")
    .bind(JSON.stringify(d.result),now,d.id,d.lease,node.id,node.id).first();
   inflight.delete(d.id);socket.send(JSON.stringify({type:'accepted',id:d.id,accepted:!!row}));
  })().catch(()=>{try{socket.close(1011,'Transport interrupted')}catch{};open=false});
 });
 (async()=>{
  while(open){
   const live=await env.DB.prepare('SELECT * FROM controller_nodes WHERE id=? AND enabled=1').bind(node.id).first();
   if(!live){socket.close(1008,'Credential revoked');break}
   // Delivery slots follow durable ownership, so a lost reply cannot leak capacity.
   if(inflight.size>=live.capacity){
    const claims=(await env.DB.prepare("SELECT id,lease FROM bridge_jobs WHERE controller=? AND state='claimed' AND lease_until>?").bind(node.id,Date.now()/1000).all()).results;
    const owned=new Map(claims.map(row=>[row.id,row.lease]));
    for(const [id,lease] of inflight)if(owned.get(id)!==lease)inflight.delete(id);
   }
   // Match the same resource admission policy as the HTTP inbox.
   if(inflight.size<live.capacity){
    const job=await claimControllerJob(env.DB,live);
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
   if(!await bearerPrincipal(req,conf)){socket.close(1008,'Reauthorize');open=false;return}
   if(typeof event.data!=='string'||event.data.length>125000)throw Error('Invalid frame');
   const d=JSON.parse(event.data);
   if(d.type==='ping'){socket.send(JSON.stringify({type:'pong'}));return}
   if(d.type!=='invoke'||typeof d.id!=='string'||pending.has(d.id))throw Error('Invalid invocation');
   pending.add(d.id);
   try{
    const a=d.arguments;
    let result=await runTool(env,'biome_invoke',a);
    // A held work-inbox read replaces empty polling on the Mac. Heartbeats and
    // other invocations remain independent on the multiplexed connection.
    while(open&&a?.operation==='provider.poll'&&!result.isError){
     const value=toolValue(result);
     if(value.actions?.length||value.fences?.length||value.activations?.length)break;
     await transportSleep(250);
     if(!await bearerPrincipal(req,conf)){socket.close(1008,'Reauthorize');open=false;return}
     result=await runTool(env,'biome_invoke',{...a,request_id:crypto.randomUUID()});
    }
    if(open)socket.send(JSON.stringify({type:'reply',id:d.id,result}));
   }finally{pending.delete(d.id)}
  })().catch(()=>{try{socket.close(1011,'Reconnect')}catch{};open=false});
 });
 return new Response(null,{status:101,webSocket:client});
}
