// Public gateway proxy to the persistent session owner. No per-message DB work.
export async function leasedStream(req,env){
 const origin=req.headers.get('Origin');
 if(origin&&origin!==new URL(req.url).origin)return Response.json({error:'Untrusted origin'},{status:403});
 if(req.headers.get('Upgrade')?.toLowerCase()!=='websocket')return Response.json({error:'WebSocket required'},{status:400});
 if(!/^Stream [A-Za-z0-9_-]+\.[0-9a-f]{64}$/.test(req.headers.get('Authorization')||''))return Response.json({error:'Stream credential required'},{status:401});
 const route=new URL(req.url).pathname.slice('/api/stream/'.length);
 if(!/^[a-f0-9]{32}\/[a-z_]{1,32}$/.test(route))return Response.json({error:'Invalid stream attachment'},{status:400});
 // STREAM_ROUTER is a persistent service binding, or an explicitly configured
 // TLS endpoint. An absent deployment fails clearly instead of falling back to
 // controller jobs or stateless Worker tables.
 if(env.CUSTOMER_HTTP_STREAM_ROUTER)return env.CUSTOMER_HTTP_STREAM_ROUTER.fetch(new Request('http://stream-router/'+route,req));
 if(env.STREAM_ROUTER)return env.STREAM_ROUTER.fetch(new Request('https://stream-router/'+route,req));
 if(!env.STREAM_ROUTER_ORIGIN)return Response.json({error:'Persistent stream router is not configured'},{status:503});
 const target=new URL(env.STREAM_ROUTER_ORIGIN);
 if(target.protocol!=='https:'||target.username||target.password||target.search||target.hash)throw Error('Invalid stream router origin');
 target.pathname=target.pathname.replace(/\/$/,'')+'/'+route;
 return fetch(new Request(target,req),{redirect:'error'});
}
