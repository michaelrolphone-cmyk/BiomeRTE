import {discoverPage} from './discovery.mjs';
import {runTool,hexHash} from './bridge.mjs';
// Fixed, read-only host observation. No caller-supplied command, path or input.
const HOST_OBSERVATION="import json,os,platform,subprocess,time,re\nr={'host':platform.node(),'cpu_count':os.cpu_count(),'load_average':list(os.getloadavg()),'volumes':[]}\nfor line in subprocess.check_output(['df','-kP']).decode().splitlines()[1:]:\n p=re.split(r'\\s+',line.strip(),maxsplit=5)\n if len(p)==6 and p[0].startswith('/dev/'):\n  try:\n   v=os.statvfs(p[5]);r['volumes'].append({'device':p[0],'mount':p[5],'total_bytes':v.f_blocks*v.f_frsize,'used_bytes':(v.f_blocks-v.f_bfree)*v.f_frsize,'available_bytes':v.f_bavail*v.f_frsize})\n  except OSError:pass\nlinux=platform.system()=='Linux'\nif linux:\n m={p[0].rstrip(':'):int(p[1])*1024 for p in (x.split() for x in open('/proc/meminfo')) if len(p)>2 and p[1].isdigit()};r['memory_total_bytes']=m.get('MemTotal');r['memory_available_bytes']=m.get('MemAvailable')\nelse:r['memory_total_bytes']=int(subprocess.check_output(['sysctl','-n','hw.memsize']).decode())\ndef network():\n rows={}\n if linux:\n  for line in open('/proc/net/dev'):\n   if ':' not in line:continue\n   n,v=line.split(':',1);n=n.strip();v=v.split()\n   if n.startswith(('eth','wlan','en','wl')):rows[n]=(int(v[0]),int(v[8]))\n else:\n  for line in subprocess.check_output(['netstat','-ibn']).decode().splitlines()[1:]:\n   p=line.split()\n   if len(p)>=10 and p[0].startswith('en') and p[2].startswith('<Link'):\n    q=p[-7:];rows[p[0]]=(int(q[2]),int(q[5]))\n return rows\nfirst=network();start=time.time();time.sleep(1);second=network();end=time.time();r['observed_at']=end;r['sample_seconds']=end-start;r['network']=[]\nfor n,(rx,tx) in second.items():\n old=first.get(n);valid=old and rx>=old[0] and tx>=old[1] and end>start\n r['network'].append({'interface':n,'received_bytes':rx,'sent_bytes':tx,'rx_bytes_per_second':(rx-old[0])/(end-start) if valid else None,'tx_bytes_per_second':(tx-old[1])/(end-start) if valid else None})\nprint(json.dumps(r,separators=(',',':')))\n";
const command=['/bin/bash','-c',"exec python3 - <<'BIOME_CONSOLE_OBSERVATION'\n"+HOST_OBSERVATION+'BIOME_CONSOLE_OBSERVATION'];
const samples=new Map();let catalog=null,catalogAt=0;
function value(result){if(result.isError)throw Error(result.content?.find(c=>c.type==='text')?.text||'Host read unavailable');return result.structuredContent??JSON.parse(result.content.find(c=>c.type==='text').text);}
export async function consoleMetrics(env,{owner,realm='site',controller},deps={discoverPage,runTool,hexHash}){
 if(!catalog||Date.now()-catalogAt>60000){catalog=value(await deps.discoverPage(env,{section:'offers',limit:512}));catalogAt=Date.now();}
 const offers=(catalog.items||[]).filter(o=>o.contract==='host.terminal.run:1'&&o.state==='ready'&&(!controller||o.controller===controller));
 const unique=[...new Map(offers.map(o=>[o.controller+':'+o.resource,o])).values()];
 const hosts=await Promise.all(unique.map(async offer=>{
  const key=owner+':'+realm+':'+offer.controller+':'+offer.resource;let sample=samples.get(key);
  const invoke=async(operation,args,id)=>value(await deps.runTool(env,'biome_invoke',{operation,arguments:args,realm,controller:offer.controller,request_id:id||crypto.randomUUID()}));
  try{
   if(!sample||sample.finished&&Date.now()-sample.checkedAt>30000){
    const operation='console-observe-'+(await deps.hexHash(key+':'+Math.floor(Date.now()/30000))).slice(0,48);
    sample={operation,checkedAt:Date.now(),finished:false,previous:sample?.metrics||sample?.previous};samples.set(key,sample);
    await invoke('cap.submit',{contract:'host.terminal.run:1',resource:offer.resource,operation,inputs:{argv:command}},operation+'-submit');
   }
   if(!sample.finished){const status=await invoke('cap.status',{operation:sample.operation});sample.state=status.state;
    if(['succeeded','failed','uncertain','cancelled'].includes(status.state)){sample.finished=true;sample.checkedAt=Date.now();if(status.state==='succeeded'&&status.result?.exit_code===0){const text=status.result.stdout;if(status.result.streams?.stdout?.preview_truncated)throw Error('Host observation exceeded retained preview; no partial metrics are shown.');sample.metrics=JSON.parse(text);sample.observed_at=sample.metrics.observed_at;}else sample.error=status.result?.stderr||'Host observation '+status.state;}
   }
   const metrics=sample.metrics||sample.previous;
   return {resource:offer.resource,controller:offer.controller,provider:offer.provider,state:sample.state||'queued',operation:sample.operation,metrics:metrics||null,previous:sample.previous||null,error:sample.error||null,refreshing:!sample.finished};
  }catch(error){return {resource:offer.resource,controller:offer.controller,state:'unavailable',error:error.message,metrics:sample?.metrics||sample?.previous||null};}
 }));
 return {hosts,resources:(catalog.items||[]).map(({id,provider,resource,contract,state,capacity,configuration,controller,host,service})=>({id,provider,resource,contract,state,capacity,configuration,controller,host,service})),coverage:catalog.coverage||null,source:'host.terminal.run:1 · fixed read-only observation',sample_interval_seconds:30};
}
