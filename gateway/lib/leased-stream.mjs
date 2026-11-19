import {runTool} from './bridge.mjs';

// Router-only readback. The MAC authenticates exact bytes, target authority and
// timestamp; this endpoint cannot execute arbitrary controller operations.
export async function inspectStream(req,env,invoke=runTool){
 if(req.method!=='POST')return Response.json({error:'Use POST'},{status:405});
 if(!env.STREAM_ROUTER_SECRET)return Response.json({error:'Router authentication unavailable'},{status:503});
 if(Number(req.headers.get('Content-Length')||0)>2048)return new Response(null,{status:413});
 const raw=await req.text();
 if(raw.length>2048)return new Response(null,{status:413});
 const signature=req.headers.get('Authorization')||'';
 if(!/^Router [0-9a-f]{64}$/.test(signature))return new Response(null,{status:401});
 const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(env.STREAM_ROUTER_SECRET),{name:'HMAC',hash:'SHA-256'},false,['verify']);
 const bytes=Uint8Array.from(signature.slice(7).match(/../g),h=>parseInt(h,16));
 if(!await crypto.subtle.verify('HMAC',key,bytes,new TextEncoder().encode(raw)))return new Response(null,{status:401});
 let body;try{body=JSON.parse(raw)}catch{return new Response(null,{status:400})}
 if(!/^[a-f0-9]{64}$/.test(body.authority||'')||!/^[a-f0-9]{32}$/.test(body.session||'')||
    typeof body.timestamp!=='number'||!Number.isFinite(body.timestamp)||Math.abs(Date.now()/1000-body.timestamp)>30)return new Response(null,{status:400});
 const allowed=(env.STREAM_ROUTER_AUTHORITIES||'').split(',');
 if(!allowed.includes(body.authority))return new Response(null,{status:403});
 const result=await invoke(env,'biome_invoke',{controller:body.authority,operation:'stream.inspect',arguments:{session:body.session},realm:'site',request_id:crypto.randomUUID()});
 if(result.isError)return Response.json({error:'Grant unavailable'},{status:409});
 const value=result.structuredContent??JSON.parse(result.content.find(x=>x.type==='text').text);
 return Response.json(value,{headers:{'Cache-Control':'no-store'}});
}

// Public gateway proxy to the persistent session owner. No per-message DB work.
export async function leasedStream(req,env){
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return Response.json({error:'Untrusted origin'},{status:403});
 if(req.headers.get('Upgrade')?.toLowerCase()!=='websocket')return Response.json({error:'WebSocket required'},{status:400});
 if(!/^Stream [A-Za-z0-9_-]+\.[0-9a-f]{64}$/.test(req.headers.get('Authorization')||''))return Response.json({error:'Stream credential required'},{status:401});
 const route=new URL(req.url).pathname.slice('/api/stream/'.length);
 if(!/^[a-f0-9]{32}\/[a-z_]{1,32}$/.test(route))return Response.json({error:'Invalid stream attachment'},{status:400});
 // Sites private connectivity forwards the upgrade to the Mac-hosted owner.
 // Attachment authentication stays bound to the controller-issued stream grant.
 if(env.CUSTOMER_HTTP_STREAM_ROUTER)
  return env.CUSTOMER_HTTP_STREAM_ROUTER.fetch(new Request('http://stream-router/'+route,req));
 // STREAM_ROUTER is a persistent service binding, or an explicitly configured
 // TLS endpoint. An absent deployment fails clearly instead of falling back to
 // controller jobs or stateless Worker tables.
 if(env.STREAM_ROUTER)return env.STREAM_ROUTER.fetch(new Request('https://stream-router/'+route,req));
 if(!env.STREAM_ROUTER_ORIGIN)return Response.json({error:'Persistent stream router is not configured'},{status:503});
 const target=new URL(env.STREAM_ROUTER_ORIGIN);
 if(target.protocol!=='https:'||target.username||target.password||target.search||target.hash)throw Error('Invalid stream router origin');
 target.pathname=target.pathname.replace(/\/$/,'')+'/'+route;
 const response=await fetch(new Request(target,req),{redirect:'manual'});
 if(response.status>=300&&response.status<400)return Response.json({error:'Stream router redirected'},{status:502});
 return response;
}
