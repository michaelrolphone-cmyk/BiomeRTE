import {readFile,writeFile} from 'node:fs/promises';
const modules=["lib/controller-network.mjs", "lib/capability-map.mjs", "lib/leased-stream.mjs", "lib/provider-topology.mjs", "lib/diagnostics.mjs", "lib/journal-retirement.mjs", "lib/coordination-session.mjs", "lib/journal-checkpoint.mjs", "lib/journal-upload.mjs", "lib/journal-storage.mjs", "lib/maintenance.mjs", "lib/access.mjs", "lib/oauth.mjs", "lib/bridge.mjs", "lib/ownership-transfer.mjs", "lib/ha.mjs", "lib/recovery.mjs", "lib/discovery.mjs", "lib/events.mjs", "lib/grok.mjs", "lib/agent-inbox.mjs", "lib/transport.mjs", "lib/decisions.mjs", "lib/service-authorities.mjs", "lib/native-call.mjs", "lib/dashboard-assets.mjs", "lib/dashboard-defaults.mjs", "lib/dashboard-data.mjs", "lib/dashboard.mjs", "lib/provider.mjs", "lib/console-metrics.mjs", "lib/console-read.mjs", "worker/index.js"];
const isolated=new Set(['lib/controller-network.mjs','lib/service-authorities.mjs','lib/capability-map.mjs','lib/ownership-transfer.mjs','lib/provider-topology.mjs','lib/console-read.mjs','lib/console-metrics.mjs']);
const chunks=[];
for(const file of modules){
 let source=await readFile(file,'utf8');
 source=source.replace(/^import .* from .*$/gm,'');
 if(isolated.has(file)){
  const names=[...source.matchAll(/^export (?:async )?function ([A-Za-z_$][\w$]*)/gm)].map(x=>x[1]);
  const expected=file==='lib/controller-network.mjs'?3:['lib/console-read.mjs','lib/console-metrics.mjs','lib/service-authorities.mjs'].includes(file)?1:file==='lib/capability-map.mjs'?4:file==='lib/provider-topology.mjs'?6:5;
  if(names.length!==expected||new Set(names).size!==names.length)throw new Error('Review ownership exports before bundling');
  source=source.replace(/^export /gm,'');
  if(/^export /m.test(source))throw new Error('Unsupported ownership export');
  source='export const {'+names.join(',')+'}=(()=>{\n'+source+'\nreturn {'+names.join(',')+'};\n})();';
 }
 chunks.push(source);
}
await writeFile('dist/server/index.js',chunks.join('\n'));
