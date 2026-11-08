// Prospective uploads only. A cyclic, bounded namespace sweep also discovers
// PUTs completing after their intent was retired; old archive keys are excluded.
import {hexHash} from './bridge.mjs';
const initialized=new WeakSet(),TOKEN=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const prefix=(site,namespace)=>'controller-journal-managed-v1/'+encodeURIComponent(site)+'/'+namespace+'/';
export function managedJournalKey(site,hash,token,namespace){
 if(!/^[0-9a-f]{64}$/.test(hash)||(!TOKEN.test(token)||!TOKEN.test(namespace)))throw Error('Invalid managed journal identity');
 return prefix(site,namespace)+hash+'/'+token;
}
export async function journalUploadSchema(db){
 if(initialized.has(db))return;
 for(const sql of [
  'CREATE TABLE IF NOT EXISTS journal_uploads(site TEXT,token TEXT,hash TEXT,seq INTEGER,state TEXT,expires INTEGER,PRIMARY KEY(site,token))',
  'CREATE INDEX IF NOT EXISTS journal_uploads_expiry ON journal_uploads(site,state,expires)',
  'CREATE TABLE IF NOT EXISTS journal_upload_sweeps(site TEXT PRIMARY KEY,namespace TEXT NOT NULL,cursor TEXT,lease TEXT,until INTEGER NOT NULL DEFAULT 0,cycles INTEGER NOT NULL DEFAULT 0,removed INTEGER NOT NULL DEFAULT 0)',
  `CREATE TRIGGER IF NOT EXISTS journal_upload_guard BEFORE INSERT ON controller_journal WHEN json_extract(NEW.entry,'$.$biome_upload') IS NOT NULL BEGIN SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM journal_uploads WHERE site=NEW.site AND token=json_extract(NEW.entry,'$.$biome_upload') AND hash=NEW.hash AND seq=NEW.seq AND state='pending' AND expires>unixepoch() AND json_extract(NEW.entry,'$.$biome_upload_namespace')=(SELECT namespace FROM journal_upload_sweeps WHERE site=NEW.site)) THEN RAISE(ABORT,'Journal upload lease unavailable') END; END`,
  `CREATE TRIGGER IF NOT EXISTS journal_upload_commit AFTER INSERT ON controller_journal WHEN json_extract(NEW.entry,'$.$biome_upload') IS NOT NULL BEGIN UPDATE journal_uploads SET state='committed',expires=0 WHERE site=NEW.site AND token=json_extract(NEW.entry,'$.$biome_upload'); END`,
  `CREATE TRIGGER IF NOT EXISTS journal_upload_retire AFTER DELETE ON controller_journal WHEN json_extract(OLD.entry,'$.$biome_upload') IS NOT NULL BEGIN DELETE FROM journal_uploads WHERE site=OLD.site AND token=json_extract(OLD.entry,'$.$biome_upload') AND state='committed'; END`
 ])await db.prepare(sql).run();
 initialized.add(db);
}
export async function managedJournalNamespace(db,site){
 await journalUploadSchema(db);
 await db.prepare('INSERT OR IGNORE INTO journal_upload_sweeps(site,namespace) VALUES(?,?)').bind(site,crypto.randomUUID()).run();
 return (await db.prepare('SELECT namespace FROM journal_upload_sweeps WHERE site=?').bind(site).first()).namespace;
}
export async function storeManagedJournal(env,site,text,hash){
 await journalUploadSchema(env.DB);
 if(typeof env.JOURNAL_ARCHIVE.list!=='function')throw Error('Managed journal upload requires bounded archive listing');
 const entry=JSON.parse(text),token=crypto.randomUUID(),namespace=await managedJournalNamespace(env.DB,site),key=managedJournalKey(site,hash,token,namespace);
 // A token is never reused or renewed. Retried appends create different keys.
 const admitted=await env.DB.prepare("INSERT INTO journal_uploads SELECT ?,?,?,?,'pending',unixepoch()+600 WHERE (SELECT count(*) FROM journal_uploads WHERE site=? AND state IN ('pending','collecting'))<256 RETURNING token").bind(site,token,hash,entry.seq,site).first();
 if(!admitted)throw Error('Journal upload backlog full; retry after maintenance');
 await env.JOURNAL_ARCHIVE.put(key,text,{httpMetadata:{contentType:'application/json'}});
 const object=await env.JOURNAL_ARCHIVE.get(key);
 if(!object||await hexHash(await object.text())!==hash)throw Error('Managed journal upload verification failed');
 return JSON.stringify({$biome_archive:1,$biome_upload:token,$biome_upload_namespace:namespace,changes:entry.changes.map(c=>[c[0],c[1]])});
}
async function collectIntent(env,site,token,namespace){
 // Claim and journal INSERT serialize in D1: the INSERT trigger atomically
 // changes pending to committed. A collector can never claim committed data.
 const row=await env.DB.prepare("UPDATE journal_uploads SET state='collecting' WHERE site=? AND token=? AND ((state='pending' AND expires<=unixepoch()) OR state='collecting') RETURNING hash,token").bind(site,token).first();
 if(!row)return false;
 await env.JOURNAL_ARCHIVE.delete(managedJournalKey(site,row.hash,row.token,namespace));
 await env.DB.prepare("DELETE FROM journal_uploads WHERE site=? AND token=? AND state='collecting'").bind(site,token).run();
 return true;
}
export async function collectJournalUploads(env,site,limit=32){
 if(env.JOURNAL_MANAGED_UPLOADS!=='1'||!env.JOURNAL_ARCHIVE)return {enabled:false};
 if(!Number.isInteger(limit)||limit<1||limit>32)throw Error('Invalid upload collection page limit');
 await journalUploadSchema(env.DB);
 const namespace=await managedJournalNamespace(env.DB,site);
 const lease=crypto.randomUUID(),sweep=await env.DB.prepare('UPDATE journal_upload_sweeps SET lease=?,until=unixepoch()+600 WHERE site=? AND until<=unixepoch() RETURNING cursor').bind(lease,site).first();
 if(!sweep)return {busy:true};
 let removed=0;
 try{
  const pending=(await env.DB.prepare("SELECT token FROM journal_uploads WHERE site=? AND state IN ('pending','collecting') AND expires<=unixepoch() ORDER BY expires LIMIT ?").bind(site,limit).all()).results;
  for(const row of pending)if(await collectIntent(env,site,row.token,namespace))removed++;
  const page=await env.JOURNAL_ARCHIVE.list({prefix:prefix(site,namespace),limit,...(sweep.cursor?{cursor:sweep.cursor}:{})});
  if(!Array.isArray(page.objects)||page.objects.length>limit||typeof page.truncated!=='boolean'||(page.truncated&&(typeof page.cursor!=='string'||!page.cursor)))throw Error('Invalid managed journal listing');
  for(const object of page.objects){
   if(typeof object.key!=='string'||!object.key.startsWith(prefix(site,namespace)))throw Error('Managed journal listing escaped namespace');
   const parts=object.key.slice(prefix(site,namespace).length).split('/');
   if(parts.length!==2||!TOKEN.test(parts[1])||!/^[0-9a-f]{64}$/.test(parts[0]))continue; // never delete foreign/unrecognized keys
   const [hash,token]=parts,row=await env.DB.prepare('SELECT hash,state,expires FROM journal_uploads WHERE site=? AND token=?').bind(site,token).first();
   if(row){if(row.hash!==hash)continue;if(await collectIntent(env,site,token,namespace))removed++;}
   else{
    // The token's intent must exist before any PUT starts and is never reused.
    // No future journal commit can pass its trigger after that intent is gone.
    // A late PUT is safe to delete; if it finishes later still, a future cycle
    // sees it again. No persistent per-object tombstone is required.
    await env.JOURNAL_ARCHIVE.delete(object.key);removed++;
   }
  }
  const saved=await env.DB.prepare('UPDATE journal_upload_sweeps SET cursor=?,cycles=cycles+?,removed=removed+? WHERE site=? AND lease=? AND until>unixepoch() RETURNING site').bind(page.truncated?page.cursor:null,page.truncated?0:1,removed,site,lease).first();
  if(!saved)throw Error('Journal upload sweep lease expired');
  return {enabled:true,removed,listed:page.objects.length,cycle_complete:!page.truncated};
 }finally{await env.DB.prepare('UPDATE journal_upload_sweeps SET until=0 WHERE site=? AND lease=?').bind(site,lease).run();}
}
