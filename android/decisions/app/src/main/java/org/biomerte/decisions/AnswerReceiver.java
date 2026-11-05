package org.biomerte.decisions;
import android.content.*;
import android.appwidget.AppWidgetManager;
import android.view.View;
import android.widget.RemoteViews;
import org.json.*;
import java.util.*;
public class AnswerReceiver extends BroadcastReceiver {
 private static final Set<String> saving=java.util.concurrent.ConcurrentHashMap.newKeySet();
 @Override public void onReceive(Context c,Intent i){
  if(!"biome.decision.choice".equals(i.getAction())||i.getData()==null||DecisionWidget.locked(c))return;
  List<String> parts=i.getData().getPathSegments();if(parts.size()!=3)return;String key=parts.get(1);if(!saving.add(key))return;
  try{int widget=Integer.parseInt(parts.get(0));RemoteViews patch=new RemoteViews(c.getPackageName(),R.layout.widget);patch.setTextViewText(R.id.response_status,"Saving reply…");patch.setViewVisibility(R.id.response_status,View.VISIBLE);patch.setViewVisibility(R.id.sending_progress,View.VISIBLE);patch.setViewVisibility(R.id.choices,View.INVISIBLE);AppWidgetManager.getInstance(c).partiallyUpdateAppWidget(widget,patch);}catch(Exception ignored){}
  PendingResult pending=goAsync();App.IO.execute(()->{try{
   if(DecisionWidget.locked(c))throw new Exception("Unlock to answer");
   JSONObject s=new State(c).read();JSONArray rows=s.getJSONArray("rows");for(int n=0;n<rows.length();n++){JSONObject row=rows.getJSONObject(n);if(row.getString("id").equals(key)&&row.getString("digest").equals(parts.get(2))){if(!State.submitted(s,key))new State(c).queue(c,row,i.getStringExtra("choice"),"");DecisionWidget.update(c);App.sync(c);return;}}
   throw new Exception("Question changed. Tap the title to review.");
  }catch(Exception e){App.error(c,e);}finally{saving.remove(key);pending.finish();}});
 }
}
