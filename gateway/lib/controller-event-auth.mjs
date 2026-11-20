import {sameSecret} from './access.mjs';
import {canDeliver} from './events.mjs';
import {limitedJSON,reply} from './bridge.mjs';
export async function controllerEventAuthorization(req,env){
 if(req.method!=='POST')return reply({error:'Use POST'},405);
 if(req.headers.has('Origin'))return reply({error:'Untrusted origin'},403);
 const key=env.CONTROLLER_EVENT_AUTH_SECRET;
 if(typeof key!=='string'||key.length<32)return reply({error:'Authorization unavailable'},503);
 if(!sameSecret(req.headers.get('Authorization'),'Bearer '+key))return reply({error:'Unauthorized'},401);
 let body;try{body=await limitedJSON(req,4096)}catch{return reply({error:'Invalid authorization request'},400)}
 if(!body||typeof body!=='object'||Array.isArray(body)||Object.keys(body).sort().join(',')!=='grant,owner'||
    typeof body.owner!=='string'||!body.owner||body.owner.length>256||
    body.grant!==null&&(typeof body.grant!=='string'||!body.grant||body.grant.length>256))
  return reply({error:'Invalid authorization request'},400);
 return reply({allowed:await canDeliver(env.DB,body.owner,body.grant)});
}
