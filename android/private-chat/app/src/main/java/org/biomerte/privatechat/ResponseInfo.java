package org.biomerte.privatechat;
import org.json.*;
/** Per-response provenance is stored only inside the encrypted phone vault. */
final class ResponseInfo {
 static JSONObject capture(JSONObject pending,JSONObject reply)throws JSONException{
  JSONObject info=new JSONObject().put("v",1).put("requested_model",pending.optString("selected_model")).put("response_model",reply.optString("model")).put("request_id",Requests.id(pending)).put("received_at",System.currentTimeMillis());
  if(pending.has("submitted_at"))info.put("submitted_at",pending.getLong("submitted_at"));
  JSONObject envelope=pending.optJSONObject("envelope");if(envelope!=null)info.put("session_id",envelope.optString("sid"));
  JSONObject remote=reply.optJSONObject("diagnostics");
  if(remote!=null){JSONObject safe=new JSONObject();for(String key:new String[]{"v","requested_model","resolved_model","response_model","model_verified","admission_ms","inference_ms","finish_reason"})if(remote.has(key))safe.put(key,remote.get(key));
   JSONObject usage=remote.optJSONObject("usage");if(usage!=null){JSONObject tokens=new JSONObject();for(String key:new String[]{"prompt_tokens","completion_tokens","total_tokens"})if(usage.has(key)&&usage.optLong(key,-1)>=0)tokens.put(key,usage.getLong(key));safe.put("usage",tokens);}info.put("server",safe);
  }
  if(!pending.optBoolean("context_redacted")&&pending.optJSONArray("request_messages")!=null)info.put("context",new JSONArray(pending.getJSONArray("request_messages").toString()));
  else info.put("context_state",pending.optBoolean("context_redacted")?"removed_after_deletion":"not_recorded");
  return info;
 }
 static boolean verified(JSONObject info){
  if(info==null)return false;JSONObject server=info.optJSONObject("server");if(server==null||!server.optBoolean("model_verified"))return false;
  String actual=info.optString("response_model"),requested=info.optString("requested_model");return !actual.isEmpty()&&!requested.isEmpty()&&requested.equals(server.optString("requested_model"))&&actual.equals(server.optString("resolved_model"))&&actual.equals(server.optString("response_model"));
 }
 static String value(JSONObject data,String key){String value=data.optString(key);return value.isEmpty()?"Not recorded":value;}
 static String date(JSONObject info,String key){long time=info.optLong(key,0);return time<=0?"Not recorded":java.text.DateFormat.getDateTimeInstance().format(new java.util.Date(time));}
 static String summary(JSONObject message){
  JSONObject info=message.optJSONObject("info");if(info==null)return "Older response\n\nModel label: "+value(message,"model")+"\nVerification and sent context were not recorded for this response.";
  StringBuilder out=new StringBuilder(verified(info)?"Model match verified by Biome":"Model verification unavailable");
  out.append("\n\nRequested model: ").append(value(info,"requested_model")).append("\nReported model: ").append(value(info,"response_model"));
  JSONObject server=info.optJSONObject("server");if(server!=null){out.append("\nLoaded instance: ").append(value(server,"resolved_model"));if(!verified(info)&&server.optBoolean("model_verified"))out.append("\nWarning: recorded model details do not agree.");
   if(server.has("admission_ms"))out.append("\nQueue and model preparation: ").append(server.optLong("admission_ms")).append(" ms");if(server.has("inference_ms"))out.append("\nLM Studio response time: ").append(server.optLong("inference_ms")).append(" ms");out.append("\nFinish reason: ").append(value(server,"finish_reason"));JSONObject tokens=server.optJSONObject("usage");if(tokens!=null)for(String key:new String[]{"prompt_tokens","completion_tokens","total_tokens"})if(tokens.has(key))out.append("\n").append(key.replace('_',' ')).append(": ").append(tokens.optLong(key));
  }
  out.append("\n\nSubmitted on phone: ").append(date(info,"submitted_at")).append("\nReceived on phone: ").append(date(info,"received_at")).append("\nRequest ID: ").append(value(info,"request_id")).append("\nSession ID: ").append(value(info,"session_id"));
  if(info.optJSONArray("context")==null)out.append(info.optString("context_state").equals("removed_after_deletion")?"\n\nSent context removed after a message was deleted.":"\n\nSent context was not recorded for this older request.");return out.toString();
 }
 static String contextText(JSONArray context)throws JSONException{StringBuilder text=new StringBuilder();for(int i=0;i<context.length();i++){JSONObject message=context.getJSONObject(i);if(i>0)text.append("\n\n────────\n\n");text.append(i+1).append(" · ").append(message.getString("role")).append("\n").append(message.getString("content"));}return text.toString();}
 static void clearContexts(JSONObject data,String thread)throws JSONException{
  JSONArray messages=Conversations.find(data,thread).getJSONArray("messages");for(int i=0;i<messages.length();i++){JSONObject info=messages.getJSONObject(i).optJSONObject("info");if(info!=null){info.remove("context");info.put("context_state","removed_after_deletion");}}
  JSONArray requests=data.optJSONArray("requests");if(requests!=null)for(int i=0;i<requests.length();i++){JSONObject p=requests.getJSONObject(i);if(thread.equals(p.optString("thread_id"))){p.put("context_redacted",true);if(p.optString("stage").equals("message"))p.remove("request_messages");}}
 }
}
