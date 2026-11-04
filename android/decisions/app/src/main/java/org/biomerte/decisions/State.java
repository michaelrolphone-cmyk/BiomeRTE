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
  JSONObject s=read(),current=null;JSONArray rows=s.getJSONArray("rows"),out=s.getJSONArray("outbox");
  for(int n=0;n<rows.length();n++)if(rows.getJSONObject(n).getString("id").equals(displayed.getString("id")))current=rows.getJSONObject(n);
  if(current==null||!current.getString("digest").equals(displayed.getString("digest")))throw new Exception("Question changed. Review the latest version.");
  for(int n=0;n<out.length();n++)if(out.getJSONObject(n).getString("decision_id").equals(current.getString("id")))throw new Exception("A reply is already saved for this question");
  JSONObject payload=new JSONObject().put("decision_id",current.getString("id")).put("digest",current.getString("digest")).put("proposal_revision",current.getJSONObject("request").getInt("proposal_revision")).put("choice",choice).put("text",text);
  DecisionModel.validate(current,choice,text);
  JSONObject envelope=Client.envelope(c,s.getJSONObject("config"),"respond",payload,UUID.randomUUID().toString());
  JSONObject item=new JSONObject().put("id",envelope.getString("id")).put("decision_id",current.getString("id")).put("envelope",envelope).put("state","queued").put("text",text);
  out.put(item);s.put("status","Reply saved · sending");save(s);return item;
 }}
 public void receipt(String id,JSONObject receipt)throws Exception{synchronized(LOCK){JSONObject s=read();JSONArray out=s.getJSONArray("outbox");for(int n=0;n<out.length();n++){JSONObject item=out.getJSONObject(n);if(item.getString("id").equals(id)){item.put("state","accepted").put("receipt",receipt);s.put("status","Response accepted");}}save(s);}}
 public void needsReview(String id)throws Exception{synchronized(LOCK){JSONObject s=read();JSONArray out=s.getJSONArray("outbox");for(int n=0;n<out.length();n++)if(out.getJSONObject(n).getString("id").equals(id))out.getJSONObject(n).put("state","needs_review");save(s);}}
 public void draft(String id,String value)throws Exception{synchronized(LOCK){JSONObject s=read();JSONObject drafts=s.optJSONObject("drafts");if(drafts==null)drafts=new JSONObject();drafts.put(id,value);s.put("drafts",drafts);save(s);}}
 public void next()throws Exception{synchronized(LOCK){JSONObject s=read();s.put("index",s.optInt("index")+1);save(s);}}
 public static boolean submitted(JSONObject s,String id)throws Exception{JSONArray out=s.getJSONArray("outbox");for(int n=0;n<out.length();n++)if(out.getJSONObject(n).getString("decision_id").equals(id))return true;return false;}
 public static JSONArray pending(JSONObject s)throws Exception{JSONArray rows=s.getJSONArray("rows"),ready=new JSONArray();for(int i=0;i<rows.length();i++){JSONObject row=rows.getJSONObject(i);boolean accepted=false;JSONArray out=s.getJSONArray("outbox");for(int n=0;n<out.length();n++){JSONObject item=out.getJSONObject(n);if(item.getString("decision_id").equals(row.getString("id"))&&item.getString("state").equals("accepted"))accepted=true;}if(!accepted)ready.put(row);}return ready;}
}
