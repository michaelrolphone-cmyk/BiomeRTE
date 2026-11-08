import {readCheckpointRecord,recordJournalRejection} from './journal-checkpoint.mjs';
import {hexHash} from './bridge.mjs';
const archiveKey=(site,hash)=>'controller-journal/'+encodeURIComponent(site)+'/'+hash;
export function journalPointer(text){try{return JSON.parse(text).$biome_archive===1}catch{return false}}
export async function journalText(env,site,row){
 if(!row)return null;
 if(!journalPointer(row.entry))return row.entry;
 if(!env.JOURNAL_ARCHIVE)throw Error('Journal archive binding unavailable');
 const object=await env.JOURNAL_ARCHIVE.get(archiveKey(site,row.hash));
 if(!object)throw Error('Journal archive object unavailable');
 const text=await object.text();
 if(await hexHash(text)!==row.hash)throw Error('Journal archive integrity failure');
 return text;
}
export function journalEntryLimit(env){
 const limit=env.JOURNAL_MAX_ENTRY_BYTES===undefined?4194304:Number(env.JOURNAL_MAX_ENTRY_BYTES);
 if(!Number.isSafeInteger(limit)||limit<4096||limit>4194304)throw Error('JOURNAL_MAX_ENTRY_BYTES must be between 4096 and 4194304 bytes');
 return limit;
}
export function journalAdmission(env,text){
 const bytes=new TextEncoder().encode(text).length,limit=journalEntryLimit(env);
 if(bytes>limit){const error=Error('Journal transaction exceeds checkpoint input limit: '+bytes+' bytes; maximum '+limit+'. Split the transaction before retrying.');error.status=413;throw error;}
 return bytes;
}
export async function storedJournal(env,site,text,hash,existing=false){
 if(!existing)try{journalAdmission(env,text);}catch(error){if(error.status===413)await recordJournalRejection(env,site,new TextEncoder().encode(text).length);throw error;}
 if(!env.JOURNAL_ARCHIVE)return text;
 if(await hexHash(text)!==hash)throw Error('Journal source integrity failure');
 const key=archiveKey(site,hash);
 await env.JOURNAL_ARCHIVE.put(key,text,{httpMetadata:{contentType:'application/json'}});
 // Verify durable bytes before replacing the only SQL copy or publishing a head.
 const object=await env.JOURNAL_ARCHIVE.get(key);
 if(!object||await hexHash(await object.text())!==hash)throw Error('Journal archive verification failed');
 const entry=JSON.parse(text);
 return JSON.stringify({$biome_archive:1,changes:entry.changes.map(c=>[c[0],c[1]])});
}
export async function archiveJournalBatch(env,site,limit=4){
 if(!env.JOURNAL_ARCHIVE)return {moved:0,bytes_freed:0};
 const rows=(await env.DB.prepare("SELECT seq,hash,entry FROM controller_journal WHERE site=? AND length(entry)>4096 AND COALESCE(json_extract(entry,'$.$biome_archive'),0)!=1 ORDER BY seq LIMIT ?").bind(site,limit).all()).results;
 let moved=0,bytes_freed=0;
 for(const row of rows){
  const pointer=await storedJournal(env,site,row.entry,row.hash,true);
  await env.DB.prepare('UPDATE controller_journal SET entry=? WHERE site=? AND seq=? AND hash=? AND entry=?').bind(pointer,site,row.seq,row.hash,row.entry).run();
  moved++;bytes_freed+=row.entry.length-pointer.length;
 }
 if(moved)console.log('controller_journal_archived',{site,moved,bytes_freed});
 return {moved,bytes_freed};
}
export async function journalRecord(env,site,kind,id){
 const row=await env.DB.prepare("SELECT j.seq,j.hash,j.entry FROM controller_journal j WHERE j.site=? AND EXISTS (SELECT 1 FROM json_each(j.entry,'$.changes') c WHERE json_extract(c.value,'$[0]')=? AND json_extract(c.value,'$[1]')=?) ORDER BY j.seq DESC LIMIT 1").bind(site,kind,id).first();
 const snapshot=await readCheckpointRecord(env,site,kind,id);
 if(snapshot&&(!row||row.seq<=snapshot.seq))return {value:snapshot.value};
 if(!row)return null;
 const entry=JSON.parse(await journalText(env,site,row));
 return {value:entry.changes.find(c=>c[0]===kind&&c[1]===id)?.[2]??null};
}
