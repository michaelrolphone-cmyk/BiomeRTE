package org.biomerte.decisions;

import android.content.Context;
import org.json.*;
import java.util.UUID;

public final class State {
 private static final Object LOCK=new Object();
 private final Vault vault;
 public State(Context c){vault=new Vault(c);}
 public JSONObject read()throws Exception{synchronized(LOCK){String raw=vault.get("state");return raw==null?new JSONObject().put("rows",new JSONArray()).put("outbox",new JSONArray()):new JSONObject(raw);}}
 public void save(JSONObject state)throws Exception{synchronized(LOCK){vault.put("state",state.toString());}}
 public void status(String message)throws Exception{synchronized(LOCK){JSONObject s=read();s.put("status",message);save(s);}}
 public void rows(JSONArray rows)throws Exception{synchronized(LOCK){JSONObject s=read();s.put("rows",rows).put("last_sync",System.currentTimeMillis()).put("status","");save(s);}}
 public void configure(JSONObject config)throws Exception{synchronized(LOCK){JSONObject s=read();if(s.has("config")&&!s.getJSONObject("config").getString("site").equals(config.getString("site")))throw new Exception("This installation is paired to another Biome. Use a separate Android profile to keep its pending replies.");s.put("config",config);save(s);}}

 public JSONObject queue(Context c,JSONObject displayed,String choice,String text)throws Exception{synchronized(LOCK){
  if(c.getSystemService(android.app.KeyguardManager.class).isDeviceLocked())throw new Exception("Unlock the phone to respond");
  DecisionModel.requireValid(displayed);
  JSONObject s=read(),current=null;
  JSONArray rows=s.getJSONArray("rows"),out=s.getJSONArray("outbox");
  for(int n=0;n<rows.length();n++){
   JSONObject row=rows.optJSONObject(n);
   if(row!=null&&row.optString("id").equals(displayed.getString("id")))current=row;
  }
  if(current==null||!current.getString("digest").equals(displayed.getString("digest")))throw new Exception("Question changed. Review the latest version.");
  if(delivery(s,current.getString("id"))!=null)throw new Exception("A reply is already saved for this question");
  DecisionModel.validate(current,choice,text);
  JSONObject payload=new JSONObject().put("decision_id",current.getString("id")).put("digest",current.getString("digest")).put("proposal_revision",current.getJSONObject("request").getInt("proposal_revision")).put("choice",choice).put("text",text);
  JSONObject envelope=Client.envelope(c,s.getJSONObject("config"),"respond",payload,UUID.randomUUID().toString());
  JSONObject item=new JSONObject().put("id",envelope.getString("id")).put("decision_id",current.getString("id")).put("envelope",envelope).put("state","queued").put("text",text);
  out.put(item);save(s);return item;
 }}

 public void receipt(String id,JSONObject receipt)throws Exception{synchronized(LOCK){
  JSONObject s=read();JSONObject item=find(s,id);
  if(item!=null)recordReceipt(item,receipt);
  save(s);
 }}
 static void recordReceipt(JSONObject item,JSONObject receipt)throws Exception {
  if(!receipt.optBoolean("authenticated")||!receipt.optString("decision_id").equals(item.getString("decision_id")))
   throw new Exception("Response receipt mismatch");
  boolean review="needs_review".equals(receipt.optString("outcome"));
  item.put("state",review?"needs_review":"accepted").put("receipt",receipt);
  if(review)item.put("delivery_error","Your written reply was recorded for review. It did not approve the request.");
  else item.remove("delivery_error");
  item.remove("next_attempt_at");
 }
 public void needsReview(String id)throws Exception{synchronized(LOCK){
  JSONObject s=read();JSONObject item=find(s,id);
  if(item!=null)item.put("state","needs_review").put("delivery_error","Question changed or was withdrawn. Your original reply is retained for review.");
  save(s);
 }}
 public void blocked(String id,String message,int status)throws Exception{synchronized(LOCK){
  JSONObject s=read();JSONObject item=find(s,id);
  if(item!=null)blockItem(item,message,status);
  save(s);
 }}
 static void blockItem(JSONObject item,String message,int status)throws Exception {
  item.put("state","blocked").put("delivery_error",message).put("http_status",status);
  item.remove("next_attempt_at");
 }
 public void retryLater(String id,String message)throws Exception{synchronized(LOCK){
  JSONObject s=read();JSONObject item=find(s,id);
  if(item!=null){
   int attempts=item.optInt("attempts")+1;
   long delay=Math.min(300000L,10000L*(1L<<Math.min(attempts-1,5)));
   item.put("state","queued").put("attempts",attempts).put("delivery_error",message).put("next_attempt_at",System.currentTimeMillis()+delay);
  }
  save(s);
 }}
 public void retrySaved(String id)throws Exception{synchronized(LOCK){
  JSONObject s=read();JSONObject item=find(s,id);
  if(item==null)throw new Exception("Saved reply is no longer available");
  retryItem(item);save(s);
 }}
 static void retryItem(JSONObject item)throws Exception {
  if(!"blocked".equals(item.optString("state")))throw new Exception("Only a blocked saved reply can be retried");
  // Never recreate, re-sign, or alter the original envelope or operation ID.
  item.put("state","queued").put("next_attempt_at",0);
  item.remove("delivery_error");item.remove("http_status");
 }
 static boolean due(JSONObject item,long now){
  return item!=null&&"queued".equals(item.optString("state"))&&item.optLong("next_attempt_at")<=now;
 }
 private static JSONObject find(JSONObject state,String id)throws Exception {
  JSONArray out=state.getJSONArray("outbox");
  for(int n=0;n<out.length();n++){JSONObject item=out.optJSONObject(n);if(item!=null&&id.equals(item.optString("id")))return item;}
  return null;
 }
 public void draft(String id,String value)throws Exception{synchronized(LOCK){JSONObject s=read();JSONObject drafts=s.optJSONObject("drafts");if(drafts==null)drafts=new JSONObject();drafts.put(id,value);s.put("drafts",drafts);save(s);}}
 public void next()throws Exception{synchronized(LOCK){JSONObject s=read();s.put("index",s.optInt("index")+1);save(s);}}
 public static JSONObject delivery(JSONObject s,String id)throws Exception {
  JSONArray out=s.getJSONArray("outbox");
  for(int n=0;n<out.length();n++){JSONObject item=out.optJSONObject(n);if(item!=null&&id.equals(item.optString("decision_id")))return item;}
  return null;
 }
 public static boolean submitted(JSONObject s,String id)throws Exception{return delivery(s,id)!=null;}
 public static String deliveryLabel(JSONObject item){
  if(item==null)return "";
  switch(item.optString("state")){
   case "accepted":return "Response accepted";
   case "blocked":return "Reply retained · needs attention";
   case "needs_review":return "Reply retained · review required";
   default:return item.optString("delivery_error").isEmpty()?"Reply saved · waiting to send":"Reply saved · waiting to retry";
  }
 }
 public static int attentionCount(JSONObject s)throws Exception {
  int count=0;JSONArray out=s.getJSONArray("outbox");
  for(int n=0;n<out.length();n++){JSONObject item=out.optJSONObject(n);if(item!=null&&("blocked".equals(item.optString("state"))||"needs_review".equals(item.optString("state"))))count++;}
  return count;
 }
 public static JSONArray pending(JSONObject s)throws Exception {
  JSONArray rows=s.getJSONArray("rows"),ready=new JSONArray();
  for(int n=0;n<rows.length();n++){
   Object value=rows.get(n);
   if(!DecisionModel.problem(value).isEmpty())continue;
   JSONObject row=(JSONObject)value,item=delivery(s,row.getString("id"));
   if(item==null||!"accepted".equals(item.optString("state")))ready.put(row);
  }
  return ready;
 }
 public static JSONArray invalidRows(JSONObject s)throws Exception {
  JSONArray rows=s.getJSONArray("rows"),invalid=new JSONArray();
  for(int n=0;n<rows.length();n++){
   Object value=rows.get(n);String problem=DecisionModel.problem(value);
   if(!problem.isEmpty())invalid.put(new JSONObject().put("id",value instanceof JSONObject?((JSONObject)value).optString("id","Unknown request"):"Unknown request").put("problem",problem));
  }
  return invalid;
 }
}
