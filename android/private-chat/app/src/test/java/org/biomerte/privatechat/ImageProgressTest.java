package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import android.widget.*;
import java.lang.reflect.*;
import java.util.UUID;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class ImageProgressTest {
 @Test public void validProgressUpdatesButStaleAndTamperedFramesDoNot()throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();Field busy=MainActivity.class.getDeclaredField("busy");busy.setAccessible(true);busy.set(a,true);
  PendingRecoveryTest.set(a,"data",new JSONObject());
  String id=UUID.randomUUID().toString(),sid=UUID.randomUUID().toString();byte[] key=Crypto.random(32),hash=Crypto.random(32);
  JSONObject pending=new JSONObject().put("response_key",Vault.b64(key)).put("transcript_hash",Vault.b64(hash)).put("envelope",new JSONObject().put("id",id).put("sid",sid));long[] seq={0};
  Method show=MainActivity.class.getDeclaredMethod("showImageProgress",JSONObject.class,JSONObject.class,int.class,long[].class);show.setAccessible(true);
  show.invoke(a,frame(id,sid,key,hash,2,7),pending,0,seq);Field status=MainActivity.class.getDeclaredField("status");status.setAccessible(true);TextView label=(TextView)status.get(a);assertEquals("Generating image · Step 7 of 28",label.getText().toString());
  show.invoke(a,frame(id,sid,key,hash,1,2),pending,0,seq);assertEquals("Generating image · Step 7 of 28",label.getText().toString());
  JSONObject bad=frame(id,sid,key,hash,3,9);bad.put("ct",Vault.b64(new byte[32]));show.invoke(a,bad,pending,0,seq);assertEquals(2,seq[0]);assertEquals("Generating image · Step 7 of 28",label.getText().toString());
 }
 JSONObject frame(String id,String sid,byte[] key,byte[] hash,int seq,int step)throws Exception{
  byte[] nonce=Crypto.random(12);JSONObject update=new JSONObject().put("stage","sampling").put("step",step).put("total",28).put("seq",seq);
  return new JSONObject().put("v",2).put("kind","progress").put("id",id).put("sid",sid).put("nonce",Vault.b64(nonce)).put("ct",Vault.b64(Crypto.crypt(true,key,nonce,ImageCrypto.sessionAAD("progress",sid,id,hash),update.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8))));
 }
}
