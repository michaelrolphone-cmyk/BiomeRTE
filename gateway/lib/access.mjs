export const privateHeaders={'Cache-Control':'private, no-store',Pragma:'no-cache',Vary:'Cookie, Authorization'};
export function sameSecret(a,b){if(!a||!b)return false;let difference=a.length^b.length;for(let i=0;i<Math.max(a.length,b.length);i++)difference|=(a.charCodeAt(i)||0)^(b.charCodeAt(i)||0);return difference===0;}
export async function ownerPrincipal(req,env){
 const id=req.headers.get('oai-authenticated-user-id'),email=req.headers.get('oai-authenticated-user-email');
 if(!id||!email||!env.BIOME_OWNER_EMAIL||email.toLowerCase()!==env.BIOME_OWNER_EMAIL.toLowerCase())return null;
 let row=await env.DB.prepare('SELECT user_id FROM biome_owner WHERE singleton=1').first();
 if(!row){await env.DB.prepare('INSERT OR IGNORE INTO biome_owner(singleton,user_id) VALUES(1,?)').bind(id).run();row=await env.DB.prepare('SELECT user_id FROM biome_owner WHERE singleton=1').first();}
 return row?.user_id===id?id:null;
}
