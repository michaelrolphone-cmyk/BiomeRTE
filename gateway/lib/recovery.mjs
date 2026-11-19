import {journalRecord,storedJournal,archiveJournalBatch} from './journal-storage.mjs';
import {controllerStatus} from './ha.mjs';
import {hexHash} from './bridge.mjs';
export const liveAt=function liveAt(nodes){const roots=x=>{let r=[];if(Array.isArray(x))for(const a of x)r.push(...roots(a));else if(x&&typeof x==="object"){if(Object.keys(x).length===1&&typeof x.$==="string")r.push(x.$.split(".")[0]);else for(const v of Object.values(x))r.push(...roots(v));}return r;};const live=Object.fromEntries(Object.keys(nodes).map(k=>[k,new Set()]));let changed=true;while(changed){changed=false;for(const [id,n] of Object.entries(nodes)){const s=new Set(roots(n));let next=n.type==="choose"?[...n.cases.map(c=>c.to),n.default]:n.type==="fork"?n.branches:["end","fail"].includes(n.type)?[]:[n.next,...(n.type==="each"?[n.body]:[])];const kills=new Set([n.save,...(n.type==="set"?Object.keys(n.values):[])]);for(const dest of next)for(const k of live[dest]??[])if(!kills.has(k))s.add(k);for(const k of live[n.on_error]??[])s.add(k);if([...s].some(k=>!live[id].has(k))){live[id]=s;changed=true;}}}return live;};
async function latest(env,site,kind,id){
 return (await journalRecord(env,site,kind,id))?.value??null;
}
function ordered(v){return Array.isArray(v)?v.map(ordered):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,ordered(v[k])])):v}
export async function compactRuns(env,args,requestId){
 if(env.CONTROLLER_COORDINATION_MODE==='local')throw Error('Local SQLite recovery must run on its owning controller');
 const db=env.DB;
 if(!Number.isInteger(args.expected_head)||!Array.isArray(args.run_ids)||args.run_ids.length<1||args.run_ids.some(id=>typeof id!=='string'))throw Error('Expected head and run IDs');
 if(args.mode && !['suspend','resume'].includes(args.mode))throw Error('Invalid recovery mode');
 const cluster=await controllerStatus(db);
 const prior=await latest(env,cluster.site,'recovery_compactions',requestId);
 if(prior)return prior;
 if(cluster.head.seq!==args.expected_head)throw Error('Shared head advanced; inspect before recovery');
 const changes=[],removed=[];
 for(const id of args.run_ids){
  const run=await latest(env,cluster.site,'runs',id);
  if(!run?.name?.startsWith('assistant.'))throw Error('Only assistant temporary variables can be compacted');
  if(args.mode==='suspend' && run.state==='running'){run.state='suspended';run.recovery_suspended_by=requestId;}
  if(args.mode==='resume'){if(run.state!=='suspended'||!run.recovery_suspended_by)throw Error('Run is not recovery-suspended');run.state='running';delete run.recovery_suspended_by;}
  const version=await latest(env,cluster.site,'process_versions',run.rev);
  const live=liveAt(version.graph.nodes);
  for(const token of run.tokens){
   if(!live[token.at])throw Error('Unknown token activity');
   for(const key of Object.keys(token.vars)){
    if(key in token.vars&&!live[token.at].has(key)){delete token.vars[key];removed.push({run:id,token:token.id,variable:key});}
   }
  }
  changes.push(['runs',id,run]);
 }
 const receipt={request_id:requestId,previous_head:cluster.head.seq,removed};
 changes.push(['recovery_compactions',requestId,receipt]);
 const entry=JSON.stringify(ordered({seq:cluster.head.seq+1,prev:cluster.head.hash,changes})).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
 const hash=await hexHash(entry);
 await archiveJournalBatch(env,cluster.site);
 const stored=await storedJournal(env,cluster.site,entry,hash);
 await db.prepare("INSERT OR IGNORE INTO controller_journal(site,seq,hash,entry) SELECT ?,?,?,? WHERE COALESCE((SELECT MAX(seq) FROM controller_journal WHERE site=?),0)=? AND COALESCE((SELECT hash FROM controller_journal WHERE site=? ORDER BY seq DESC LIMIT 1),'')=?").bind(cluster.site,cluster.head.seq+1,hash,stored,cluster.site,cluster.head.seq,cluster.site,cluster.head.hash).run();
 const row=await db.prepare('SELECT hash FROM controller_journal WHERE site=? AND seq=?').bind(cluster.site,cluster.head.seq+1).first();
 if(row?.hash!==hash)throw Error('Shared head advanced; recovery not applied');
 return {...receipt,head:cluster.head.seq+1};
}

