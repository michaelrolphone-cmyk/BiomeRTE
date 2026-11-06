package org.biomerte.decisions;

import android.app.Application;
import android.view.*;
import android.widget.*;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.Config;
import org.robolectric.android.controller.ActivityController;
import java.io.IOException;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class) @Config(sdk=35)
public class DecisionResilienceTest {
 private final Application app=RuntimeEnvironment.getApplication();

 private JSONObject row(String id)throws Exception {
  return new JSONObject().put("id",id).put("digest","exact-digest-"+id).put("state","pending")
   .put("request",new JSONObject().put("id","request-"+id).put("workflow_instance_id","workflow-"+id)
    .put("proposal_revision",1).put("title","Maintenance review").put("question","Approve this exact maintenance command?")
    .put("choices",new JSONArray()
     .put(new JSONObject().put("id","approve").put("label","Approve").put("meaning","approve"))
     .put(new JSONObject().put("id","reject").put("label","Reject").put("meaning","reject")))
    .put("supporting",new JSONObject().put("host","test-host").put("argv",new JSONArray().put("/usr/bin/true"))));
 }
 private JSONObject proposal()throws Exception {
  return new JSONObject().put("revision",1)
   .put("action",new JSONObject().put("contract","test.action:1").put("resource","test.resource").put("inputs",new JSONObject().put("value","test")))
   .put("targets",new JSONArray().put("test resource")).put("affected_systems",new JSONArray().put("test system"))
   .put("data_operations",new JSONArray()).put("expected_effects",new JSONArray().put("test outcome"))
   .put("consequences",new JSONArray()).put("conditions",new JSONArray()).put("recovery_actions",new JSONArray())
   .put("scope",new JSONObject().put("kind","single"));
 }
 private JSONObject state(JSONArray rows,JSONArray outbox)throws Exception{return new JSONObject().put("rows",rows).put("outbox",outbox);}
 private JSONObject saved(String decisionId)throws Exception {
  return new JSONObject().put("id","original-operation").put("decision_id",decisionId).put("state","queued").put("text","ok")
   .put("envelope",new JSONObject().put("id","original-operation").put("signature","original-signature")
    .put("payload","exact-original-base64-payload").put("command","respond").put("site","original-site").put("device","original-device"));
 }
 private List<String> buttons(View view){
  List<String> text=new ArrayList<>();
  if(view instanceof Button)text.add(((Button)view).getText().toString());
  if(view instanceof ViewGroup)for(int n=0;n<((ViewGroup)view).getChildCount();n++)text.addAll(buttons(((ViewGroup)view).getChildAt(n)));
  return text;
 }
 private void cannotAnswer(JSONObject row)throws Exception{
  try{DecisionModel.validate(row,"approve","");fail("Malformed request was answerable");}catch(Exception expected){}
  assertFalse(DecisionModel.problem(row).isEmpty());
  assertEquals("No response buttons for malformed requests",1,DecisionWidget.rows(app,row,false,false).size());
 }

 @Test public void malformedEntryIsRetainedWithoutHidingAValidBinaryDecision()throws Exception {
  JSONObject valid=row("maintenance:new"),bad=row("bad");
  bad.getJSONObject("request").put("proposal",new JSONObject().put("revision",1));
  JSONArray raw=new JSONArray();Client.appendPage(raw,new JSONArray().put(bad).put("not an object").put(valid));
  JSONObject s=state(raw,new JSONArray());
  assertEquals(3,s.getJSONArray("rows").length());
  assertEquals(2,State.invalidRows(s).length());
  assertEquals("maintenance:new",State.pending(s).getJSONObject(0).getString("id"));
  assertEquals(3,DecisionWidget.rows(app,valid,false,false).size());
  cannotAnswer(bad);
 }

 @Test public void malformedProposalTypesAndRevisionMismatchCannotBeApproved()throws Exception {
  for(String field:new String[]{"action","targets","scope"}){
   JSONObject r=row("bad-"+field);JSONObject p=proposal();p.put(field,"wrong type");
   r.getJSONObject("request").put("proposal",p);cannotAnswer(r);
  }
  JSONObject mismatch=row("mismatch");mismatch.getJSONObject("request").put("proposal",proposal().put("revision",2));cannotAnswer(mismatch);
  JSONObject missing=row("missing");missing.getJSONObject("request").put("authorization_request","permission");cannotAnswer(missing);
 }

 @Test public void validGeneralBinaryMaintenanceAndConcreteAuthorizationRemainAnswerable()throws Exception {
  JSONObject maintenance=row("maintenance:reset");
  DecisionModel.validate(maintenance,"approve","");
  assertTrue(DecisionModel.describe(maintenance.getJSONObject("request")).contains("test-host"));
  JSONObject authorized=row("authorized");
  authorized.getJSONObject("request").put("authorization_request","permission").put("proposal",proposal());
  DecisionModel.validate(authorized,"approve","");
  assertTrue(DecisionModel.describe(authorized.getJSONObject("request")).contains("This action once"));
 }

 @Test public void malformedDecisionStillOffersBackRefreshAndPairingSettings()throws Exception {
  JSONObject bad=row("bad");bad.getJSONObject("request").put("proposal",new JSONObject());
  try(ActivityController<MainActivity> controller=Robolectric.buildActivity(MainActivity.class).create()){
   MainActivity activity=controller.get();activity.renderDecision(state(new JSONArray().put(bad),new JSONArray()),bad);
   List<String> controls=buttons(activity.findViewById(android.R.id.content));
   assertTrue(controls.contains("Back to inbox"));assertTrue(controls.contains("Refresh"));assertTrue(controls.contains("Pairing settings"));
   assertFalse(controls.contains("Approve"));assertFalse(controls.contains("Reject"));
  }
 }

 @Test public void lockedMalformedDecisionDoesNotExposeQuestionOrOptions()throws Exception {
  JSONObject bad=row("secret");bad.getJSONObject("request").put("proposal",new JSONObject());
  List<RemoteViews> views=DecisionWidget.rows(app,bad,true,false);assertEquals(1,views.size());
  View view=views.get(0).apply(app,new FrameLayout(app));
  assertEquals("Unlock to review",((TextView)view.findViewById(R.id.question)).getText().toString());
 }

 @Test public void localNavigationExecutorContinuesWhileNetworkIsBlocked()throws Exception {
  CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);
  Future<?> network=App.NETWORK.submit(()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
  try{
   assertTrue(entered.await(2,TimeUnit.SECONDS));
   Future<Boolean> local=App.IO.submit(()->true);
   assertTrue("Local widget/navigation work must not queue behind HTTP",local.get(2,TimeUnit.SECONDS));
  }finally{release.countDown();network.get(2,TimeUnit.SECONDS);}
 }

 @Test public void forbiddenReplyIsIsolatedAndManualRetryPreservesTheExactEnvelope()throws Exception {
  JSONObject blocked=saved("old"),next=saved("new").put("id","new-operation");
  String original=blocked.getJSONObject("envelope").toString(),id=blocked.getString("id");
  IOException failure=Client.failure(403,"<html>Forbidden</html>");
  assertTrue(failure instanceof Client.HttpFailure);assertFalse(((Client.HttpFailure)failure).retryable());
  State.blockItem(blocked,Client.rejectedReply(failure),403);
  assertFalse(State.due(blocked,Long.MAX_VALUE));assertTrue(State.due(next,System.currentTimeMillis()));
  assertFalse(State.deliveryLabel(blocked).contains("sending"));
  assertTrue(blocked.getString("delivery_error").contains("HTTP 403"));
  assertFalse(blocked.getString("delivery_error").toLowerCase().contains("identity"));
  State.retryItem(blocked);
  assertEquals(id,blocked.getString("id"));assertEquals(original,blocked.getJSONObject("envelope").toString());
  assertTrue(State.due(blocked,System.currentTimeMillis()));
 }

 @Test public void writtenNeedsReviewReceiptIsNotApprovalAndDoesNotBlockTheNextQuestion()throws Exception {
  JSONObject first=row("old"),second=row("fresh"),item=saved("old");
  String original=item.getJSONObject("envelope").toString();
  State.recordReceipt(item,new JSONObject().put("authenticated",true).put("decision_id","old").put("outcome","needs_review").put("choice","").put("text","ok"));
  JSONObject s=state(new JSONArray().put(first).put(second),new JSONArray().put(item));
  assertEquals("needs_review",item.getString("state"));assertFalse(State.deliveryLabel(item).contains("sending"));
  assertEquals(original,item.getJSONObject("envelope").toString());assertEquals("ok",item.getString("text"));
  assertEquals(2,State.pending(s).length());assertFalse(State.submitted(s,"fresh"));
  assertEquals(1,DecisionWidget.rows(app,first,false,true).size());
  assertEquals(3,DecisionWidget.rows(app,second,false,false).size());
  try{State.retryItem(item);fail("Review text must not be changed into another automatic reply");}catch(Exception expected){}
 }

 @Test public void needsReviewScreenDoesNotClaimToBeSending()throws Exception {
  JSONObject r=row("review"),item=saved("review").put("state","needs_review").put("delivery_error","Review required");
  try(ActivityController<MainActivity> controller=Robolectric.buildActivity(MainActivity.class).create()){
   MainActivity activity=controller.get();activity.renderDecision(state(new JSONArray().put(r),new JSONArray().put(item)),r);
   List<String> controls=buttons(activity.findViewById(android.R.id.content));
   assertTrue(controls.contains("Back to inbox"));assertTrue(controls.contains("Refresh"));
   assertFalse(controls.contains("Retry saved reply"));assertFalse(controls.contains("Approve"));
  }
 }

 @Test public void unauthenticatedOrMismatchedReceiptCannotSettleTheSavedReply()throws Exception {
  JSONObject item=saved("exact");String before=item.toString();
  for(JSONObject receipt:new JSONObject[]{
   new JSONObject().put("authenticated",false).put("decision_id","exact"),
   new JSONObject().put("authenticated",true).put("decision_id","other")}){
   try{State.recordReceipt(item,receipt);fail("Invalid receipt accepted");}catch(Exception expected){}
   assertEquals(before,item.toString());
  }
 }

 @Test public void retryClassificationKeepsChangedQuestionsAndTransientFailuresDistinct()throws Exception {
  assertTrue(Client.failure(409,"{\"code\":\"decision_changed\"}") instanceof Client.QuestionChanged);
  assertFalse(((Client.HttpFailure)Client.failure(401,"not JSON")).retryable());
  assertFalse(((Client.HttpFailure)Client.failure(422,"not JSON")).retryable());
  assertTrue(((Client.HttpFailure)Client.failure(429,"not JSON")).retryable());
  assertTrue(((Client.HttpFailure)Client.failure(503,"not JSON")).retryable());
 }

 @Test public void repeatedOrMalformedCursorStopsTheRefreshWithoutAnInfiniteLoop()throws Exception {
  Set<String> seen=new HashSet<>();
  assertEquals("page2",Client.nextCursor(new JSONObject().put("next_cursor","page2"),seen));
  try{Client.nextCursor(new JSONObject().put("next_cursor","page2"),seen);fail();}catch(IOException expected){}
  try{Client.nextCursor(new JSONObject().put("next_cursor",42),seen);fail();}catch(IOException expected){}
  assertEquals("",Client.nextCursor(new JSONObject().put("next_cursor",JSONObject.NULL),seen));
 }
}
