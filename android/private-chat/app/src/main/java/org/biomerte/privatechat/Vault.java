package org.biomerte.privatechat;
import android.content.Context;
import android.util.AtomicFile;
import android.util.Base64;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
public final class Vault {
 private final AtomicFile file;
 public Vault(Context ctx){file=new AtomicFile(new File(ctx.getNoBackupFilesDir(),"conversation.vault"));}
 public boolean exists(){return file.getBaseFile().exists();}
 public static String b64(byte[] b){return Base64.encodeToString(b,Base64.NO_WRAP);}
 public static byte[] unb64(String s){return Base64.decode(s,Base64.NO_WRAP);}
 public JSONObject header()throws Exception{return new JSONObject(new String(file.readFully(),StandardCharsets.UTF_8));}
 public JSONObject unlock(char[] password,byte[][] output)throws Exception{
  JSONObject h=header();if(h.getInt("v")!=1||h.getInt("iterations")!=Crypto.ITERATIONS)throw new IOException();
  byte[] salt=unb64(h.getString("salt"));if(salt.length!=32)throw new IOException();
  byte[] key=Crypto.derive(password,salt);
  try{JSONObject v=new JSONObject(new String(Crypto.crypt(false,key,unb64(h.getString("nonce")),Crypto.aad("vault","local"),unb64(h.getString("ct"))),StandardCharsets.UTF_8));output[0]=key;output[1]=salt;return v;}
  catch(Exception e){Crypto.wipe(key);throw e;}
 }
 public void save(JSONObject value,byte[] key,byte[] salt)throws Exception{
  byte[] n=Crypto.random(12),plain=value.toString().getBytes(StandardCharsets.UTF_8);
  JSONObject h=new JSONObject().put("v",1).put("iterations",Crypto.ITERATIONS).put("salt",b64(salt)).put("nonce",b64(n));
  try{h.put("ct",b64(Crypto.crypt(true,key,n,Crypto.aad("vault","local"),plain)));}
  finally{Crypto.wipe(plain);}
  FileOutputStream stream=null;
  try{stream=file.startWrite();stream.write(h.toString().getBytes(StandardCharsets.UTF_8));file.finishWrite(stream);}
  catch(Exception e){if(stream!=null)file.failWrite(stream);throw e;}
 }
}
