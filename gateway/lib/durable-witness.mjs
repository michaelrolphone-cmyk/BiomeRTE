// Bounded durable coordination metadata. No payload, history, or execution journal.
const protocol='biomerte-durable-witness/1';
const initialized=new WeakMap();
const nowSQL="(CAST(strftime('%s','now') AS REAL)+(CAST(strftime('%f','now') AS REAL)-CAST(strftime('%S','now') AS REAL)))";
const object=v=>v&&typeof v==='object'&&!Array.isArray(v);
const hash=v=>typeof v==='string'&&/^[0-9a-f]{64}$/.test(v);
const identity=v=>typeof v==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(v);
const error=(message,status=400)=>Object.assign(Error(message),{status});
const response=(value,status=200)=>Response.json(value,{status,headers:{'Cache-Control':'no-store'}});
function exact(value,allowed){
 if(!object(value)||Object.keys(value).some(k=>!allowed.includes(k)))throw error('Unexpected witness fields');
}
function ref(value){
 exact(value,['sequence','digest']);
 if(!Number.isSafeInteger(value.sequence)||value.sequence<0||!hash(value.digest))throw error('Exact checkpoint reference required');
 return value;
}
async function authorized(req,env){
 if(req.headers.get('Origin'))throw error('Origin is not permitted',403);
 if(typeof env.DURABLE_WITNESS_SECRET!=='string'||env.DURABLE_WITNESS_SECRET.length<32)
  throw error('Durable witness is not configured',503);
 const encoder=new TextEncoder();
 const [a,b]=await Promise.all([
  crypto.subtle.digest('SHA-256',encoder.encode(req.headers.get('Authorization')??'')),
  crypto.subtle.digest('SHA-256',encoder.encode('Bearer '+env.DURABLE_WITNESS_SECRET))]);
 const left=new Uint8Array(a),right=new Uint8Array(b);let difference=0;
 for(let i=0;i<left.length;i++)difference|=left[i]^right[i];
 if(difference)throw error('Unauthorized',401);
}
async function schema(db){
 let pending=initialized.get(db);
 if(!pending){
  pending=(async()=>{
   await db.prepare('CREATE TABLE IF NOT EXISTS service_witness_leases(witness TEXT PRIMARY KEY,epoch INTEGER NOT NULL,boot_id TEXT NOT NULL,expires_at REAL NOT NULL,ready_at REAL NOT NULL,max_grant_s REAL NOT NULL)').run();
   await db.prepare('CREATE TABLE IF NOT EXISTS service_witness_heads(witness TEXT NOT NULL,service_id TEXT NOT NULL,scope_digest TEXT NOT NULL,sequence INTEGER NOT NULL,digest TEXT NOT NULL,committed_epoch INTEGER NOT NULL,PRIMARY KEY(witness,service_id))').run();
  })();initialized.set(db,pending);
 }
 try{await pending}catch(e){initialized.delete(db);throw e}
}
async function lease(db,witness){
 return db.prepare('SELECT *, '+nowSQL+' AS server_now FROM service_witness_leases WHERE witness=?').bind(witness).first();
}
function owner(value){
 exact(value,['boot_id','epoch']);
 if(!identity(value.boot_id)||value.boot_id.length<16||!Number.isSafeInteger(value.epoch)||value.epoch<1)
  throw error('Exact witness epoch owner required');
 return value;
}
function current(row,who,ready=false){
 if(!row||row.boot_id!==who.boot_id||row.epoch!==who.epoch||row.expires_at<=row.server_now||
    ready&&row.ready_at>row.server_now)throw error('Witness lease is stale or quarantined',409);
 return row;
}
const leaseSQL='EXISTS(SELECT 1 FROM service_witness_leases WHERE witness=? AND epoch=? AND boot_id=? AND expires_at>'+nowSQL+' AND ready_at<='+nowSQL+')';
export async function durableWitness(req,env){
 try{
  if(req.method!=='POST')throw error('POST required',405);
  if(new URL(req.url).protocol!=='https:')throw error('HTTPS required',403);
  await authorized(req,env);
  const raw=await req.text();if(new TextEncoder().encode(raw).length>16384)throw error('Witness metadata request too large',413);
  let value;try{value=JSON.parse(raw)}catch{throw error('JSON metadata required')}
  exact(value,['protocol','operation','witness','boot_id','lease_s','max_grant_s','owner','cursor','service_id','scope_digest','checkpoint','expected','previous_digest']);
  if(value.protocol!==protocol||!identity(value.witness)||value.witness!==(env.DURABLE_WITNESS_ID??'biomerte-services'))throw error('Witness protocol and configured identity required');
  const db=env.DB;await schema(db);
  const witness=value.witness,operation=value.operation;
  if(operation==='acquire'){
   if(!identity(value.boot_id)||value.boot_id.length<16||
      typeof value.lease_s!=='number'||!Number.isFinite(value.lease_s)||value.lease_s<=0||value.lease_s>600||
      typeof value.max_grant_s!=='number'||!Number.isFinite(value.max_grant_s)||value.max_grant_s<=0||
      value.max_grant_s>300||value.lease_s<2*value.max_grant_s)throw error('Bounded witness lease and grant interval required');
   await db.prepare('INSERT INTO service_witness_leases(witness,epoch,boot_id,expires_at,ready_at,max_grant_s) VALUES(?,1,?,'+nowSQL+'+?,'+nowSQL+'+?,?) ON CONFLICT(witness) DO UPDATE SET epoch=service_witness_leases.epoch+1,boot_id=excluded.boot_id,expires_at='+nowSQL+'+?,ready_at='+nowSQL+'+MAX(service_witness_leases.max_grant_s,excluded.max_grant_s),max_grant_s=excluded.max_grant_s WHERE service_witness_leases.expires_at<='+nowSQL)
    .bind(witness,value.boot_id,value.lease_s,value.max_grant_s,value.max_grant_s,value.lease_s).run();
   const row=await lease(db,witness);
   if(row.boot_id!==value.boot_id||row.expires_at<=row.server_now)
    return response({protocol,error:'Another witness still owns its lease',retry_after_s:Math.max(0,row.expires_at-row.server_now)},409);
   return response({protocol,lease:row});
  }
  const who=owner(value.owner);
  if(operation==='renew'){
   if(typeof value.lease_s!=='number'||!Number.isFinite(value.lease_s)||value.lease_s<=0||value.lease_s>600)
    throw error('Bounded witness lease required');
   await db.prepare('UPDATE service_witness_leases SET expires_at='+nowSQL+'+? WHERE witness=? AND epoch=? AND boot_id=? AND expires_at>'+nowSQL+' AND ? >= 2*max_grant_s')
    .bind(value.lease_s,witness,who.epoch,who.boot_id,value.lease_s).run();
   return response({protocol,lease:current(await lease(db,witness),who)});
  }
  current(await lease(db,witness),who);
  if(operation==='heads'){
   if(value.service_id!==undefined){
    if(!identity(value.service_id))throw error('Service identity required');
    const head=await db.prepare('SELECT service_id,scope_digest,sequence,digest,committed_epoch FROM service_witness_heads WHERE witness=? AND service_id=?').bind(witness,value.service_id).first();
    return response({protocol,head:head??null});
   }
   const cursor=value.cursor??'';if(cursor!==''&&!identity(cursor))throw error('Invalid witness cursor');
   const rows=(await db.prepare('SELECT service_id,scope_digest,sequence,digest,committed_epoch FROM service_witness_heads WHERE witness=? AND service_id>? ORDER BY service_id LIMIT 64').bind(witness,cursor).all()).results;
   return response({protocol,heads:rows,next:rows.length===64?rows[rows.length-1].service_id:null});
  }
  if(operation==='commit'){
   if(!identity(value.service_id)||!hash(value.scope_digest))throw error('Service and fixed checkpoint scope required');
   const candidate=ref(value.checkpoint),expected=value.expected===null?null:ref(value.expected);
   if(value.previous_digest!==''&&!hash(value.previous_digest))throw error('Exact parent digest required');
   current(await lease(db,witness),who,true);
   const grant=[witness,who.epoch,who.boot_id];
   // The two conditional mutations form one atomic D1 batch. Exact retries
   // succeed; a different history cannot overwrite a later durable reference.
   const update=db.prepare('UPDATE service_witness_heads SET sequence=?,digest=?,committed_epoch=? WHERE witness=? AND service_id=? AND scope_digest=? AND ((sequence=? AND digest=?) OR (sequence=? AND digest=? AND ? > sequence AND ?=digest)) AND '+leaseSQL)
    .bind(candidate.sequence,candidate.digest,who.epoch,witness,value.service_id,value.scope_digest,
          candidate.sequence,candidate.digest,expected?.sequence??null,expected?.digest??null,
          candidate.sequence,value.previous_digest,...grant);
   const insert=db.prepare('INSERT OR IGNORE INTO service_witness_heads(witness,service_id,scope_digest,sequence,digest,committed_epoch) SELECT ?,?,?,?,?,? WHERE ? IS NULL AND '+leaseSQL)
    .bind(witness,value.service_id,value.scope_digest,candidate.sequence,candidate.digest,who.epoch,
          expected?.sequence??null,...grant);
   await db.batch([update,insert]);
   current(await lease(db,witness),who,true);
   const head=await db.prepare('SELECT service_id,scope_digest,sequence,digest,committed_epoch FROM service_witness_heads WHERE witness=? AND service_id=?').bind(witness,value.service_id).first();
   if(!head||head.scope_digest!==value.scope_digest||head.sequence!==candidate.sequence||head.digest!==candidate.digest)
    return response({protocol,error:'Checkpoint compare-and-swap conflict',head:head??null},409);
   return response({protocol,head,durable:true});
  }
  throw error('Unknown witness operation');
 }catch(e){
  return response({protocol,error:e.status?e.message:'Durable witness unavailable'},e.status??503);
 }
}
