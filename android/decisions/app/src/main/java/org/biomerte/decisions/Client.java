package org.biomerte.decisions;

import android.content.Context;
import org.json.*;
import java.net.*;
import javax.net.ssl.HttpsURLConnection;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public final class Client {
 public static final class QuestionChanged extends IOException {}
 static final class HttpFailure extends IOException {
  final int status;
  HttpFailure(int status){super("Decision service rejected the request (HTTP "+status+"). Saved replies are retained.");this.status=status;}
  boolean retryable(){return status==408||status==429||status>=500;}
 }
 static IOException failure(int status,String body){
  if(status!=401&&status!=403){
   try{if(new JSONObject(body).optString("code").equals("decision_changed"))return new QuestionChanged();}
   catch(JSONException ignored){}
  }
  return new HttpFailure(status);
 }
 static String rejectedReply(Exception error){
  return error instanceof HttpFailure?"Reply rejected (HTTP "+((HttpFailure)error).status+"). Saved reply retained; automatic retry paused.":"Reply could not be verified. Saved reply retained; automatic retry paused.";
 }
 public static JSONObject envelope(Context c,JSONObject config,String command,JSONObject payload,String id)throws Exception{
  JSONObject e=new JSONObject().put("device",config.getString("device")).put("site",config.getString("site")).put("command",command).put("id",id).put("payload",Vault.b64(payload.toString().getBytes(StandardCharsets.UTF_8)));e.put("signature",new Vault(c).sign(e));return e;
 }
 private static String read(InputStream in)throws IOException {
  if(in==null)return "";
  try(InputStream input=in){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[8192];int n;while((n=input.read(b))!=-1)out.write(b,0,n);return out.toString("UTF-8");}
 }
 public static JSONObject post(JSONObject config,JSONObject envelope)throws Exception{
  URL origin=new URL(config.getString("origin"));if(!origin.getProtocol().equals("https")||origin.getUserInfo()!=null||origin.getQuery()!=null||origin.getRef()!=null||!origin.getPath().matches("/?"))throw new IOException("Secure Biome origin required");
  HttpsURLConnection c=(HttpsURLConnection)new URL(origin.toString().replaceAll("/$","")+"/api/decisions").openConnection();
  c.setInstanceFollowRedirects(false);c.setConnectTimeout(20000);c.setReadTimeout(30000);c.setUseCaches(false);c.setRequestMethod("POST");c.setRequestProperty("Content-Type","application/json");c.setDoOutput(true);
  String gate=config.optString("gate_token");if(!gate.isEmpty())c.setRequestProperty("OAI-Sites-Authorization","Bearer "+gate);
  try{
   try(OutputStream out=c.getOutputStream()){out.write(envelope.toString().getBytes(StandardCharsets.UTF_8));}
   int status=c.getResponseCode();
   if(status!=200)throw failure(status,read(c.getErrorStream()));
   try{return new JSONObject(read(c.getInputStream()));}
   catch(JSONException invalid){throw new IOException("Decision service returned invalid JSON; saved replies are retained.",invalid);}
  }finally{c.disconnect();}
 }
 public static void pair(Context c,JSONObject bundle)throws Exception{
  if(!bundle.optString("format").equals("biome-decisions-pairing/1"))throw new IOException("Choose a Biome Decisions pairing file");
  State state=new State(c);JSONObject current=state.read();JSONObject existing=current.optJSONObject("config");if(existing!=null&&!existing.getString("site").equals(bundle.getString("site")))throw new IOException("This app is already paired to another Biome");
  Vault v=new Vault(c);String installation=v.get("installation");if(installation==null){installation=UUID.randomUUID().toString();v.put("installation",installation);}
  JSONObject e=new JSONObject().put("command","enroll").put("pair_id",bundle.getString("pair_id")).put("code",bundle.getString("code")).put("public_key",v.publicKey()).put("installation",installation);
  JSONObject result=post(bundle,e);if(!result.getString("site").equals(bundle.getString("site")))throw new IOException("Pairing site mismatch");
  JSONObject config=new JSONObject().put("origin",bundle.getString("origin")).put("site",result.getString("site")).put("device",result.getString("device"));if(bundle.has("gate_token"))config.put("gate_token",bundle.getString("gate_token"));state.configure(config);
 }
 static void appendPage(JSONArray rows,JSONArray page)throws JSONException{
  // Retain malformed entries as evidence. They are isolated by State.pending.
  for(int n=0;n<page.length();n++)rows.put(page.get(n));
 }
 static String nextCursor(JSONObject result,Set<String> seen)throws IOException {
  Object value=result.opt("next_cursor");
  if(value==null||value==JSONObject.NULL)return "";
  if(!(value instanceof String))throw new IOException("Invalid inbox cursor; previous inbox retained");
  String cursor=(String)value;
  if(!cursor.isEmpty()&&!seen.add(cursor))throw new IOException("Inbox cursor repeated; previous inbox retained");
  return cursor;
 }
 public static synchronized void sync(Context c)throws Exception{
  State state=new State(c);JSONObject snapshot=state.read(),config=snapshot.optJSONObject("config");if(config==null)return;
  String inboxError="";
  try {
   JSONArray rows=new JSONArray();String cursor="";Set<String> seen=new HashSet<>();
   do{
    JSONObject result=post(config,envelope(c,config,"inbox",new JSONObject().put("cursor",cursor),UUID.randomUUID().toString()));
    if(!result.getString("site").equals(config.getString("site")))throw new IOException("Inbox site mismatch");
    appendPage(rows,result.getJSONArray("decisions"));cursor=nextCursor(result,seen);
   }while(!cursor.isEmpty());
   state.rows(rows);
  }catch(Exception error){inboxError=error.getMessage()==null?"Inbox connection interrupted; existing decisions retained.":error.getMessage();}

  // Inbox refresh and local navigation do not depend on a successful old reply.
  // New replies get an attempt before earlier transient failures.
  JSONArray out=state.read().getJSONArray("outbox");
  List<JSONObject> due=new ArrayList<>();long now=System.currentTimeMillis();
  for(int n=0;n<out.length();n++){JSONObject item=out.optJSONObject(n);if(State.due(item,now))due.add(item);}
  due.sort(Comparator.comparingInt(item->item.optInt("attempts")));
  for(JSONObject item:due){
   String id=item.optString("id");
   try{
    JSONObject receipt=post(config,item.getJSONObject("envelope"));
    if(!receipt.optBoolean("authenticated")||!receipt.optString("decision_id").equals(item.getString("decision_id"))){
     state.blocked(id,rejectedReply(new IOException("Response receipt mismatch")),200);continue;
    }
    state.receipt(id,receipt);
   }catch(QuestionChanged changed){state.needsReview(id);}
   catch(HttpFailure failure){
    if(!failure.retryable()){state.blocked(id,rejectedReply(failure),failure.status);continue;}
    state.retryLater(id,"Delivery interrupted (HTTP "+failure.status+"); saved reply will retry.");
    break;
   }catch(JSONException invalid){state.blocked(id,"Saved reply is malformed; retained for review without automatic retry.",0);}
   catch(Exception error){
    state.retryLater(id,"Connection interrupted; saved reply will retry.");
    break;
   }
  }
  state.status(inboxError);
 }
}
