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
 private boolean saving=false;
 private String sendingId="";
 private final Handler main=new Handler(Looper.getMainLooper());
 @Override public void onCreate(Bundle b){super.onCreate(b);if(b!=null){editing=b.getString("editing","");sendingId=b.getString("sendingId","");}getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);if(Build.VERSION.SDK_INT>=33&&checkSelfPermission("android.permission.POST_NOTIFICATIONS")!=android.content.pm.PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},9);}
 @Override protected void onSaveInstanceState(Bundle b){saveDraft();b.putString("editing",editing);b.putString("sendingId",sendingId);super.onSaveInstanceState(b);}
 @Override protected void onResume(){super.onResume();App.changed=()->{if(editing.isEmpty()||!sendingId.isEmpty())load();};load();App.sync(this);}
 @Override protected void onPause(){saveDraft();App.changed=null;super.onPause();}
 private void saveDraft(){if(reply!=null&&!editing.isEmpty()){String id=editing,value=reply.getText().toString();App.work(this,()->new State(this).draft(id,value));}}
 private int dp(int n){return (int)(getResources().getDisplayMetrics().density*n+.5f);}
 private void page(String title){reply=null;displayed=null;ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);column=new LinearLayout(this);column.setOrientation(LinearLayout.VERTICAL);column.setPadding(dp(20),dp(32),dp(20),dp(28));column.setBackgroundColor(getColor(R.color.canvas));scroll.addView(column);setContentView(scroll);label("BIOME DECISIONS",12,true);label(title,28,true);}
 private TextView label(String value,int size,boolean bold){TextView t=new TextView(this);t.setText(value);t.setTextColor(getColor(R.color.ink));t.setTextSize(size);if(bold)t.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));t.setPadding(0,dp(7),0,dp(10));column.addView(t,new LinearLayout.LayoutParams(-1,-2));return t;}
 private Button button(String title,boolean primary,Runnable click){Button b=new Button(this);b.setText(title);b.setAllCaps(false);b.setTextSize(16);b.setMinHeight(dp(52));b.setTextColor(getColor(primary?R.color.on_accent:R.color.accent));GradientDrawable background=new GradientDrawable();background.setColor(getColor(primary?R.color.accent:R.color.surface));background.setCornerRadius(dp(14));background.setStroke(dp(1),getColor(R.color.outline));b.setBackground(background);b.setPadding(dp(14),dp(10),dp(14),dp(10));b.setOnClickListener(v->click.run());LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=dp(10);column.addView(b,p);return b;}
 private void error(Exception e){Toast.makeText(this,e.getMessage()==null?"Could not update decisions":e.getMessage(),Toast.LENGTH_LONG).show();}
 private void load(){if(saving)return;App.work(this,()->{JSONObject s=new State(this).read();runOnUiThread(()->{try{if(!saving)render(s);}catch(Exception e){error(e);}});});}
 private void render(JSONObject s)throws Exception{
  if(DecisionWidget.locked(this)){page("Unlock to review");return;}
  if(!sendingId.isEmpty()){renderDelivery(s,sendingId);return;}
  String requested=getIntent().getStringExtra("decision");if(editing.isEmpty()&&requested!=null&&!requested.isEmpty()){editing=requested;getIntent().removeExtra("decision");}
  JSONArray rows=State.pending(s);JSONObject row=null;if(!editing.isEmpty())for(int n=0;n<rows.length();n++)if(rows.getJSONObject(n).getString("id").equals(editing))row=rows.getJSONObject(n);
  if(row!=null){renderDecision(s,row);return;}
  editing="";page("Your decisions");
  if(!s.has("config")){label("Connect this phone to Biome to review decisions and answer workflow permission requests.",16,false);button("Choose pairing JSON",true,this::choosePairing);return;}
  App.start(this);String status=s.optString("status");label(status.isEmpty()?"Connected to Biome":status,14,false);
  if(rows.length()==0){label("✓",64,true);label("No decisions",30,true);}else{
   label(rows.length()+" waiting on you",16,false);
   for(int n=0;n<rows.length();n++){JSONObject r=rows.getJSONObject(n),request=r.getJSONObject("request");String id=r.getString("id");button(request.optString("title","Decision")+"\n"+request.getString("question"),false,()->{editing=id;load();});}
  }
  JSONArray out=s.getJSONArray("outbox");for(int n=out.length()-1;n>=0;n--){JSONObject item=out.getJSONObject(n);if(!"accepted".equals(item.getString("state")))label("Saved reply · "+("needs_review".equals(item.getString("state"))?"question changed; review required":"waiting to send")+(!item.optString("text").isEmpty()?"\n"+item.getString("text"):""),14,false);}
  button("Refresh",false,()->App.sync(this));button("Pairing settings",false,this::choosePairing);
 }
 private void renderDecision(JSONObject s,JSONObject row)throws Exception{
  JSONObject r=row.getJSONObject("request");page(r.optString("title","Decision"));displayed=row;label(r.getString("question"),20,true);label(DecisionModel.describe(r),15,false);
  if(State.submitted(s,row.getString("id"))){sendingId=row.getString("id");renderDelivery(s,sendingId);return;}
  label("Optional reply",14,true);reply=new EditText(this);reply.setTextSize(16);reply.setTextColor(getColor(R.color.ink));reply.setHint("Conditions, a question, or requested changes…");reply.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);reply.setMinLines(3);JSONObject drafts=s.optJSONObject("drafts");if(drafts!=null)reply.setText(drafts.optString(row.getString("id")));column.addView(reply,new LinearLayout.LayoutParams(-1,-2));
  if(r.has("authorization_request"))label("An approval with written conditions or questions will require clarification before permission is granted.",13,false);
  JSONArray choices=r.getJSONArray("choices");for(int n=0;n<choices.length();n++){JSONObject option=choices.getJSONObject(n);String choice=option.getString("id");button(option.getString("label"),n==0,()->answer(choice));}
  button("Send written reply",false,()->answer(""));button("Back to inbox",false,()->{saveDraft();editing="";load();});
 }
 private void progress(String message){ProgressBar p=new ProgressBar(this);p.setIndeterminate(true);p.setContentDescription(message);column.addView(p,new LinearLayout.LayoutParams(dp(40),dp(40)));TextView t=label(message,18,true);t.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);}
 private void renderDelivery(JSONObject s,String id)throws Exception{
  JSONObject item=null;JSONArray out=s.getJSONArray("outbox");for(int n=0;n<out.length();n++)if(out.getJSONObject(n).getString("decision_id").equals(id))item=out.getJSONObject(n);
  page("Your reply");
  if(item==null){progress("Saving your reply…");return;}
  String state=item.getString("state");
  if(state.equals("accepted")){label("✓ Received by Biome",26,true);label("Your reply was accepted.",16,false);}
  else if(state.equals("needs_review")){label("Question changed",24,true);label("Your reply is saved, but Biome has not accepted it. Review the updated question.",16,false);}
  else{progress("Reply saved · sending to Biome…");label("Your reply will retry automatically if the connection is interrupted.",16,false);if(!s.optString("status").isEmpty())label(s.getString("status"),14,false);}
  if(!item.optString("text").isEmpty())label(item.getString("text"),16,false);
  button("Back to inbox",false,()->{sendingId="";editing="";load();});
 }
 private void answer(String choice){
  if(saving||!sendingId.isEmpty()||displayed==null||reply==null)return;
  JSONObject row=displayed;String value=reply.getText().toString();
  try{DecisionModel.validate(row,choice,value);}catch(Exception e){error(e);return;}
  saving=true;sendingId=row.optString("id");reply.setEnabled(false);
  for(int n=0;n<column.getChildCount();n++)if(column.getChildAt(n) instanceof Button)column.getChildAt(n).setEnabled(false);
  page("Your reply");progress("Saving and sending your reply…");
  App.IO.execute(()->{try{
   saveReply(row,choice,value);main.post(()->{saving=false;load();});App.sync(this);
  }catch(Exception e){main.post(()->{saving=false;sendingId="";error(e);load();});}});
 }
 protected void saveReply(JSONObject row,String choice,String value)throws Exception{new State(this).draft(row.getString("id"),value);new State(this).queue(this,row,choice,value);DecisionWidget.update(this);}
 private void choosePairing(){saveDraft();startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"),10);}
 @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request!=10||result!=RESULT_OK||data==null||data.getData()==null)return;
  App.IO.execute(()->{try{String raw;try(InputStream in=getContentResolver().openInputStream(data.getData())){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[4096];int n;while((n=in.read(b))!=-1){if(out.size()+n>65536)throw new IOException("Pairing file too large");out.write(b,0,n);}raw=out.toString("UTF-8");}Client.pair(this,new JSONObject(raw));App.start(this);App.sync(this);main.post(()->{editing="";load();});}catch(Exception e){main.post(()->error(e));}});
 }
}
