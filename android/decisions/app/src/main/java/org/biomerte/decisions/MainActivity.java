package org.biomerte.decisions;
import android.app.*;
import android.content.*;
import android.os.*;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
 private LinearLayout column;
 private String editing="";
 private EditText reply;
 private JSONObject displayed;
 private final java.util.List<Button> responseButtons=new java.util.ArrayList<>();
 private final Handler main=new Handler(Looper.getMainLooper());
 @Override public void onCreate(Bundle b){super.onCreate(b);if(b!=null)editing=b.getString("editing","");getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);if(Build.VERSION.SDK_INT>=33&&checkSelfPermission("android.permission.POST_NOTIFICATIONS")!=android.content.pm.PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},9);}
 @Override protected void onSaveInstanceState(Bundle b){saveDraft();b.putString("editing",editing);super.onSaveInstanceState(b);}
 @Override protected void onResume(){super.onResume();App.changed=()->{if(editing.isEmpty())load();};load();App.sync(this);}
 @Override protected void onPause(){saveDraft();App.changed=null;super.onPause();}
 private void saveDraft(){if(reply!=null&&!editing.isEmpty()){String id=editing,value=reply.getText().toString();App.work(this,()->new State(this).draft(id,value));}}
 private int dp(int n){return (int)(getResources().getDisplayMetrics().density*n+.5f);}
 private void page(String title){reply=null;displayed=null;responseButtons.clear();ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);column=new LinearLayout(this);column.setOrientation(LinearLayout.VERTICAL);column.setPadding(dp(20),dp(32),dp(20),dp(28));column.setBackgroundColor(getColor(R.color.canvas));scroll.addView(column);setContentView(scroll);label("BIOME DECISIONS",12,true);label(title,28,true);}
 private TextView label(String value,int size,boolean bold){TextView t=new TextView(this);t.setText(value);t.setTextColor(getColor(R.color.ink));t.setTextSize(size);if(bold)t.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));t.setPadding(0,dp(7),0,dp(10));column.addView(t,new LinearLayout.LayoutParams(-1,-2));return t;}
 private Button button(String title,boolean primary,Runnable click){Button b=new Button(this);b.setText(title);b.setAllCaps(false);b.setTextSize(16);b.setMinHeight(dp(52));b.setTextColor(getColor(primary?R.color.on_accent:R.color.accent));GradientDrawable background=new GradientDrawable();background.setColor(getColor(primary?R.color.accent:R.color.surface));background.setCornerRadius(dp(14));background.setStroke(dp(1),getColor(R.color.outline));b.setBackground(background);b.setPadding(dp(14),dp(10),dp(14),dp(10));b.setOnClickListener(v->click.run());LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=dp(10);column.addView(b,p);return b;}
 private void error(Exception e){Toast.makeText(this,e.getMessage()==null?"Could not update decisions":e.getMessage(),Toast.LENGTH_LONG).show();}
 private void load(){App.IO.execute(()->{try{JSONObject s=new State(this).read();runOnUiThread(()->{try{render(s);}catch(Exception e){recovery(e);}});}catch(Exception e){runOnUiThread(()->recovery(e));}});}
 private void refreshInbox(){saveDraft();editing="";load();App.sync(this);}
 private void navigation(boolean back){
  if(back)button("Back to inbox",false,()->{saveDraft();editing="";load();});
  button("Refresh",false,this::refreshInbox);
  button("Pairing settings",false,this::choosePairing);
 }
 private void recovery(Exception error){
  page("Could not review this request");navigation(true);
  label(error.getMessage()==null?"The request could not be displayed. Refresh to get the latest inbox.":error.getMessage(),16,false);
 }
 private void retrySaved(String id){App.work(this,()->{
  new State(this).retrySaved(id);DecisionWidget.update(this);
  main.post(()->{editing="";load();});App.sync(this);
 });}
 private void savedReply(JSONObject item)throws Exception{
  label(State.deliveryLabel(item),16,true);
  if(!item.optString("delivery_error").isEmpty())label(item.getString("delivery_error"),14,false);
  if(!item.optString("text").isEmpty())label("Original reply:\n"+item.getString("text"),14,false);
  if("blocked".equals(item.optString("state"))){
   label("Retry sends the same original signed reply. It does not change your choice or grant new permission.",13,false);
   String id=item.getString("id");button("Retry saved reply",false,()->retrySaved(id));
  }
 }
 private void render(JSONObject s)throws Exception{
  if(DecisionWidget.locked(this)){page("Unlock to review");return;}
  String requested=getIntent().getStringExtra("decision");if(editing.isEmpty()&&requested!=null&&!requested.isEmpty()){editing=requested;getIntent().removeExtra("decision");}
  JSONArray rows=State.pending(s);JSONObject row=null;if(!editing.isEmpty())for(int n=0;n<rows.length();n++)if(rows.getJSONObject(n).getString("id").equals(editing))row=rows.getJSONObject(n);
  if(row!=null){renderDecision(s,row);return;}
  editing="";page("Your decisions");
  if(!s.has("config")){label("Connect this phone to Biome to review decisions and answer workflow permission requests.",16,false);button("Choose pairing JSON",true,this::choosePairing);return;}
  navigation(false);
  App.start(this);String status=s.optString("status");label(status.isEmpty()?"Connected to Biome":status,14,false);
  JSONArray invalid=State.invalidRows(s);
  if(invalid.length()>0){
   label(invalid.length()+" request(s) cannot be reviewed",18,true);
   for(int n=0;n<invalid.length();n++)label(invalid.getJSONObject(n).getString("id")+"\n"+invalid.getJSONObject(n).getString("problem"),14,false);
   label("These requests are retained on this phone, with response controls disabled. Other valid requests remain available.",14,false);
  }
  if(rows.length()==0&&invalid.length()==0){label("✓",64,true);label("No decisions",30,true);}else if(rows.length()==0){label("Refresh after the request is corrected or withdrawn.",16,false);}else{
   label(rows.length()+" waiting on you",16,false);
   for(int n=0;n<rows.length();n++){JSONObject r=rows.getJSONObject(n),request=r.getJSONObject("request");String id=r.getString("id");button(request.optString("title","Decision")+"\n"+request.getString("question"),false,()->{editing=id;load();});}
  }
  JSONArray out=s.getJSONArray("outbox");for(int n=out.length()-1;n>=0;n--){JSONObject item=out.optJSONObject(n);if(item!=null&&!"accepted".equals(item.optString("state")))savedReply(item);}
 }
 void renderDecision(JSONObject s,JSONObject row)throws Exception{
  JSONObject r=row.optJSONObject("request");page(r==null?"Decision":r.optString("title","Decision"));navigation(true);
  String problem=DecisionModel.problem(row);
  if(!problem.isEmpty()){label("This request cannot be reviewed.\n"+problem,16,false);return;}
  label(r.getString("question"),20,true);label(DecisionModel.describe(r),15,false);displayed=row;
  JSONObject item=State.delivery(s,row.getString("id"));
  if(item!=null){savedReply(item);return;}
  label("Optional reply",14,true);reply=new EditText(this);reply.setTextSize(16);reply.setTextColor(getColor(R.color.ink));reply.setHint("Conditions, a question, or requested changes…");reply.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);reply.setMinLines(3);JSONObject drafts=s.optJSONObject("drafts");if(drafts!=null)reply.setText(drafts.optString(row.getString("id")));column.addView(reply,new LinearLayout.LayoutParams(-1,-2));
  boolean hasApprove=false;JSONArray options=r.getJSONArray("choices");
  for(int n=0;n<options.length();n++)if("approve".equals(options.getJSONObject(n).getString("id")))hasApprove=true;
  if(hasApprove)label("To approve without conditions, leave the optional reply empty and choose Approve. A written reply requests review; it is not an approval.",13,false);
  JSONArray choices=r.getJSONArray("choices");for(int n=0;n<choices.length();n++){JSONObject option=choices.getJSONObject(n);String choice=option.getString("id");responseButtons.add(button(option.getString("label"),n==0,()->answer(choice)));}
  responseButtons.add(button("Send written reply",false,()->answer("")));
 }
 private void answer(String choice){if(displayed==null||reply==null)return;JSONObject row=displayed;String value=reply.getText().toString();for(Button button:responseButtons)button.setEnabled(false);
  App.IO.execute(()->{try{new State(this).draft(row.getString("id"),value);new State(this).queue(this,row,choice,value);DecisionWidget.update(this);main.post(()->{editing="";load();});App.sync(this);}catch(Exception e){main.post(()->{error(e);load();});}});
 }
 private void choosePairing(){saveDraft();startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"),10);}
 @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request!=10||result!=RESULT_OK||data==null||data.getData()==null)return;
  App.NETWORK.execute(()->{try{String raw;try(InputStream in=getContentResolver().openInputStream(data.getData())){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[4096];int n;while((n=in.read(b))!=-1){if(out.size()+n>65536)throw new IOException("Pairing file too large");out.write(b,0,n);}raw=out.toString("UTF-8");}Client.pair(this,new JSONObject(raw));App.start(this);App.sync(this);main.post(()->{editing="";load();});}catch(Exception e){main.post(()->error(e));}});
 }
}
