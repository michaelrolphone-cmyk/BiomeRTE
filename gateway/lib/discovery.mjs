import {runTool,toolError} from './bridge.mjs';

const sections=['operations','contracts','offers','guide'];

// The controller owns discovery pagination. Its default response is already a
// page, not the former full snapshot; never decode it as snapshot.capabilities.
export async function discoverPage(env,args={},realm='site'){
 if(!args || typeof args!=='object' || Array.isArray(args) ||
    Object.keys(args).some(k=>!['cursor','section','limit'].includes(k)) ||
    (args.section!==undefined && !sections.includes(args.section)) ||
    (args.cursor!==undefined && typeof args.cursor!=='string') ||
    !['site','local'].includes(realm))return toolError('Invalid discovery arguments.');
 if(args.limit!==undefined&&(!Number.isSafeInteger(args.limit)||args.limit<1))return toolError('Discovery limit must be a positive integer.');
 if(args.cursor?.startsWith('discovery-'))return toolError('Legacy discovery snapshot expired. Start again without cursor.');
 return runTool(env,'biome_invoke',{
  operation:'system.discovery',arguments:args,realm,
  request_id:'discovery-'+crypto.randomUUID()
 });
}
