import {enqueue,toolError} from './bridge.mjs';

const sections=['operations','contracts','offers','guide'];
const byteLength=value=>new TextEncoder().encode(JSON.stringify(value)).length;

export async function discoverPage(env,args){
 if(!args || typeof args!=='object' || Array.isArray(args) ||
    Object.keys(args).some(k=>!['cursor','section'].includes(k)) ||
    (args.section!==undefined && !sections.includes(args.section)))return toolError('Invalid discovery arguments.');
 let section=args.section??'all', offset=0, key, result;
 if(args.cursor!==undefined){
  const match=typeof args.cursor==='string' && /^discovery-([a-f0-9-]{36}):(all|operations|contracts|offers|guide):([0-9]{1,8})$/.exec(args.cursor);
  if(!match || (args.section && args.section!==match[2]))return toolError('Invalid discovery cursor.');
  key='discovery-'+match[1]; section=match[2]; offset=Number(match[3]);
  const row=await env.DB.prepare("SELECT result FROM bridge_jobs WHERE id=? AND state='done' AND updated>=? AND request=?").bind(key,Math.floor(Date.now()/1000)-3600,JSON.stringify({arguments:{},name:'biome_discover'})).first();
  if(!row)return toolError('Discovery snapshot expired. Start again without cursor.');
  result=JSON.parse(row.result);
 }else{
  key='discovery-'+crypto.randomUUID();
  result=await enqueue(env,'biome_discover',{},key);
 }
 if(result.isError)return result;
 const snapshot=JSON.parse(result.content[0].text);
 const records={operations:snapshot.operations,contracts:snapshot.capabilities.contracts,offers:snapshot.capabilities.offers,guide:[snapshot.capability_caller_guide]};
 const entries=[];
 for(const group of section==='all'?sections:[section]){
  for(const [index,value] of records[group].entries()){
   if(byteLength(value)<=6500){entries.push({section:group,index,value});continue;}
   // Split a single large record without splitting Unicode code points. Join
   // the fragments for this index, then JSON.parse to recover the record.
   const chars=Array.from(JSON.stringify(value));
   for(let i=0;i<chars.length;i+=500)entries.push({section:group,index,part:i/500,fragment:chars.slice(i,i+500).join('')});
  }
 }
 if(offset>entries.length)return toolError('Discovery offset out of range.');
 const items=[];
 while(offset<entries.length && (!items.length || byteLength([...items,entries[offset]])<8000))items.push(entries[offset++]);
 const next_cursor=offset<entries.length?key+':'+section+':'+offset:null;
 const page={items,next_cursor};
 if(next_cursor)page.next_call={name:'biome_invoke',arguments:{operation:'discovery.page',arguments:{cursor:next_cursor},request_id:key+'-'+offset}};
 return {content:[{type:'text',text:JSON.stringify(page)}]};
}
