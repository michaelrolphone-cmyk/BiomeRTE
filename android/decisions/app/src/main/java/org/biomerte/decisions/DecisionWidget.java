package org.biomerte.decisions;
import android.app.*;
import android.appwidget.*;
import android.content.*;
import android.net.Uri;
import android.os.*;
import android.view.View;
import android.widget.*;
import org.json.*;
import java.util.*;

public class DecisionWidget extends AppWidgetProvider {
 public static PendingIntent open(Context c,String id){Intent i=new Intent(c,MainActivity.class).setData(Uri.parse("biome-decisions://review/"+id)).putExtra("decision",id);return PendingIntent.getActivity(c,id.hashCode(),i,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);}
 private void refresh(Context c,boolean next){PendingResult result=goAsync();App.IO.execute(()->{try{if(next)new State(c).next();update(c);}catch(Exception e){App.error(c,e);}finally{result.finish();}});}
 @Override public void onUpdate(Context c,AppWidgetManager m,int[] ids){refresh(c,false);}
 @Override public void onAppWidgetOptionsChanged(Context c,AppWidgetManager m,int id,Bundle options){refresh(c,false);}
 @Override public void onReceive(Context c,Intent i){super.onReceive(c,i);if("biome.decisions.next".equals(i.getAction()))refresh(c,true);}
 public static boolean locked(Context c){return c.getSystemService(KeyguardManager.class).isDeviceLocked();}
 public static List<RemoteViews> rows(Context c,JSONObject row,boolean locked,boolean submitted)throws Exception{
  ArrayList<RemoteViews> rows=new ArrayList<>();
  String problem=locked||row==null?"":DecisionModel.problem(row);
  JSONObject request=locked||row==null?null:row.optJSONObject("request");
  RemoteViews question=new RemoteViews(c.getPackageName(),R.layout.widget_question);
  question.setTextViewText(R.id.title,locked?"Biome Decisions":request==null?"Biome Decisions":request.optString("title","Decision"));
  question.setTextViewText(R.id.question,locked?"Unlock to review":!problem.isEmpty()?"This request cannot be reviewed.\n"+problem+"\nOpen the inbox to refresh.":request==null?"Open inbox":request.getString("question")+"\n\n"+DecisionModel.describe(request));rows.add(question);
  if(!problem.isEmpty())return rows;
  if(!locked&&request!=null&&!submitted){JSONArray choices=request.getJSONArray("choices");for(int n=0;n<choices.length();n++){JSONObject option=choices.getJSONObject(n);RemoteViews view=new RemoteViews(c.getPackageName(),R.layout.widget_choice);view.setTextViewText(R.id.widget_choice,option.getString("label"));view.setContentDescription(R.id.widget_choice,option.getString("label"));view.setOnClickFillInIntent(R.id.widget_choice,new Intent().putExtra("choice",option.getString("id")));rows.add(view);}}
  return rows;
 }
 public static void update(Context c)throws Exception{
  AppWidgetManager m=AppWidgetManager.getInstance(c);int[] ids=m.getAppWidgetIds(new ComponentName(c,DecisionWidget.class));if(ids.length==0)return;
  JSONObject s=new State(c).read();JSONArray ready=State.pending(s);boolean isLocked=locked(c);int invalid=State.invalidRows(s).length(),attention=State.attentionCount(s);
  JSONObject row=ready.length()==0?null:ready.getJSONObject(Math.floorMod(s.optInt("index"),ready.length()));
  for(int id:ids){
   // Never show the all-clear for an unpaired or unobserved inbox.
   if(row==null&&invalid==0&&attention==0&&s.has("config")&&s.optLong("last_sync")>0&&s.optString("status").isEmpty()){
    RemoteViews empty=new RemoteViews(c.getPackageName(),R.layout.widget_empty);empty.setOnClickPendingIntent(R.id.empty_label,open(c,""));m.updateAppWidget(id,empty);continue;
   }
   RemoteViews view=new RemoteViews(c.getPackageName(),R.layout.widget);String key=row==null?"":row.getString("id"),digest=row==null?"":row.getString("digest");
   view.setTextViewText(R.id.count,"Biome Decisions · "+ready.length());view.setOnClickPendingIntent(R.id.count,open(c,key));
   String status=isLocked?"":s.optString("status");boolean submitted=row!=null&&State.submitted(s,key);
   if(!isLocked&&submitted)status=State.deliveryLabel(State.delivery(s,key));
   else if(!isLocked&&invalid>0)status=invalid+" request(s) cannot be reviewed; open inbox";
   else if(!isLocked&&attention>0)status="Saved replies need attention; open inbox";
   view.setTextViewText(R.id.response_status,status);view.setViewVisibility(R.id.response_status,status.isEmpty()?View.GONE:View.VISIBLE);
   view.setTextViewText(R.id.connection,s.optString("status").isEmpty()?(s.has("config")?"Connected to Biome":"Open to pair with Biome"):isLocked?"Open to review connection":s.optString("status"));
   Intent choice=new Intent(c,AnswerReceiver.class).setAction("biome.decision.choice").setData(Uri.parse("biome-decisions://choice/"+id+"/"+key+"/"+digest));
   view.setPendingIntentTemplate(R.id.choices,PendingIntent.getBroadcast(c,id,choice,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_MUTABLE));
   if(Build.VERSION.SDK_INT>=31){RemoteViews.RemoteCollectionItems.Builder items=new RemoteViews.RemoteCollectionItems.Builder().setViewTypeCount(2).setHasStableIds(false);List<RemoteViews> rows=rows(c,row,isLocked,submitted);for(int n=0;n<rows.size();n++)items.addItem(n,rows.get(n));view.setRemoteAdapter(R.id.choices,items.build());}
   else view.setRemoteAdapter(R.id.choices,new Intent(c,ChoicesService.class).setData(Uri.parse("biome-decisions://rows/"+id+"/"+key+"/"+digest)).putExtra("decision",key).putExtra("digest",digest));
   view.setTextViewText(R.id.other,submitted?"View saved reply":row==null?"Open inbox":"Write reply…");view.setOnClickPendingIntent(R.id.other,open(c,key));
   view.setViewVisibility(R.id.next,ready.length()>1?View.VISIBLE:View.GONE);view.setOnClickPendingIntent(R.id.next,PendingIntent.getBroadcast(c,id,new Intent(c,DecisionWidget.class).setAction("biome.decisions.next"),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE));m.updateAppWidget(id,view);if(Build.VERSION.SDK_INT<31)m.notifyAppWidgetViewDataChanged(id,R.id.choices);
  }
 }
}
