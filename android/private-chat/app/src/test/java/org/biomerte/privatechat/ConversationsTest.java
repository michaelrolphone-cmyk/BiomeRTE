package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class ConversationsTest {
 JSONObject message(String content)throws Exception{return new JSONObject().put("role","user").put("content",content);}
 JSONObject old()throws Exception{return new JSONObject().put("messages",new JSONArray().put(message("original"))).put("draft","draft one").put("selected_model","llama").put("pairing",new JSONObject().put("marker","paired"));}
 @Test public void upgradeKeepsHistoryPairingAndBindsSavedRequests()throws Exception{
  JSONObject d=old();d.put("pending",new JSONObject().put("stage","message").put("response_key","key"));d.put("suspended_requests",new JSONArray().put(new JSONObject().put("image",true)));
  Conversations.ensure(d);String id=d.getString("active_thread");Conversations.ensure(d);
  assertEquals(1,d.getJSONArray("threads").length());assertEquals("key",d.getJSONObject("pending").getString("response_key"));assertEquals(id,d.getJSONObject("pending").getString("thread_id"));assertEquals(id,d.getJSONArray("suspended_requests").getJSONObject(0).getString("thread_id"));assertEquals("paired",d.getJSONObject("pairing").getString("marker"));
 }
 @Test public void threadsKeepSeparateHistoriesDraftsAndModelsAcrossReload()throws Exception{
  JSONObject d=old();Conversations.ensure(d);String first=d.getString("active_thread");JSONObject second=Conversations.create(d);String id=second.getString("id");
  assertEquals(0,d.getJSONArray("messages").length());assertFalse(d.has("draft"));d.getJSONArray("messages").put(message("second"));d.put("draft","draft two").put("selected_model","other");Conversations.select(d,first);
  assertEquals("original",d.getJSONArray("messages").getJSONObject(0).getString("content"));assertEquals("draft one",d.getString("draft"));assertEquals("llama",d.getString("selected_model"));
  JSONObject saved=Conversations.snapshot(d);assertFalse(saved.has("messages"));Conversations.ensure(saved);Conversations.select(saved,id);assertEquals("second",saved.getJSONArray("messages").getJSONObject(0).getString("content"));assertEquals("draft two",saved.getString("draft"));assertEquals("other",saved.getString("selected_model"));
 }
 @Test public void deleteMessageAdjustsImageBoundaryAndIsDurable()throws Exception{
  JSONObject d=old();d.getJSONArray("messages").put(message("later"));d.put("images",new JSONArray().put(new JSONObject().put("after_message",1)).put(new JSONObject().put("after_message",2)));
  Conversations.deleteMessage(d,0);assertEquals(0,d.getJSONArray("images").getJSONObject(0).getInt("after_message"));assertEquals(1,d.getJSONArray("images").getJSONObject(1).getInt("after_message"));
  JSONObject saved=Conversations.snapshot(d);Conversations.ensure(saved);assertEquals("later",saved.getJSONArray("messages").getJSONObject(0).getString("content"));assertFalse(saved.toString().contains("original"));
 }
 @Test public void deleteIndividualImageThenEmptyResult()throws Exception{
  JSONObject d=old();Conversations.ensure(d);JSONObject entry=new JSONObject().put("prompt","example").put("images",new JSONArray().put("one").put("two"));d.getJSONArray("images").put(entry);
  Conversations.deleteImage(d,entry,0);assertEquals("two",entry.getJSONArray("images").getString(0));Conversations.deleteImage(d,entry,0);assertEquals(0,d.getJSONArray("images").length());
 }
 @Test public void completionBelongsToOriginalThreadAfterSwitch()throws Exception{
  JSONObject d=old();Conversations.ensure(d);String first=d.getString("active_thread");JSONObject pending=new JSONObject().put("thread_id",first).put("image",true);d.put("pending",pending);Conversations.create(d);
  Conversations.find(d,pending.getString("thread_id")).getJSONArray("messages").put(message("reply"));assertEquals(0,d.getJSONArray("messages").length());Conversations.select(d,first);assertEquals(2,d.getJSONArray("messages").length());
 }
 @Test public void inferenceContextOmitsPresentationMetadata()throws Exception{
  JSONArray history=new JSONArray().put(new JSONObject().put("role","assistant").put("content","reply").put("model","llama"));JSONArray clean=Conversations.context(history);assertFalse(clean.getJSONObject(0).has("model"));assertTrue(history.getJSONObject(0).has("model"));
 }
 @Test public void terminalFailureRetainsRequestKeysAndPromptWithoutBlockingNewSend()throws Exception{
  JSONObject d=old();JSONObject pending=new JSONObject().put("image_prompt","saved prompt").put("response_key","saved key");d.put("pending",pending);Conversations.retainFailure(d);assertFalse(d.has("pending"));JSONObject saved=d.getJSONArray("suspended_requests").getJSONObject(0);assertTrue(saved.getBoolean("terminal_failed"));assertEquals("saved prompt",saved.getString("image_prompt"));assertEquals("saved key",saved.getString("response_key"));
 }
}
