package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import android.os.Looper;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.Supplier;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class ParallelRequestsTest {
 static final String ONE="10000000-0000-4000-8000-000000000001",TWO="10000000-0000-4000-8000-000000000002";
 JSONObject request(String id,String thread)throws Exception{return new JSONObject().put("request_id",id).put("thread_id",thread).put("stage","message").put("after_message",0).put("response_key",Vault.b64(new byte[32])).put("transcript_hash",Vault.b64(new byte[32])).put("envelope",new JSONObject().put("kind","request").put("id",id).put("sid",id));}
 @Test public void migrationPreservesAllIdentitiesAndKeysOnce()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray());Conversations.ensure(d);String t=d.getString("active_thread");JSONObject a=request("a",t),b=request("b",t);d.put("pending",a).put("suspended_requests",new JSONArray().put(b).put(a));Requests.migrate(d);Requests.migrate(d);
  assertEquals(2,Requests.all(d).length());assertFalse(d.has("pending"));assertFalse(d.has("suspended_requests"));assertEquals(a.getString("response_key"),Requests.all(d).getJSONObject(0).getString("response_key"));
  JSONObject restored=new JSONObject(Conversations.snapshot(d).toString());Requests.migrate(restored);assertEquals("b",Requests.id(Requests.all(restored).getJSONObject(1)));
 }
 @Test public void outOfOrderRepliesGoToOriginalThreadsOnceAndNextToPrompt()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray());Conversations.ensure(d);String first=d.getString("active_thread");JSONArray m=d.getJSONArray("messages");m.put(new JSONObject().put("role","user").put("content","first").put("id","anchor1")).put(new JSONObject().put("role","user").put("content","second").put("id","anchor2"));
  JSONObject a=request("a",first).put("anchor_id","anchor1"),b=request("b",first).put("anchor_id","anchor2");Requests.add(d,a);Requests.add(d,b);Conversations.create(d);
  Requests.complete(d,b,new JSONObject().put("text","reply2"));Requests.complete(d,a,new JSONObject().put("text","reply1"));Requests.complete(d,a,new JSONObject().put("text","duplicate"));
  assertEquals(0,d.getJSONArray("messages").length());assertEquals(4,m.length());assertEquals("reply1",m.getJSONObject(1).getString("content"));assertEquals("reply2",m.getJSONObject(3).getString("content"));assertEquals(0,Requests.all(d).length());
 }
 @Test public void delayedImageUsesSavedAnchorAfterEarlierReplyInsertion()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray());Conversations.ensure(d);String thread=d.getString("active_thread");JSONArray m=d.getJSONArray("messages");
  m.put(new JSONObject().put("id","first").put("role","user").put("content","first")).put(new JSONObject().put("id","later").put("role","user").put("content","later"));
  JSONObject chat=request("chat",thread).put("anchor_id","first"),image=request("image",thread).put("image",true).put("anchor_id","later").put("after_message",2);
  Requests.add(d,chat);Requests.add(d,image);Requests.complete(d,chat,new JSONObject().put("text","earlier reply"));Requests.complete(d,image,new JSONObject().put("images",new JSONArray()));
  assertEquals(3,d.getJSONArray("images").getJSONObject(0).getInt("after_message"));assertFalse(d.getJSONArray("images").getJSONObject(0).getBoolean("expanded"));
 }
 @Test public void modelDuplicatesMergeLoadedFlagWithoutMergingDifferentQuantizations()throws Exception{
  JSONArray m=Models.unique(new JSONArray().put(new JSONObject().put("id","llama@q4").put("name","Llama")).put(new JSONObject().put("id","llama@q4").put("name","Llama").put("loaded",true)).put(new JSONObject().put("id","llama@q8").put("name","Llama")));
  assertEquals(2,m.length());assertTrue(m.getJSONObject(0).getBoolean("loaded"));assertEquals("llama@q8",m.getJSONObject(1).getString("id"));
 }
 @Test public void foregroundStartsAllWatchersWithoutManualSelectionAndRoutesResults()throws Exception{recover(false);}
 @Test public void failedRequestDoesNotBlockOtherThreadsOrRestartItself()throws Exception{recover(true);}
 void recover(boolean failOne)throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();JSONObject d=new JSONObject().put("messages",new JSONArray());Conversations.ensure(d);String first=d.getString("active_thread");JSONObject p=request(ONE,first);Requests.add(d,p);Conversations.create(d);String second=d.getString("active_thread");Requests.add(d,request(TWO,second));
  CountDownLatch started=new CountDownLatch(2),release=new CountDownLatch(1);Set<String> seen=ConcurrentHashMap.newKeySet();
  PendingRecoveryTest.set(a,"data",d);PendingRecoveryTest.set(a,"vaultKey",new byte[32]);PendingRecoveryTest.set(a,"salt",new byte[32]);PendingRecoveryTest.call(a,"chooseConversation");
  d.put("oauth",new JSONObject().put("access_token","test").put("expires_at",Long.MAX_VALUE)).put("pairing",new JSONObject().put("resource","privatechat.mac.test"));
  PendingRecoveryTest.call(a,"save");JSONObject header=new Vault(a).header();
  assertFalse(header.toString().contains("privatechat.mac.test"));
  JSONObject restored=new JSONObject(new String(Crypto.crypt(false,new byte[32],Vault.unb64(header.getString("nonce")),Crypto.aad("vault","local"),Vault.unb64(header.getString("ct"))),java.nio.charset.StandardCharsets.UTF_8));
  PendingRecoveryTest.call(a,"lock");a=Robolectric.buildActivity(MainActivity.class).setup().get();d=restored;Conversations.ensure(d);PendingRecoveryTest.set(a,"data",d);PendingRecoveryTest.set(a,"vaultKey",new byte[32]);PendingRecoveryTest.set(a,"salt",new byte[32]);PendingRecoveryTest.call(a,"chooseConversation");
  PendingRecoveryTest.set(a,"clientFactory",(Supplier<Biome>)()->new Biome(){@Override public JSONObject exchange(String token,String resource,JSONObject outbound,boolean image,java.util.function.BooleanSupplier active,java.util.function.Consumer<JSONObject> progress)throws Exception{
   String id=outbound.getString("id");assertTrue(seen.add(id));started.countDown();assertTrue(release.await(5,TimeUnit.SECONDS));if(failOne&&id.equals(ONE))throw new Biome.RequestFailed();byte[] nonce=new byte[12];JSONObject body=new JSONObject().put("text","answer-"+id);
   return new JSONObject().put("v",2).put("kind","response").put("id",id).put("sid",id).put("nonce",Vault.b64(nonce)).put("ct",Vault.b64(Crypto.crypt(true,new byte[32],nonce,new WireCrypto(false).sessionAAD("response",id,id,new byte[32]),body.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8))));
  }});
  try{a.onResume();PendingRecoveryTest.call(a,"resume");assertTrue("Both watchers start while neither has finished",started.await(5,TimeUnit.SECONDS));assertEquals(2,seen.size());assertEquals(second,d.getString("active_thread"));release.countDown();
   for(int i=0;i<100&&Requests.all(d).length()>(failOne?1:0);i++){Thread.sleep(20);Shadows.shadowOf(Looper.getMainLooper()).idle();}
   assertEquals(failOne?1:0,Requests.all(d).length());if(failOne){assertTrue(Requests.all(d).getJSONObject(0).getBoolean("terminal_failed"));PendingRecoveryTest.call(a,"resume");assertEquals(2,seen.size());}else assertEquals("answer-"+ONE,Conversations.find(d,first).getJSONArray("messages").getJSONObject(0).getString("content"));assertEquals("answer-"+TWO,Conversations.find(d,second).getJSONArray("messages").getJSONObject(0).getString("content"));
  }finally{release.countDown();PendingRecoveryTest.call(a,"lock");((ExecutorService)PendingRecoveryTest.get(a,"executor")).shutdownNow();}
 }
}
