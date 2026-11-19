import {consoleRead} from '../lib/console-read.mjs';
import {gatewayErrorKind} from '../lib/diagnostics.mjs';
import {nativeProviderCall,nativeProviderPair} from '../lib/provider.mjs';
import {nativeCall} from '../lib/native-call.mjs';
import {maintenanceDevice} from '../lib/maintenance.mjs';
import {dashboard} from '../lib/dashboard.mjs';
import {agentInbox} from '../lib/agent-inbox.mjs';
import {grokCall,grokTools,sealGrokJob} from '../lib/grok.mjs';
import {eventGrant,changeSubscription,delegationEvent,leaseEvent} from '../lib/events.mjs';
import {privateHeaders,ownerPrincipal} from '../lib/access.mjs';
import {bearerPrincipal,challenge,oauth,resourceMetadata,serverMetadata} from '../lib/oauth.mjs';
import {discoverPage} from '../lib/discovery.mjs';
import {device,limitedJSON,reply,runTool,toolsList,enqueue} from '../lib/bridge.mjs';
import {exchangeStream,providerSocket} from '../lib/transport.mjs';
import {mobileDecision} from '../lib/decisions.mjs';
import {leasedStream,inspectStream} from '../lib/leased-stream.mjs';

const protocols=['2026-07-28','2025-11-25','2025-06-18','2025-03-26','2024-11-05'];
async function dispatch(req,env) {
 const url=new URL(req.url), path=url.pathname;
 const conf={...env,ownerPrincipal:r=>ownerPrincipal(r,env)};
 if(path==='/api/stream/inspect')return inspectStream(req,env);
 if(path.startsWith('/api/stream/'))return leasedStream(req,env);
 if(path==='/api/console/connection')return reply({connected:!!await ownerPrincipal(req,env),transport:'controller-owner-session'});
 if(path==='/api/console/read')return consoleRead(req,env);
 if(path==='/dashboard'||path.startsWith('/dashboard/'))return dashboard(req,env);
 if(path==='/maintenance/device')return maintenanceDevice(req,env);
 if(path==='/v1/call')return nativeCall(req,env);
 if(path==='/api/provider/call')return nativeProviderCall(req,env);
 if(path==='/api/provider/pair/begin')return nativeProviderPair(req,env,'begin');
 if(path==='/api/provider/pair/finish')return nativeProviderPair(req,env,'finish');
 if(path==='/api/decisions')return mobileDecision(req,env);
 if(path==='/api/exchange')return exchangeStream(req,env,conf);
 if(path==='/api/provider/connect')return providerSocket(req,env,conf);
 if (path.startsWith('/agent-events/')) return agentInbox(req,conf,path);
 if (path.startsWith('/bridge/')) return device(req,env,path);
 if (req.method==='GET' && ['/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/api/mcp','/.well-known/oauth-protected-resource/mcp'].includes(path)) return resourceMetadata(conf);
 if (req.method==='GET' && path==='/.well-known/oauth-authorization-server') return serverMetadata(conf);
 if (path.startsWith('/oauth/')) return oauth(req,conf,path.slice(7));
 if(path==='/')return new Response('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BiomeRTE Controller</title></head><body><h1>BiomeRTE Controller</h1><p><a href="/dashboard/">Open dashboard</a></p><p>Connect an OAuth client at /api/mcp.</p></body></html>',{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'}});
 if (!['/api/mcp','/mcp'].includes(path)) return reply({error:'Not found'},404);
 const origin=req.headers.get('Origin');
 if (origin && origin!==url.origin) return reply({error:'Untrusted origin'},403);
 const external=path==='/api/mcp';
 const bearerOwner=external ? await bearerPrincipal(req,conf) : null;
 if(external && !bearerOwner) return Response.json({error:'invalid_token'},{status:401,headers:{...privateHeaders,'WWW-Authenticate':challenge(conf)}});
 if(req.method!=='POST') return new Response(null,{status:405,headers:{Allow:'POST'}});
 let body;
 try{body=await req.json()}catch{return reply({error:'Invalid JSON'},400)}
 if(!body || Array.isArray(body) || body.jsonrpc!=='2.0' || typeof body.method!=='string') return reply({jsonrpc:'2.0',id:null,error:{code:-32600,message:'Invalid request'}},400);
 const {method,id,params={}}=body;
 const ok=result=>reply({jsonrpc:'2.0',id,result});
 if(method.startsWith('notifications/'))return new Response(null,{status:202});
 if(method==='server/discover')return ok({resultType:'complete',supportedVersions:protocols,capabilities:{tools:{},events:{}}});
 if(method==='initialize')return ok({protocolVersion:protocols.includes(params.protocolVersion)?params.protocolVersion:'2025-03-26',capabilities:{tools:{},events:{}},serverInfo:{name:'BiomeRTE Controller',version:'1.0.0'}});
 if(method==='ping')return ok({});
 if(method==='tools/list')return ok({tools:[...toolsList,...grokTools]});
 if(method==='events/list')return ok({events:[delegationEvent,leaseEvent]});
 if(method==='events/subscribe'||method==='events/unsubscribe'){
  const owner=bearerOwner||await ownerPrincipal(req,env);
  if(!owner)return reply({error:'Owner sign-in required'},403);
  const response=await changeSubscription(env,method,params,owner,await eventGrant(req,env,external));
  return reply({jsonrpc:'2.0',id,...response});
 }
 if(method!=='tools/call')return reply({jsonrpc:'2.0',id,error:{code:-32601,message:'Unknown method'}});
 const owner=bearerOwner||await ownerPrincipal(req,env);
 if(!owner)return reply({error:'Owner sign-in required'},403);
 const args=params.arguments??{};
 if(params.name==='biome_discover')return ok(await discoverPage(env,args));
 if(params.name==='biome_invoke' && ['discovery.page','system.discovery'].includes(args.operation))return ok(await discoverPage(env,{...(args.arguments??{}),...(args.controller===undefined?{}:{controller:args.controller})},args.realm??'site'));
 const grok=grokCall(params);
 if(grok){
  const grant=await eventGrant(req,env,external);
  const key='event-grok-'+crypto.randomUUID();
  const {action,args}=grok;
  if(!args||typeof args!=='object'||Array.isArray(args)||JSON.stringify(args).length>12000)
   return ok({isError:true,content:[{type:'text',text:'Invalid receiver parameters.'}]});
  const sealed=await sealGrokJob(env,key,{owner,grant,action,args});
  return ok(await enqueue(env,'sealed_grok_receiver',sealed,key));
 }
 return ok(await runTool({...env,maintenanceActor:owner},params.name,params.arguments));
}
export default {async fetch(req,env){try{return await dispatch(req,env)}catch(error){console.error('controller_gateway_failure',{path:new URL(req.url).pathname,error_kind:gatewayErrorKind(error)});return Response.json({error:'Controller gateway unavailable'},{status:503,headers:{'Cache-Control':'no-store'}})}}};
