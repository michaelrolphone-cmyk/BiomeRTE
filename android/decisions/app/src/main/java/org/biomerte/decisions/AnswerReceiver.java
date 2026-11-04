package org.biomerte.decisions;
import android.content.*;
import org.json.*;
import java.util.List;
public class AnswerReceiver extends BroadcastReceiver {
 @Override public void onReceive(Context c,Intent i){
  if(!"biome.decision.choice".equals(i.getAction())||i.getData()==null)return;
  PendingResult pending=goAsync();App.IO.execute(()->{try{
   if(DecisionWidget.locked(c))throw new Exception("Unlock to answer");List<String> parts=i.getData().getPathSegments();if(parts.size()!=3)return;
   JSONObject s=new State(c).read();JSONArray rows=s.getJSONArray("rows");for(int n=0;n<rows.length();n++){JSONObject row=rows.getJSONObject(n);if(row.getString("id").equals(parts.get(1))&&row.getString("digest").equals(parts.get(2))){new State(c).queue(c,row,i.getStringExtra("choice"),"");DecisionWidget.update(c);App.sync(c);return;}}
   throw new Exception("Question changed. Open Biome Decisions.");
  }catch(Exception e){App.error(c,e);}finally{pending.finish();}});
 }
}
