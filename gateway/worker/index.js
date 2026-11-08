import {maintenanceDevice} from '../lib/maintenance.mjs';
import {nativeCall} from '../lib/native-call.mjs';
import {agentInbox} from '../lib/agent-inbox.mjs';
import {grokCall,grokTools,sealGrokJob} from '../lib/grok.mjs';
import {eventGrant,changeSubscription,delegationEvent,leaseEvent} from '../lib/events.mjs';
import {privateHeaders,ownerPrincipal} from '../lib/access.mjs';
import {bearerPrincipal,challenge,oauth,resourceMetadata,serverMetadata} from '../lib/oauth.mjs';
import {discoverPage} from '../lib/discovery.mjs';
import {device,limitedJSON,reply,runTool,toolsList,enqueue} from '../lib/bridge.mjs';
import {exchangeStream,providerSocket} from '../lib/transport.mjs';
import {mobileDecision} from '../lib/decisions.mjs';

const protocols=['2026-07-28','2025-11-25','2025-06-18','2025-03-26','2024-11-05'];
async function dispatch(req,env) {
 const url=new URL(req.url), path=url.pathname;
 const conf={...env,ownerPrincipal:r=>ownerPrincipal(r,env)};
 if(path==='/maintenance/device')return maintenanceDevice(req,env);
 if(path==='/v1/call')return nativeCall(req,env);
 if(path==='/api/decisions')return mobileDecision(req,env);
 if(path==='/api/exchange')return exchangeStream(req,env,conf);
 if(path==='/api/provider/connect')return providerSocket(req,env,conf);
 if (path.startsWith('/agent-events/')) return agentInbox(req,conf,path);
 if (path.startsWith('/bridge/')) return device(req,env,path);
 if (req.method==='GET' && ['/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/api/mcp','/.well-known/oauth-protected-resource/mcp'].includes(path)) return resourceMetadata(conf);
 if (req.method==='GET' && path==='/.well-known/oauth-authorization-server') return serverMetadata(conf);
 if (path.startsWith('/oauth/')) return oauth(req,conf,path.slice(7));
 if (path==='/') return new Response('BiomeRTE controller MCP. Connect an OAuth client at /api/mcp. Manage authorized connections at /oauth/connections.',{headers:{'content-type':'text/plain','cache-control':'no-store'}});
 if (!['/api/mcp','/mcp'].includes(path)) return reply({error:'Not found'},404);
 const origin=req.headers.get('Origin');
 if (origin && origin!==url.origin) return reply({error:'Untrusted origin'},403);
 const external=path==='/api/mcp';
 const bearerOwner=external ? await bearerPrincipal(req,conf) : null;
 if(external && !bearerOwner) return Response.json({error:'invalid_token'},{status:401,headers:{...privateHeaders,'WWW-Authenticate':challenge(conf)}});
 if(req.method!=='POST') return new Response(null,{status:405,headers:{Allow:'POST'}});
 let body;
 try{body=await limitedJSON(req,125000)}catch{return reply({error:'Invalid JSON or oversized request'},400)}
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
 if(params.name==='biome_invoke' && args.operation==='discovery.page')return ok(await discoverPage(env,args.arguments??{},args.realm??'site'));
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
 return ok(await runTool(env,params.name,params.arguments));
}
export default {async fetch(req,env){try{return await dispatch(req,env)}catch{return Response.json({error:'Controller gateway unavailable'},{status:503,headers:{'Cache-Control':'no-store'}})}}};
