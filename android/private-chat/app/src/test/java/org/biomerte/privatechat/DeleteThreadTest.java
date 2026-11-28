package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import android.app.AlertDialog;
import android.widget.*;
import java.lang.reflect.*;
import java.util.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class DeleteThreadTest {
 JSONObject data()throws Exception{return new JSONObject().put("messages",new JSONArray().put(new JSONObject().put("role","user").put("content","deleted secret"))).put("images",new JSONArray().put(new JSONObject().put("prompt","secret image").put("images",new JSONArray()))).put("pairing",new JSONObject().put("marker","keep"));}
 JSONObject request(String id,String thread)throws Exception{return new JSONObject().put("request_id",id).put("thread_id",thread).put("stage","hello").put("request_messages",new JSONArray().put("deleted context"));}
 @Test public void removesActiveThreadCompletelyAndKeepsOtherConversationState()throws Exception{
  JSONObject d=data();Conversations.ensure(d);String first=d.getString("active_thread");Conversations.current(d).put("instructions","secret instructions").put("pinned",true);d.put("draft","secret draft");Requests.add(d,request("remove",first));Conversations.create(d);String keep=d.getString("active_thread");d.put("draft","surviving draft");Requests.add(d,request("keep",keep));Conversations.select(d,first);
  JSONObject next=Conversations.withoutThread(d,first);assertFalse(Conversations.exists(next,first));assertEquals(keep,next.getString("active_thread"));assertEquals("surviving draft",next.getString("draft"));assertEquals("keep",Requests.id(Requests.all(next).getJSONObject(0)));assertFalse(Conversations.snapshot(next).toString().contains("secret"));assertEquals("keep",next.getJSONObject("pairing").getString("marker"));
 }
 @Test public void lastDeletionStaysEmptyAcrossPersistenceAndNewConversationStartsClean()throws Exception{
  JSONObject d=data();Conversations.ensure(d);String first=d.getString("active_thread");JSONObject next=Conversations.withoutThread(d,first);JSONObject restored=new JSONObject(Conversations.snapshot(next).toString());Conversations.ensure(restored);assertEquals(0,restored.getJSONArray("threads").length());assertEquals(0,restored.getJSONArray("messages").length());Conversations.create(restored);assertFalse(first.equals(restored.getString("active_thread")));assertEquals(0,restored.getJSONArray("messages").length());
 }
 @Test public void lateChatAndImageRepliesCannotRecreateDeletedThread()throws Exception{
  JSONObject d=data();Conversations.ensure(d);String thread=d.getString("active_thread");JSONObject chat=request("chat",thread),image=request("image",thread).put("image",true);Requests.add(d,chat);Requests.add(d,image);JSONObject next=Conversations.withoutThread(d,thread);Requests.complete(next,chat,new JSONObject().put("text","late"));Requests.complete(next,image,new JSONObject().put("images",new JSONArray()));assertEquals(0,next.getJSONArray("threads").length());assertEquals(0,Requests.all(next).length());assertFalse(Conversations.snapshot(next).toString().contains("late"));
 }
 @Test public void survivingRequestFailureUpdatesCurrentQueueAfterAtomicDeletion()throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();JSONObject d=data();Conversations.ensure(d);String gone=d.getString("active_thread");Conversations.create(d);String keep=d.getString("active_thread");JSONObject old=request("live",keep);Requests.add(d,old);JSONObject next=Conversations.withoutThread(d,gone);PendingRecoveryTest.set(a,"data",next);PendingRecoveryTest.set(a,"vaultKey",new byte[32]);PendingRecoveryTest.set(a,"salt",new byte[32]);
  Method failure=MainActivity.class.getDeclaredMethod("requestInterrupted",JSONObject.class,int.class,Exception.class);failure.setAccessible(true);failure.invoke(a,old,0,new Biome.RequestFailed());assertTrue(Requests.find(next,"live").getBoolean("terminal_failed"));assertFalse(old.optBoolean("terminal_failed"));
 }
 @Test public void confirmCancelAndDeletePreserveSurvivingDraftAndOnlyCancelOwnedWatchers()throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();JSONObject d=data();Conversations.ensure(d);String deleted=d.getString("active_thread");Conversations.create(d);String keep=d.getString("active_thread");d.put("draft","keep this draft");Requests.add(d,request("gone",deleted));Requests.add(d,request("live",keep));
  PendingRecoveryTest.set(a,"data",d);PendingRecoveryTest.set(a,"vaultKey",new byte[32]);PendingRecoveryTest.set(a,"salt",new byte[32]);PendingRecoveryTest.call(a,"chat");
  Method confirm=MainActivity.class.getDeclaredMethod("confirmDeleteConversation",String.class);confirm.setAccessible(true);confirm.invoke(a,deleted);AlertDialog dialog=(AlertDialog)PendingRecoveryTest.get(a,"deleteThreadDialog");dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick();Shadows.shadowOf(android.os.Looper.getMainLooper()).idle();assertTrue(Conversations.exists((JSONObject)PendingRecoveryTest.get(a,"data"),deleted));
  java.util.Map<String,Biome> clients=(java.util.Map<String,Biome>)PendingRecoveryTest.get(a,"requestClients");List<String> cancelled=new ArrayList<>();clients.put("gone",new Biome(){public void cancel(){cancelled.add("gone");}});clients.put("live",new Biome(){public void cancel(){cancelled.add("live");}});
  confirm.invoke(a,deleted);dialog=(AlertDialog)PendingRecoveryTest.get(a,"deleteThreadDialog");dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick();Shadows.shadowOf(android.os.Looper.getMainLooper()).idle();JSONObject next=(JSONObject)PendingRecoveryTest.get(a,"data");assertFalse(Conversations.exists(next,deleted));assertEquals("keep this draft",next.getString("draft"));assertEquals(Arrays.asList("gone"),cancelled);assertTrue(clients.containsKey("live"));assertTrue((Boolean)PendingRecoveryTest.get(a,"conversationsScreen"));
 }
}
