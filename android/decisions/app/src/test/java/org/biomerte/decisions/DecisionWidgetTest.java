package org.biomerte.decisions;
import android.app.Application;
import android.view.*;
import android.widget.*;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.Config;
import java.util.*;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class) @Config(sdk=35)
public class DecisionWidgetTest {
 private final Application app=RuntimeEnvironment.getApplication();
 private JSONObject row(int count)throws Exception{JSONArray choices=new JSONArray();for(int n=0;n<count;n++)choices.put(new JSONObject().put("id","c"+n).put("label","Option "+n).put("meaning","m"+n));return new JSONObject().put("id","question").put("digest","digest").put("request",new JSONObject().put("title","Concrete action").put("question","Allow changes to the test disk?").put("choices",choices));}
 @Test public void everyOptionUsesDirectButtonInOneContinuousList()throws Exception{List<RemoteViews> rows=DecisionWidget.rows(app,row(17),false,false);assertEquals(18,rows.size());for(int n=1;n<rows.size();n++){View v=rows.get(n).apply(app,new FrameLayout(app));assertEquals("Option "+(n-1),((Button)v.findViewById(R.id.widget_choice)).getText().toString());}}
 @Test public void lockedPhoneHidesQuestionsAndAllResponseActions()throws Exception{List<RemoteViews> rows=DecisionWidget.rows(app,row(4),true,false);assertEquals(1,rows.size());View view=rows.get(0).apply(app,new FrameLayout(app));assertEquals("Unlock to review",((TextView)view.findViewById(R.id.question)).getText().toString());assertFalse(((TextView)view.findViewById(R.id.question)).getText().toString().contains("disk"));}
 @Test public void queuedReplyDoesNotClaimTheQuestionIsAnswered()throws Exception{JSONObject r=row(4),s=new JSONObject().put("rows",new JSONArray().put(r)).put("outbox",new JSONArray().put(new JSONObject().put("decision_id","question").put("state","queued")));assertEquals(1,State.pending(s).length());assertEquals(1,DecisionWidget.rows(app,r,false,true).size());s.getJSONArray("outbox").getJSONObject(0).put("state","accepted");assertEquals(0,State.pending(s).length());}
 @Test public void freeTextIsPreservedAndUnknownChoicesCannotBeQueued()throws Exception{JSONObject r=row(2);String original="Only after backing up · café 🗂";DecisionModel.validate(r,"c0",original);DecisionModel.validate(r,"",original);try{DecisionModel.validate(r,"missing",original);fail();}catch(Exception expected){}try{DecisionModel.validate(r,"","");fail();}catch(Exception expected){}}
 @Test public void emptyStateIsLargeCheckmarkAndNoDecisionsOnly()throws Exception{View v=new RemoteViews(app.getPackageName(),R.layout.widget_empty).apply(app,new FrameLayout(app));assertNotNull(v.findViewById(R.id.empty_check));assertEquals("No decisions",((TextView)v.findViewById(R.id.empty_label)).getText().toString());assertEquals(2,((ViewGroup)v).getChildCount());}
 @Test public void widgetTapOpensTheNativeInboxAndHandlesAnUnknownQuestion()throws Exception{
  DecisionWidget.open(app,"withdrawn-question").send();
  android.content.Intent launched=org.robolectric.Shadows.shadowOf(app).getNextStartedActivity();
  assertNotNull(launched);assertEquals(MainActivity.class.getName(),launched.getComponent().getClassName());
  try(org.robolectric.android.controller.ActivityController<MainActivity> controller=Robolectric.buildActivity(MainActivity.class,launched).setup()){
   App.IO.submit(()->{}).get();org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle();
   assertFalse(controller.get().isFinishing());assertNotNull(controller.get().findViewById(android.R.id.content));
  }
 }

 private boolean containsText(View v,String text){if(v instanceof TextView&&((TextView)v).getText().toString().contains(text))return true;if(v instanceof ViewGroup)for(int n=0;n<((ViewGroup)v).getChildCount();n++)if(containsText(((ViewGroup)v).getChildAt(n),text))return true;return false;}
 private boolean hasSpinner(View v){if(v instanceof ProgressBar)return true;if(v instanceof ViewGroup)for(int n=0;n<((ViewGroup)v).getChildCount();n++)if(hasSpinner(((ViewGroup)v).getChildAt(n)))return true;return false;}
 @Test public void replyShowsImmediateProgressWhileExecutorIsBusyAndRepeatedTapQueuesOnce()throws Exception{
  JSONObject r=row(2);r.getJSONObject("request").put("proposal_revision",1);
  JSONObject s=new JSONObject().put("rows",new JSONArray().put(r)).put("outbox",new JSONArray()).put("config",new JSONObject().put("site","test").put("device","test"));
  new State(app).save(s);
  java.util.concurrent.CountDownLatch entered=new java.util.concurrent.CountDownLatch(1),release=new java.util.concurrent.CountDownLatch(1);
  App.IO.execute(()->{entered.countDown();try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});entered.await();
  try(org.robolectric.android.controller.ActivityController<MainActivity> c=Robolectric.buildActivity(MainActivity.class).create()){
   MainActivity a=c.get();java.lang.reflect.Method render=MainActivity.class.getDeclaredMethod("renderDecision",JSONObject.class,JSONObject.class);render.setAccessible(true);render.invoke(a,s,r);
   java.lang.reflect.Method answer=MainActivity.class.getDeclaredMethod("answer",String.class);answer.setAccessible(true);answer.invoke(a,"c0");answer.invoke(a,"c0");
   assertTrue(hasSpinner(a.findViewById(android.R.id.content)));assertTrue(containsText(a.findViewById(android.R.id.content),"Saving and sending"));
   release.countDown();App.IO.submit(()->{}).get();org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle();
   assertEquals(1,new State(app).read().getJSONArray("outbox").length());
  }finally{release.countDown();}
 }
 @Test public void deliveryDistinguishesQueuedAndAcceptedWithoutLeavingTheReply()throws Exception{
  JSONObject item=new JSONObject().put("decision_id","question").put("state","queued"),s=new JSONObject().put("outbox",new JSONArray().put(item));
  try(org.robolectric.android.controller.ActivityController<MainActivity> c=Robolectric.buildActivity(MainActivity.class).create()){
   MainActivity a=c.get();java.lang.reflect.Method render=MainActivity.class.getDeclaredMethod("renderDelivery",JSONObject.class,String.class);render.setAccessible(true);render.invoke(a,s,"question");
   assertTrue(hasSpinner(a.findViewById(android.R.id.content)));assertFalse(containsText(a.findViewById(android.R.id.content),"Received by Biome"));
   item.put("state","accepted");render.invoke(a,s,"question");assertFalse(hasSpinner(a.findViewById(android.R.id.content)));assertTrue(containsText(a.findViewById(android.R.id.content),"Received by Biome"));
   item.put("state","needs_review");render.invoke(a,s,"question");assertFalse(containsText(a.findViewById(android.R.id.content),"Received by Biome"));assertTrue(containsText(a.findViewById(android.R.id.content),"Question changed"));
  }
 }
}
