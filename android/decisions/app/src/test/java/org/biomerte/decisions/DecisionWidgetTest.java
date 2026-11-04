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
}
