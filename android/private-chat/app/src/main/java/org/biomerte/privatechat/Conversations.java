package org.biomerte.privatechat;
import org.json.*;
import java.util.UUID;

/** All thread content lives inside the password-encrypted vault. */
final class Conversations {
 private static final String[] FIELDS={"messages","images","draft","selected_model"};
 static void ensure(JSONObject data)throws JSONException{
  if(!data.has("threads")){
   JSONObject first=new JSONObject().put("id",UUID.randomUUID().toString()).put("title","Conversation 1");
   for(String key:FIELDS)if(data.has(key))first.put(key,data.get(key));
   if(!first.has("messages"))first.put("messages",new JSONArray());
   if(!first.has("images"))first.put("images",new JSONArray());
   data.put("threads",new JSONArray().put(first)).put("active_thread",first.getString("id"));
  }
  JSONObject active=find(data,data.optString("active_thread"));
  if(!data.has("messages"))bind(data,active);
  if(!active.has("images"))active.put("images",new JSONArray());
  if(!data.has("images"))data.put("images",active.getJSONArray("images"));
  tag(data.optJSONObject("pending"),active.getString("id"));
  JSONArray saved=data.optJSONArray("suspended_requests");
  if(saved!=null)for(int i=0;i<saved.length();i++)tag(saved.optJSONObject(i),active.getString("id"));
 }
 private static void tag(JSONObject pending,String id)throws JSONException{if(pending!=null&&!pending.has("thread_id"))pending.put("thread_id",id);}
 static JSONObject find(JSONObject data,String id)throws JSONException{
  JSONArray threads=data.getJSONArray("threads");
  for(int i=0;i<threads.length();i++){JSONObject t=threads.getJSONObject(i);if(t.getString("id").equals(id))return t;}
  throw new JSONException("Conversation unavailable");
 }
 static JSONObject current(JSONObject data)throws JSONException{return find(data,data.getString("active_thread"));}
 static JSONArray ordered(JSONObject data)throws JSONException{
  ensure(data);JSONArray source=data.getJSONArray("threads"),result=new JSONArray();
  for(boolean pinned:new boolean[]{true,false})for(int i=0;i<source.length();i++){JSONObject thread=source.getJSONObject(i);if(thread.optBoolean("pinned",false)==pinned)result.put(thread);}
  return result;
 }
 static String instructions(JSONObject data,String id)throws JSONException{return find(data,id).optString("instructions","").trim();}
 static JSONArray requestContext(JSONObject data)throws JSONException{
  return withInstructions(context(data.getJSONArray("messages")),instructions(data,data.getString("active_thread")));
 }
 static JSONArray withInstructions(JSONArray messages,String instructions)throws JSONException{
  JSONArray result=new JSONArray();if(!instructions.trim().isEmpty())result.put(new JSONObject().put("role","system").put("content",instructions.trim()));
  for(int i=0;i<messages.length();i++)result.put(messages.getJSONObject(i));return result;
 }
 static JSONArray imageContext(String prompt,String instructions)throws JSONException{return withInstructions(new JSONArray().put(new JSONObject().put("role","user").put("content",prompt)),instructions);}
 static void sync(JSONObject data)throws JSONException{
  ensure(data);JSONObject t=current(data);
  for(String key:FIELDS){if(data.has(key))t.put(key,data.get(key));else t.remove(key);}
 }
 private static void bind(JSONObject data,JSONObject thread)throws JSONException{
  for(String key:FIELDS){if(thread.has(key))data.put(key,thread.get(key));else data.remove(key);}
  if(!data.has("images")){JSONArray a=new JSONArray();data.put("images",a);thread.put("images",a);}
  data.put("active_thread",thread.getString("id"));
 }
 static void select(JSONObject data,String id)throws JSONException{sync(data);bind(data,find(data,id));}
 static JSONObject create(JSONObject data)throws JSONException{
  sync(data);JSONArray threads=data.getJSONArray("threads");
  JSONObject t=new JSONObject().put("id",UUID.randomUUID().toString()).put("title","Conversation "+(threads.length()+1)).put("messages",new JSONArray()).put("images",new JSONArray());
  if(data.has("selected_model"))t.put("selected_model",data.getString("selected_model"));
  threads.put(t);bind(data,t);return t;
 }
 static JSONObject snapshot(JSONObject data)throws JSONException{
  sync(data);JSONObject saved=new JSONObject(data.toString());
  for(String key:FIELDS)saved.remove(key);
  return saved;
 }
 static JSONArray context(JSONArray messages)throws JSONException{
  JSONArray clean=new JSONArray();for(int i=0;i<messages.length();i++){JSONObject m=messages.getJSONObject(i);clean.put(new JSONObject().put("role",m.getString("role")).put("content",m.getString("content")));}return clean;
 }
 static void retainFailure(JSONObject data)throws JSONException{
  JSONObject pending=data.getJSONObject("pending");pending.put("terminal_failed",true);
  JSONArray saved=data.optJSONArray("suspended_requests");if(saved==null){saved=new JSONArray();data.put("suspended_requests",saved);}saved.put(pending);data.remove("pending");
 }
 static void deleteMessage(JSONObject data,int index)throws JSONException{
  ensure(data);JSONArray messages=data.getJSONArray("messages");
  if(index<0||index>=messages.length())throw new JSONException("Message unavailable");
  messages.remove(index);JSONArray images=data.optJSONArray("images");
  if(images!=null)for(int i=0;i<images.length();i++){
   JSONObject image=images.getJSONObject(i);int at=image.optInt("after_message",messages.length()+1);
   if(at>index)image.put("after_message",at-1);
  }
 }
 static void deleteImage(JSONObject data,JSONObject entry,int index)throws JSONException{
  JSONArray entries=data.getJSONArray("images");
  for(int i=0;i<entries.length();i++)if(entries.getJSONObject(i)==entry){
   if(index<0){entries.remove(i);return;}
   JSONArray images=entry.getJSONArray("images");images.remove(index);if(images.length()==0)entries.remove(i);return;
  }
  throw new JSONException("Image unavailable");
 }
}
