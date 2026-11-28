package org.biomerte.decisions;
import android.content.Intent;
import android.widget.*;
import org.json.*;
import java.util.*;
public class ChoicesService extends RemoteViewsService {
 @Override public RemoteViewsFactory onGetViewFactory(Intent i){return new RemoteViewsFactory(){
  List<RemoteViews> rows=Collections.emptyList();
  public void onCreate(){}public void onDestroy(){rows=Collections.emptyList();}
  public void onDataSetChanged(){try{JSONObject state=new State(ChoicesService.this).read(),row=null;JSONArray all=state.getJSONArray("rows");for(int n=0;n<all.length();n++){JSONObject r=all.getJSONObject(n);if(r.getString("id").equals(i.getStringExtra("decision"))&&r.getString("digest").equals(i.getStringExtra("digest")))row=r;}rows=i.getBooleanExtra("description",false)?DecisionWidget.descriptionRows(ChoicesService.this,row,DecisionWidget.locked(ChoicesService.this)):DecisionWidget.rows(ChoicesService.this,row,DecisionWidget.locked(ChoicesService.this),row!=null&&State.submitted(state,row.getString("id")));}catch(Exception e){rows=Collections.emptyList();}}
  public int getCount(){return rows.size();}public RemoteViews getViewAt(int p){return p>=0&&p<rows.size()?rows.get(p):null;}public RemoteViews getLoadingView(){return null;}public int getViewTypeCount(){return 2;}public long getItemId(int p){return p;}public boolean hasStableIds(){return false;}
 };}
}
