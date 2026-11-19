// Diagnostic metadata only. Never log bodies, signatures, credentials, native
// request IDs, arbitrary exception messages, SQL parameters or controller text.
const activeQueueGroupLimit=64;

export async function activeQueueDiagnostics(db,now=Date.now()/1000) {
 // Two disjoint indexed ranges include dispatch-eligible queued/claimed rows
 // and still-owned claims that outlive their dispatch deadline. Expired work
 // never enters JSON normalization; request bodies stay inside SQL.
 // No queue/provider action, renewal, cleanup or new diagnostic table is used.
 const rows=(await db.prepare(`
  WITH eligible AS (
   SELECT state,controller,updated,request
   FROM bridge_jobs INDEXED BY bridge_dispatch
   WHERE state IN ('queued','claimed') AND deadline>=?
   UNION ALL
   SELECT state,controller,updated,request
   FROM bridge_jobs INDEXED BY bridge_live_claims
   WHERE state='claimed' AND lease_until>? AND deadline<?
  ), active AS (
   SELECT state,controller,updated,
    CASE WHEN json_valid(request) THEN request ELSE '{}' END AS doc
   FROM eligible
  ), operations AS (
   SELECT state,controller,updated,
    CASE json_extract(doc,'$.name')
     WHEN 'biome_invoke' THEN json_extract(doc,'$.arguments.operation')
     WHEN 'signed_provider_call' THEN
      CASE WHEN json_type(doc,'$.arguments.envelope')='text' THEN
       CASE WHEN json_valid(json_extract(doc,'$.arguments.envelope')) THEN
        json_extract(json_extract(doc,'$.arguments.envelope'),'$.operation') END END
     WHEN 'biome_native_call' THEN
      CASE WHEN json_type(doc,'$.arguments.body')='text' THEN
       CASE WHEN json_valid(json_extract(doc,'$.arguments.body')) THEN
        json_extract(json_extract(doc,'$.arguments.body'),'$.operation') END END
     WHEN 'signed_provider_pair' THEN
      CASE json_extract(doc,'$.arguments.phase') WHEN 'begin' THEN 'provider.pair.begin'
       WHEN 'finish' THEN 'provider.pair.finish' ELSE 'unknown' END
     WHEN 'biome_status' THEN 'biome_status'
     WHEN 'biome_discover' THEN 'biome_discover'
     WHEN 'biome_work_list' THEN 'biome_work_list'
     WHEN 'biome_dashboard_call' THEN 'biome_dashboard_call'
     WHEN 'biome_dashboard_chunk' THEN 'biome_dashboard_chunk'
     WHEN 'sealed_grok_receiver' THEN 'sealed_grok_receiver'
     WHEN 'mcp_event_change' THEN 'mcp_event_change'
     ELSE 'unknown' END AS operation
   FROM active
  ), normalized AS (
   SELECT state,updated,
    CASE WHEN controller IS NULL OR controller='' THEN NULL
     WHEN length(controller)<=128 AND controller NOT GLOB '*[^A-Za-z0-9_.-]*'
      THEN controller ELSE 'unknown' END AS controller,
    CASE WHEN typeof(operation)='text' AND length(operation) BETWEEN 1 AND 128
     AND operation GLOB '[a-z]*' AND operation NOT GLOB '*[^a-z0-9_.]*'
     AND (instr(operation,'.')>0 OR operation IN ('unknown','biome_status','biome_discover',
      'biome_work_list','biome_dashboard_call','biome_dashboard_chunk','sealed_grok_receiver','mcp_event_change'))
      THEN operation ELSE 'unknown' END AS operation
   FROM operations
  ), grouped AS (
   SELECT state,controller,operation,COUNT(*) AS count,MIN(updated) AS oldest_update
   FROM normalized GROUP BY state,controller,operation
  )
  SELECT state,controller,operation,count,oldest_update,
   SUM(count) OVER () AS active_jobs,COUNT(*) OVER () AS group_count
  FROM grouped
  ORDER BY count DESC,oldest_update,state,controller,operation
  LIMIT ?
 `).bind(now,now,now,activeQueueGroupLimit).all()).results;
 // updated is reset by claiming, deadline refresh and result-lease renewal.
 // This is age since the last bridge-row update, not end-to-end command age.
 return {observed_at:now,age_basis:'last_bridge_job_update',active_basis:'dispatch_deadline_or_live_claim',
  active_jobs:rows[0]?.active_jobs??0,group_count:rows[0]?.group_count??0,
  limit:activeQueueGroupLimit,truncated:(rows[0]?.group_count??0)>rows.length,
  groups:rows.map(row=>({state:row.state,controller:row.controller,operation:row.operation,
   count:row.count,oldest_state_age_seconds:Math.max(0,Math.round((now-row.oldest_update)*1000)/1000)}))};
}

export function gatewayErrorKind(error) {
 const message=typeof error==='string'?error:typeof error?.message==='string'?error.message:'';
 const journal=/^Gateway journal(?: HTTP)? (5\d\d)$/.exec(message);
 if(journal)return 'journal_http_'+journal[1];
 if(message==='Gateway journal unavailable')return 'journal_unavailable';
 if(message==='Incomplete controller result')return 'result_incomplete';
 if(message==='Controller result digest mismatch')return 'result_digest_mismatch';
 if(['Invalid controller result','Invalid stored controller result'].includes(message))return 'result_invalid';
 if(error instanceof SyntaxError)return 'invalid_json';
 if(/\bD1_ERROR\b/.test(message))return 'database_error';
 if(error instanceof TypeError)return 'type_error';
 return 'unclassified';
}
export function logProviderFailure(req,diagnostic,status,error) {
 // Logging is observational. Even a failed logger cannot change a response.
 try {
  const record={schema:1,route:new URL(req.url).pathname,status,
   stage:diagnostic.stage,reason:diagnostic.reason,
   elapsed_ms:Math.max(0,Math.round(performance.now()-diagnostic.started)),
   error_kind:gatewayErrorKind(error)};
  const ray=req.headers.get('cf-ray');
  if(ray&&/^[a-f0-9]{16}(?:-[A-Za-z]{3})?$/.test(ray))record.ray_id=ray;
  for(const field of ['bridge_job','operation','result_source','job_state','poll_count','queue_capacity','controller_status'])
   if(diagnostic[field]!==undefined)record[field]=diagnostic[field];
  console.error('provider_gateway_failure',record);
 } catch {}
}
