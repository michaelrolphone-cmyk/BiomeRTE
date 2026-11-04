package org.biomerte.privatechat;
import org.json.*;
/** Durable request identities stay in the encrypted vault across process death. */
final class Requests {
 static String id(JSONObject p)throws JSONException{String id=p.optString("request_id");if(id.isEmpty()&&p.optJSONObject("envelope")!=null)id=p.getJSONObject("envelope").optString("id");if(id.isEmpty()&&p.optJSONObject("hello")!=null)id=p.getJSONObject("hello").optString("id");if(id.isEmpty())throw new JSONException("Request identity missing");return id;}
 static JSONArray all(JSONObject d)throws JSONException{if(!d.has("requests"))d.put("requests",new JSONArray());return d.getJSONArray("requests");}
 static void migrate(JSONObject d)throws JSONException{
  Conversations.ensure(d);JSONObject legacy=d.optJSONObject("pending");if(legacy!=null){add(d,legacy);d.remove("pending");}
  JSONArray saved=d.optJSONArray("suspended_requests");if(saved!=null){for(int i=0;i<saved.length();i++)add(d,saved.getJSONObject(i));d.remove("suspended_requests");}
 }
 static void add(JSONObject d,JSONObject p)throws JSONException{String key=id(p);JSONArray a=all(d);for(int i=0;i<a.length();i++)if(id(a.getJSONObject(i)).equals(key))return;a.put(p);}
 static void remove(JSONObject d,String id)throws JSONException{JSONArray a=all(d);for(int i=a.length()-1;i>=0;i--)if(id(a.getJSONObject(i)).equals(id))a.remove(i);}
 static void replace(JSONObject d,String id,JSONObject next)throws JSONException{JSONArray a=all(d);for(int i=0;i<a.length();i++)if(id(a.getJSONObject(i)).equals(id)){a.put(i,next);return;}throw new JSONException("Request missing");}
 static int count(JSONObject d,String thread)throws JSONException{JSONArray a=all(d);int n=0;for(int i=0;i<a.length();i++)if(thread.equals(a.getJSONObject(i).optString("thread_id")))n++;return n;}
 static void complete(JSONObject d,JSONObject pending,JSONObject reply)throws JSONException{
  String id=id(pending);
  if(pending.optString("action").equals("models")){d.put("models",Models.unique(reply.getJSONArray("models")));remove(d,id);return;}
  JSONObject target=Conversations.find(d,pending.getString("thread_id"));JSONArray messages=target.getJSONArray("messages"),images=target.getJSONArray("images");
  // A delivered marker makes reconciliation safe even if persistence was interrupted.
  for(int i=0;i<messages.length();i++)if(id.equals(messages.getJSONObject(i).optString("request_id"))){remove(d,id);return;}
  for(int i=0;i<images.length();i++)if(id.equals(images.getJSONObject(i).optString("request_id"))){remove(d,id);return;}
  int at=Math.min(pending.optInt("after_message",messages.length()),messages.length());String anchor=pending.optString("anchor_id");
  if(!anchor.isEmpty())for(int i=0;i<messages.length();i++)if(anchor.equals(messages.getJSONObject(i).optString("id"))){at=i+1;break;}
  if(pending.optBoolean("image"))images.put(new JSONObject().put("request_id",id).put("prompt",pending.optString("image_prompt")).put("images",reply.getJSONArray("images")).put("after_message",at).put("expanded",false));
  else{
   JSONObject response=new JSONObject().put("role","assistant").put("content",reply.getString("text")).put("model",reply.optString("model",pending.optString("selected_model"))).put("request_id",id);
   for(int i=messages.length();i>at;i--)messages.put(i,messages.get(i-1));messages.put(at,response);
   for(int i=0;i<images.length();i++){JSONObject image=images.getJSONObject(i);if(image.optInt("after_message")>=at)image.put("after_message",image.optInt("after_message")+1);}
  }
  remove(d,id);
 }
}
