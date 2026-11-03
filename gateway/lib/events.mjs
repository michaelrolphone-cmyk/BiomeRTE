import {digest} from './oauth.mjs';
import {enqueue} from './bridge.mjs';

const name='workflow.delegation.requested';
const properties={delegation_id:{type:'string'},run_id:{type:'string'},workflow:{type:'string'},
 activity:{type:'string'},realm:{type:'string',enum:['site','local']}};
export const delegationEvent={name,description:'An explicit workflow step has requested work from ChatGPT. Read and claim the referenced delegation on its controller before acting.',
 delivery:['webhook'],inputSchema:{type:'object',properties:{workflow:{type:'string',description:'Exact workflow name; omit for all workflows.'},realm:{type:'string',enum:['site','local'],description:'Defaults to site.'}},additionalProperties:false},
 payloadSchema:{type:'object',properties,required:Object.keys(properties),additionalProperties:false}};

const leaseFields={id:{type:'string'},task:{type:'string'},contract:{type:'string'},resource:{type:'string'},
 generation:{type:'integer'},state:{type:'string',enum:['bound']},reply_to:{type:'string'},type:{type:'string',enum:['lease.available']},
 realm:properties.realm};
export const leaseEvent={name:'lease.available',description:'A requested lease is bound and usable. Match the inbox used in cap.acquire.',
 delivery:['webhook'],inputSchema:{type:'object',properties:{reply_to:{type:'string',minLength:1,maxLength:160},realm:properties.realm},required:['reply_to'],additionalProperties:false},
 payloadSchema:{type:'object',properties:leaseFields,required:Object.keys(leaseFields),additionalProperties:false}};

export async function eventGrant(req,env,external){
 if(!external)return null;
 const row=await env.DB.prepare("SELECT grant_id FROM biome_oauth_tokens WHERE hash=? AND kind='access' AND expires>?")
  .bind(await digest(req.headers.get('authorization').slice(7)),Math.floor(Date.now()/1000)).first();
 if(!row)throw Error('OAuth grant expired');
 return row.grant_id;
}

export async function canDeliver(db,owner,grant){
 if(typeof owner!=='string')return false;
 const bound=await db.prepare('SELECT user_id FROM biome_owner WHERE singleton=1').first();
 if(bound?.user_id!==owner)return false;
 if(grant===null)return true;
 if(typeof grant!=='string'||!grant)return false;
 const live=await db.prepare("SELECT 1 AS ok FROM biome_oauth_tokens WHERE principal=? AND grant_id=? AND kind IN ('access','refresh') AND expires>? LIMIT 1")
  .bind(owner,grant,Math.floor(Date.now()/1000)).first();
 return !!live;
}

export async function changeSubscription(env,method,params,owner,grant){
 if(!params||![name,leaseEvent.name].includes(params.name)||!params.arguments||Array.isArray(params.arguments)||typeof params.arguments!=='object'||
    !params.delivery||params.delivery.mode!=='webhook'||typeof params.delivery.url!=='string'||params.delivery.url.length>2048)
  return {error:{code:-32602,message:'Invalid event subscription'}};
 const args={owner,grant,method,params:{name:params.name,arguments:params.arguments,delivery:params.delivery}};
 if(params.ttlMs!==undefined)args.params.ttlMs=params.ttlMs;
 if(params.cursor!==undefined)args.params.cursor=params.cursor;
 const result=await enqueue(env,'mcp_event_change',args,'event-'+crypto.randomUUID());
 if(result.isError)return {error:{code:-32015,message:'Controller could not verify the subscription',data:{reason:'controller_unavailable'}}};
 return JSON.parse(result.content[0].text);
}
