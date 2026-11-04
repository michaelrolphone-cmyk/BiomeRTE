package org.biomerte.decisions;
import android.app.*;
import android.appwidget.*;
import android.content.*;
import android.content.res.Configuration;
import android.net.Uri;
import android.os.*;
import android.view.View;
import android.widget.*;
import org.json.*;
import java.util.*;

public class DecisionWidget extends AppWidgetProvider {
 public static PendingIntent open(Context c,String id){Intent i=new Intent(c,MainActivity.class).setData(Uri.parse("biome-decisions://review/"+id)).putExtra("decision",id);return PendingIntent.getActivity(c,id.hashCode(),i,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);}
 public static PendingIntent dialog(Context c,String id,boolean write){Intent i=new Intent(c,DecisionDialogActivity.class).setData(Uri.parse("biome-decisions://dialog/"+id+"/"+write)).putExtra("decision",id).putExtra("write",write).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_CLEAR_TOP);return PendingIntent.getActivity(c,(id+write).hashCode(),i,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);}
 private void refresh(Context c,boolean next){PendingResult result=goAsync();App.IO.execute(()->{try{if(next)new State(c).next();update(c);}catch(Exception e){App.error(c,e);}finally{result.finish();}});}
 @Override public void onUpdate(Context c,AppWidgetManager m,int[] ids){refresh(c,false);}
 @Override public void onAppWidgetOptionsChanged(Context c,AppWidgetManager m,int id,Bundle options){refresh(c,false);}
 @Override public void onReceive(Context c,Intent i){super.onReceive(c,i);if("biome.decisions.next".equals(i.getAction()))refresh(c,true);}
 public static boolean locked(Context c){return c.getSystemService(KeyguardManager.class).isDeviceLocked();}
 // The description has its own viewport; it never pushes answer buttons below it.
 public static int descriptionBudget(int height,int choices){return height-112-Math.min(144,Math.max(48,choices*48));}
 public static int mode(int height,int choices){int budget=descriptionBudget(height,choices);return budget>88?2:budget>=22?1:0;}
 public static List<RemoteViews> rows(Context c,JSONObject row,boolean locked,boolean submitted)throws Exception{
  ArrayList<RemoteViews> rows=new ArrayList<>();if(locked||row==null||submitted)return rows;
  JSONArray choices=row.getJSONObject("request").getJSONArray("choices");for(int n=0;n<choices.length();n++){JSONObject option=choices.getJSONObject(n);RemoteViews v=new RemoteViews(c.getPackageName(),R.layout.widget_choice);v.setTextViewText(R.id.widget_choice,option.getString("label"));v.setContentDescription(R.id.widget_choice,option.getString("label"));v.setOnClickFillInIntent(R.id.widget_choice,new Intent().putExtra("choice",option.getString("id")));rows.add(v);}return rows;
 }
 public static List<RemoteViews> descriptionRows(Context c,JSONObject row,boolean locked)throws Exception{
  RemoteViews v=new RemoteViews(c.getPackageName(),R.layout.widget_question);v.setViewVisibility(R.id.title,View.GONE);v.setTextViewText(R.id.question,locked?"Unlock to review":row==null?"No question":DecisionModel.fullQuestion(row.getJSONObject("request")));v.setOnClickFillInIntent(R.id.question,new Intent());return Collections.singletonList(v);
 }
 private static void collection(Context c,RemoteViews view,int id,List<RemoteViews> rows,Intent service){
  if(Build.VERSION.SDK_INT>=31){RemoteViews.RemoteCollectionItems.Builder items=new RemoteViews.RemoteCollectionItems.Builder().setViewTypeCount(1).setHasStableIds(false);for(int n=0;n<rows.size();n++)items.addItem(n,rows.get(n));view.setRemoteAdapter(id,items.build());}else view.setRemoteAdapter(id,service);
 }
 public static RemoteViews render(Context c,JSONObject s,JSONObject row,int widgetId,int height,boolean isLocked)throws Exception{
  String key=row==null?"":row.getString("id"),digest=row==null?"":row.getString("digest");JSONObject request=row==null?null:row.getJSONObject("request");
  int count=request==null?0:request.getJSONArray("choices").length(),mode=mode(height,count);boolean submitted=row!=null&&State.submitted(s,key);
  RemoteViews view=new RemoteViews(c.getPackageName(),mode==2?R.layout.widget_expanded:mode==1?R.layout.widget:R.layout.widget_compact);
  String title=isLocked?"Unlock to review":request==null?(s.has("config")?"Biome Decisions":"Tap to pair with Biome"):DecisionModel.title(request);
  view.setTextViewText(R.id.count,title);view.setContentDescription(R.id.count,title+". Read full question");
  view.setOnClickPendingIntent(R.id.count,row==null&&!s.has("config")?open(c,""):dialog(c,key,false));
  if(mode==1){view.setTextViewText(R.id.question_preview,isLocked?"":request==null?"":request.getString("question"));view.setInt(R.id.question_preview,"setMaxLines",Math.max(1,Math.min(4,descriptionBudget(height,count)/22)));view.setOnClickPendingIntent(R.id.question_preview,dialog(c,key,false));}
  Intent service=new Intent(c,ChoicesService.class).putExtra("decision",key).putExtra("digest",digest);
  if(mode==2){
   view.setPendingIntentTemplate(R.id.description,dialog(c,key,false));
   collection(c,view,R.id.description,descriptionRows(c,row,isLocked),new Intent(service).putExtra("description",true).setData(Uri.parse("biome-decisions://description/"+widgetId+"/"+key+"/"+digest)));
   if(Build.VERSION.SDK_INT>=31)view.setViewLayoutHeight(R.id.choices,Math.min(144,Math.max(48,count*48)),android.util.TypedValue.COMPLEX_UNIT_DIP);
  }
  String status=isLocked?"":s.optString("status");if(submitted&&!isLocked)status="Reply saved · sending";
  view.setTextViewText(R.id.response_status,status);view.setViewVisibility(R.id.response_status,status.isEmpty()?View.GONE:View.VISIBLE);view.setViewVisibility(R.id.sending_progress,submitted&&!isLocked?View.VISIBLE:View.GONE);
  Intent choice=new Intent(c,AnswerReceiver.class).setAction("biome.decision.choice").setData(Uri.parse("biome-decisions://choice/"+widgetId+"/"+key+"/"+digest));
  view.setPendingIntentTemplate(R.id.choices,PendingIntent.getBroadcast(c,widgetId,choice,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_MUTABLE));
  collection(c,view,R.id.choices,rows(c,row,isLocked,submitted),service.setData(Uri.parse("biome-decisions://choices/"+widgetId+"/"+key+"/"+digest)));
  view.setTextViewText(R.id.other,submitted?"View reply":row==null?"Refresh":"Write reply…");view.setOnClickPendingIntent(R.id.other,dialog(c,key,true));view.setViewVisibility(R.id.other,isLocked?View.GONE:View.VISIBLE);
  view.setViewVisibility(R.id.next,!isLocked&&State.pending(s).length()>1?View.VISIBLE:View.GONE);
  view.setOnClickPendingIntent(R.id.next,PendingIntent.getBroadcast(c,widgetId,new Intent(c,DecisionWidget.class).setAction("biome.decisions.next"),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE));
  return view;
 }
 public static void update(Context c)throws Exception{
  AppWidgetManager m=AppWidgetManager.getInstance(c);int[] ids=m.getAppWidgetIds(new ComponentName(c,DecisionWidget.class));if(ids.length==0)return;
  JSONObject s=new State(c).read();JSONArray ready=State.pending(s);boolean isLocked=locked(c);JSONObject row=ready.length()==0?null:ready.getJSONObject(Math.floorMod(s.optInt("index"),ready.length()));
  for(int id:ids){
   if(row==null&&s.has("config")&&s.optLong("last_sync")>0&&s.optString("status").isEmpty()){RemoteViews empty=new RemoteViews(c.getPackageName(),R.layout.widget_empty);empty.setOnClickPendingIntent(R.id.empty_label,dialog(c,"",false));m.updateAppWidget(id,empty);continue;}
   Bundle options=m.getAppWidgetOptions(id);int height=options.getInt(c.getResources().getConfiguration().orientation==Configuration.ORIENTATION_LANDSCAPE?AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT:AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT,180);
   m.updateAppWidget(id,render(c,s,row,id,height,isLocked));if(Build.VERSION.SDK_INT<31){m.notifyAppWidgetViewDataChanged(id,R.id.choices);if(mode(height,row==null?0:row.getJSONObject("request").getJSONArray("choices").length())==2)m.notifyAppWidgetViewDataChanged(id,R.id.description);}
  }
 }
}
