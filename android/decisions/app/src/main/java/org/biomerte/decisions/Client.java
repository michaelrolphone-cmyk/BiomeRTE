package org.biomerte.decisions;
import android.content.Context;
import org.json.*;
import java.net.*;
import javax.net.ssl.HttpsURLConnection;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

public final class Client {
 public static final class QuestionChanged extends IOException {}
 public static JSONObject envelope(Context c,JSONObject config,String command,JSONObject payload,String id)throws Exception{
  JSONObject e=new JSONObject().put("device",config.getString("device")).put("site",config.getString("site")).put("command",command).put("id",id).put("payload",Vault.b64(payload.toString().getBytes(StandardCharsets.UTF_8)));e.put("signature",new Vault(c).sign(e));return e;
 }
 public static JSONObject post(JSONObject config,JSONObject envelope)throws Exception{
  URL origin=new URL(config.getString("origin"));if(!origin.getProtocol().equals("https")||origin.getUserInfo()!=null||origin.getQuery()!=null||origin.getRef()!=null||!origin.getPath().matches("/?"))throw new IOException("Secure Biome origin required");
  HttpsURLConnection c=(HttpsURLConnection)new URL(origin.toString().replaceAll("/$","")+"/api/decisions").openConnection();
  c.setInstanceFollowRedirects(false);c.setConnectTimeout(20000);c.setReadTimeout(30000);c.setUseCaches(false);c.setRequestMethod("POST");c.setRequestProperty("Content-Type","application/json");c.setDoOutput(true);
  String gate=config.optString("gate_token");if(!gate.isEmpty())c.setRequestProperty("OAI-Sites-Authorization","Bearer "+gate);
  try{try(OutputStream out=c.getOutputStream()){out.write(envelope.toString().getBytes(StandardCharsets.UTF_8));}int status=c.getResponseCode();if(status!=200){try(InputStream in=c.getErrorStream()){if(in!=null){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[4096];int n;while((n=in.read(b))!=-1)out.write(b,0,n);if(new JSONObject(out.toString("UTF-8")).optString("code").equals("decision_changed"))throw new QuestionChanged();}}throw new IOException(status==401?"Phone identity rejected. Open pairing.":"Connection interrupted; your saved reply will retry.");}
   try(InputStream in=c.getInputStream()){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] buf=new byte[8192];int n;while((n=in.read(buf))!=-1)out.write(buf,0,n);return new JSONObject(out.toString("UTF-8"));}
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
 public static synchronized void sync(Context c)throws Exception{
  State state=new State(c);JSONObject snapshot=state.read(),config=snapshot.optJSONObject("config");if(config==null)return;
  JSONArray out=snapshot.getJSONArray("outbox");String deliveryError="";
  for(int n=0;n<out.length();n++){JSONObject item=out.getJSONObject(n);if(!item.getString("state").equals("queued"))continue;
   try{JSONObject receipt=post(config,item.getJSONObject("envelope"));if(!receipt.optBoolean("authenticated")||!receipt.getString("decision_id").equals(item.getString("decision_id")))throw new IOException("Response receipt mismatch");state.receipt(item.getString("id"),receipt);}
   catch(QuestionChanged changed){state.needsReview(item.getString("id"));deliveryError="A question changed. Your original reply is saved for review.";}
   catch(Exception error){deliveryError="Connection interrupted; saved replies will retry.";}
  }
  JSONArray rows=new JSONArray();String cursor="";do{
   JSONObject result=post(config,envelope(c,config,"inbox",new JSONObject().put("cursor",cursor),UUID.randomUUID().toString()));if(!result.getString("site").equals(config.getString("site")))throw new IOException("Inbox site mismatch");JSONArray page=result.getJSONArray("decisions");for(int n=0;n<page.length();n++)rows.put(page.getJSONObject(n));cursor=result.optString("next_cursor","");
  }while(!cursor.isEmpty());state.rows(rows);if(!deliveryError.isEmpty())state.status(deliveryError);
 }
}
