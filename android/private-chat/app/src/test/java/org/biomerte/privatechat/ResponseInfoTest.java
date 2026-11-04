package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import android.app.AlertDialog;
import android.view.*;
import android.widget.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class ResponseInfoTest {
 JSONObject request()throws Exception{return new JSONObject().put("request_id","operation").put("selected_model","selected-key").put("submitted_at",1000).put("envelope",new JSONObject().put("sid","session")).put("request_messages",new JSONArray().put(new JSONObject().put("role","system").put("content","private thread instructions")).put(new JSONObject().put("role","user").put("content","original question")));}
 JSONObject reply()throws Exception{return new JSONObject().put("text","answer").put("model","loaded-instance").put("diagnostics",new JSONObject().put("v",1).put("requested_model","selected-key").put("resolved_model","loaded-instance").put("response_model","loaded-instance").put("model_verified",true).put("admission_ms",12).put("inference_ms",34).put("usage",new JSONObject().put("prompt_tokens",9).put("total_tokens",12)).put("response_key","must never retain"));}
 @Test public void capturesImmutableContextAndVerifiedInstanceWithoutSecrets()throws Exception{
  JSONObject p=request(),info=ResponseInfo.capture(p,reply());assertTrue(ResponseInfo.verified(info));p.getJSONArray("request_messages").getJSONObject(0).put("content","edited later");
  assertEquals("private thread instructions",info.getJSONArray("context").getJSONObject(0).getString("content"));assertFalse(info.toString().contains("must never retain"));assertFalse(info.toString().contains("response_key"));
  JSONObject restored=new JSONObject(info.toString());assertTrue(ResponseInfo.verified(restored));assertEquals("session",restored.getString("session_id"));
 }
 @Test public void missingAndMismatchedProvenanceNeverPretendToBeVerified()throws Exception{
  JSONObject r=reply();r.remove("model");JSONObject info=ResponseInfo.capture(request(),r);assertFalse(ResponseInfo.verified(info));assertEquals("",info.getString("response_model"));
  info=ResponseInfo.capture(request(),reply());info.getJSONObject("server").put("requested_model","different");assertFalse(ResponseInfo.verified(info));assertTrue(ResponseInfo.summary(new JSONObject().put("info",info)).contains("do not agree"));
  assertTrue(ResponseInfo.summary(new JSONObject().put("model","old label")).contains("Older response"));
 }
 @Test public void deletingMessagesRemovesSnapshotsAndRedactsPendingDiagnostics()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray());Conversations.ensure(d);String thread=d.getString("active_thread");JSONObject p=request().put("thread_id",thread).put("stage","message");Requests.add(d,p);
  d.getJSONArray("messages").put(new JSONObject().put("role","user").put("content","original question")).put(new JSONObject().put("role","assistant").put("content","answer").put("info",ResponseInfo.capture(p,reply())));
  Conversations.deleteMessage(d,0);assertFalse(d.getJSONArray("messages").getJSONObject(0).getJSONObject("info").has("context"));assertTrue(p.getBoolean("context_redacted"));assertFalse(ResponseInfo.capture(p,reply()).has("context"));
 }
 @Test public void diagnosticMetadataNeverEntersFutureInferenceContext()throws Exception{
  JSONArray messages=new JSONArray().put(new JSONObject().put("role","assistant").put("content","answer").put("info",ResponseInfo.capture(request(),reply())));JSONArray context=Conversations.context(messages);assertEquals(2,context.getJSONObject(0).length());assertFalse(context.toString().contains("private thread instructions"));
 }
 @Test public void infoDialogExpandsContextIsSecureAndClearsOnLock()throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();JSONObject message=new JSONObject().put("role","assistant").put("content","answer").put("model","loaded-instance").put("info",ResponseInfo.capture(request(),reply()));
  JSONObject d=new JSONObject().put("messages",new JSONArray().put(message));PendingRecoveryTest.set(a,"data",d);PendingRecoveryTest.set(a,"vaultKey",new byte[32]);PendingRecoveryTest.set(a,"salt",new byte[32]);PendingRecoveryTest.call(a,"chat");
  View button=ChatUiTest.findDescription((ViewGroup)PendingRecoveryTest.get(a,"messageList"),"Response info");assertNotNull(button);button.performClick();AlertDialog dialog=(AlertDialog)PendingRecoveryTest.get(a,"infoDialog");assertTrue(dialog.isShowing());assertTrue((dialog.getWindow().getAttributes().flags&WindowManager.LayoutParams.FLAG_SECURE)!=0);
  TextView context=findText(dialog.getWindow().getDecorView(),"private thread instructions",false);assertNotNull(context);assertEquals(View.GONE,context.getVisibility());TextView expand=findText(dialog.getWindow().getDecorView(),"Show sent context",true);assertNotNull(expand);expand.performClick();assertEquals(View.VISIBLE,context.getVisibility());
  PendingRecoveryTest.call(a,"save");assertFalse(new Vault(a).header().toString().contains("private thread instructions"));PendingRecoveryTest.call(a,"lock");assertFalse(dialog.isShowing());assertNull(PendingRecoveryTest.get(a,"infoDialog"));
 }
 static TextView findText(View view,String match,boolean prefix){if(view instanceof TextView){String text=((TextView)view).getText().toString();if(prefix?text.startsWith(match):text.contains(match))return (TextView)view;}if(view instanceof ViewGroup){ViewGroup g=(ViewGroup)view;for(int i=0;i<g.getChildCount();i++){TextView found=findText(g.getChildAt(i),match,prefix);if(found!=null)return found;}}return null;}
}
