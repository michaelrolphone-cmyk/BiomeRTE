package org.biomerte.privatechat;
import org.json.*;
import java.net.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import javax.net.ssl.HttpsURLConnection;
public final class Biome {
 public static final String ORIGIN="https://biomerte-controller.cantikash.chatgpt.site", RESOURCE=ORIGIN+"/api/mcp";
 public static final class RequestFailed extends IOException {}
 private volatile HttpsURLConnection connection;
 public void cancel(){HttpsURLConnection c=connection;if(c!=null)c.disconnect();}
 public JSONObject request(String path,String text,String type,String token)throws Exception{
  URL url=new URL(ORIGIN+path);HttpsURLConnection c=(HttpsURLConnection)url.openConnection();connection=c;
  c.setInstanceFollowRedirects(false);c.setConnectTimeout(30000);c.setUseCaches(false);
  c.setRequestMethod("POST");c.setRequestProperty("Content-Type",type);c.setRequestProperty("Accept","application/json");
  if(token!=null)c.setRequestProperty("Authorization","Bearer "+token);
  c.setDoOutput(true);
  try{try(OutputStream o=c.getOutputStream()){o.write(text.getBytes(StandardCharsets.UTF_8));}
   int code=c.getResponseCode();if(code<200||code>=300)throw new IOException("Connection rejected");
   try(InputStream in=c.getInputStream()){return new JSONObject(new String(readBytes(in),StandardCharsets.UTF_8));}
  }finally{c.disconnect();if(connection==c)connection=null;}
 }
 private static byte[] readBytes(InputStream in)throws IOException{ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1)out.write(b,0,n);return out.toByteArray();}
 public JSONObject json(String path,JSONObject body,String token)throws Exception{return request(path,body.toString(),"application/json",token);}
 public JSONObject form(String path,JSONObject body)throws Exception{
  StringBuilder s=new StringBuilder();java.util.Iterator<String> keys=body.keys();
  while(keys.hasNext()){String k=keys.next();if(s.length()>0)s.append('&');s.append(URLEncoder.encode(k,"UTF-8")).append('=').append(URLEncoder.encode(body.getString(k),"UTF-8"));}
  return request(path,s.toString(),"application/x-www-form-urlencoded",null);
 }
 public JSONObject invoke(String token,String op,JSONObject args,String rid)throws Exception{
  JSONObject call=new JSONObject().put("jsonrpc","2.0").put("id",rid).put("method","tools/call")
   .put("params",new JSONObject().put("name","biome_invoke").put("arguments",new JSONObject().put("realm","site").put("operation",op).put("arguments",args).put("request_id",rid)));
  JSONObject reply=json("/api/mcp",call,token).getJSONObject("result");
  if(reply.optBoolean("isError"))throw new IOException("Controller reply pending or rejected");
  if(reply.has("structuredContent")&&!reply.isNull("structuredContent"))return reply.getJSONObject("structuredContent");
  return new JSONObject(reply.getJSONArray("content").getJSONObject(0).getString("text"));
 }
 public JSONObject exchange(String token,String resource,JSONObject envelope,java.util.function.BooleanSupplier active)throws Exception{
  String id=envelope.getString("id");JSONObject lease=invoke(token,"cap.acquire",new JSONObject().put("contract","chat.private.exchange:1").put("resource",resource).put("task",id),id+"-acquire");
  String lid=lease.getString("id");boolean settled=false;
  try{
   while(!lease.getString("state").equals("bound")){if(!active.getAsBoolean())throw new InterruptedException();Thread.sleep(1500);lease=invoke(token,"cap.lease",new JSONObject().put("lease",lid),UUID.randomUUID().toString());}
   invoke(token,"cap.invoke",new JSONObject().put("lease",lid).put("operation",id).put("inputs",new JSONObject().put("envelope",envelope)),id+"-invoke");
   while(active.getAsBoolean()){
    JSONObject status=invoke(token,"cap.status",new JSONObject().put("operation",id),UUID.randomUUID().toString());
    String state=status.getString("state");
    if(state.equals("succeeded")){settled=true;return status.getJSONObject("result").getJSONObject("envelope");}
    if(state.equals("failed")){settled=true;throw new RequestFailed();}
    if(state.equals("uncertain"))throw new IOException("Request needs reconciliation");
    Thread.sleep(1500);
   }
   throw new InterruptedException();
  }finally{
   if(settled)try{invoke(token,"cap.release",new JSONObject().put("lease",lid),id+"-release");}catch(Exception ignored){}
  }
 }
}
