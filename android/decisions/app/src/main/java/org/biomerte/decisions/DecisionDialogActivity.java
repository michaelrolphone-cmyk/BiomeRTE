package org.biomerte.decisions;
import android.app.*;
import android.content.*;
import android.os.*;
import android.view.*;
import android.view.inputmethod.InputMethodManager;
import android.widget.*;
import org.json.*;

public class DecisionDialogActivity extends Activity {
 private String id="";private boolean write,busy;private JSONObject row;private LinearLayout body;private EditText reply;private String draft="";
 private final Handler main=new Handler(Looper.getMainLooper());
 @Override public void onCreate(Bundle state){super.onCreate(state);getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);id=getIntent().getStringExtra("decision");if(id==null)id="";write=getIntent().getBooleanExtra("write",false);if(state!=null)draft=state.getString("draft","");page("Loading decision…");progress("Loading…");}
 @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);saveDraft();setIntent(intent);id=intent.getStringExtra("decision");if(id==null)id="";write=intent.getBooleanExtra("write",false);reply=null;row=null;draft="";load();}
 @Override protected void onResume(){super.onResume();if(DecisionWidget.locked(this)){page("Unlock to review");button("Close",this::close);return;}App.changed=this::refresh;load();App.sync(this);}
 @Override protected void onPause(){saveDraft();App.changed=null;super.onPause();}
 @Override protected void onSaveInstanceState(Bundle b){if(reply!=null)draft=reply.getText().toString();b.putString("draft",draft);super.onSaveInstanceState(b);}
 private void close(){finishAndRemoveTask();}
 private int dp(int n){return (int)(n*getResources().getDisplayMetrics().density+.5f);}
 private void page(String title){reply=null;body=new LinearLayout(this);body.setOrientation(LinearLayout.VERTICAL);body.setPadding(dp(20),dp(16),dp(20),dp(12));body.setBackgroundColor(getColor(R.color.canvas));setContentView(body);TextView t=label(title,20);t.setTypeface(null,android.graphics.Typeface.BOLD);}
 private TextView label(String text,int size){TextView t=new TextView(this);t.setText(text);t.setTextSize(size);t.setTextColor(getColor(R.color.ink));t.setPadding(0,dp(6),0,dp(8));body.addView(t);return t;}
 private Button button(String text,Runnable action){Button b=new Button(this);b.setText(text);b.setAllCaps(false);b.setTextColor(getColor(R.color.accent));b.setOnClickListener(v->action.run());body.addView(b,new LinearLayout.LayoutParams(-1,-2));return b;}
 private void progress(String text){ProgressBar p=new ProgressBar(this);p.setIndeterminate(true);body.addView(p,new LinearLayout.LayoutParams(dp(36),dp(36)));label(text,16).setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);}
 private void saveDraft(){if(reply==null||row==null||busy)return;String value=reply.getText().toString(),key=id;draft=value;App.work(this,()->new State(this).draft(key,value));}
 private void refresh(){if(busy||DecisionWidget.locked(this))return;App.work(this,()->{JSONObject s=new State(this).read();main.post(()->{try{if(!busy&&(row==null||State.submitted(s,id)||!hasCurrentRow(s)))render(s);}catch(Exception e){failure(e);}});});}
 private boolean hasCurrentRow(JSONObject s)throws Exception{JSONArray rows=s.getJSONArray("rows");for(int n=0;n<rows.length();n++){JSONObject current=rows.getJSONObject(n);if(current.getString("id").equals(id)&&current.getString("digest").equals(row.getString("digest")))return true;}return false;}
 private void load(){if(busy)return;App.work(this,()->{JSONObject s=new State(this).read();main.post(()->{try{if(!busy)render(s);}catch(Exception e){failure(e);}});});}
 private void render(JSONObject s)throws Exception{
  if(isFinishing())return;if(DecisionWidget.locked(this)){page("Unlock to review");button("Close",this::close);return;}
  JSONObject item=null;JSONArray out=s.getJSONArray("outbox");for(int n=0;n<out.length();n++)if(out.getJSONObject(n).getString("decision_id").equals(id))item=out.getJSONObject(n);
  if(item!=null){page("Your reply");String state=item.getString("state");if(state.equals("accepted"))label("✓ Received by Biome",22);else if(state.equals("needs_review"))label("Question changed. Your saved reply needs review.",18);else{progress("Reply saved · sending to Biome…");label(s.optString("status","Your reply will retry automatically."),14);}button("Close",this::close);return;}
  row=null;JSONArray all=s.getJSONArray("rows");for(int n=0;n<all.length();n++)if(all.getJSONObject(n).getString("id").equals(id))row=all.getJSONObject(n);
  if(row==null){page(id.isEmpty()?"No decisions":"Question no longer available");button("Refresh",()->App.sync(this));button("Close",this::close);return;}
  JSONObject request=row.getJSONObject("request");page(DecisionModel.title(request));
  ScrollView scroll=new ScrollView(this);TextView question=new TextView(this);question.setText(DecisionModel.fullQuestion(request));question.setTextColor(getColor(R.color.ink));question.setTextSize(16);question.setPadding(0,dp(8),0,dp(12));scroll.addView(question);int max=(int)(getResources().getDisplayMetrics().heightPixels*(write?.24:.55));question.measure(View.MeasureSpec.makeMeasureSpec(Math.max(dp(200),getResources().getDisplayMetrics().widthPixels-dp(72)),View.MeasureSpec.EXACTLY),View.MeasureSpec.makeMeasureSpec(0,View.MeasureSpec.UNSPECIFIED));body.addView(scroll,new LinearLayout.LayoutParams(-1,Math.min(max,question.getMeasuredHeight())));
  if(write){reply=new EditText(this);reply.setHint("Write your response…");reply.setTextColor(getColor(R.color.ink));reply.setTextSize(16);reply.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);reply.setMinLines(2);reply.setMaxLines(5);JSONObject drafts=s.optJSONObject("drafts");reply.setText(!draft.isEmpty()?draft:drafts==null?"":drafts.optString(id));body.addView(reply,new LinearLayout.LayoutParams(-1,-2));button("Send reply",()->send(""));reply.requestFocus();reply.post(()->getSystemService(InputMethodManager.class).showSoftInput(reply,InputMethodManager.SHOW_IMPLICIT));}
  else{button("Write reply…",()->{write=true;renderAgain(s);});}
  button("Close",this::close);
 }
 private void renderAgain(JSONObject s){try{render(s);}catch(Exception e){failure(e);}}
 private void failure(Exception e){busy=false;page("Could not send reply");label(e.getMessage()==null?"Your saved reply is retained.":e.getMessage(),16);button("Review",this::load);button("Close",this::close);}
 private void send(String choice){if(busy||row==null||reply==null)return;String value=reply.getText().toString();JSONObject selected=row;
  try{DecisionModel.validate(selected,choice,value);}catch(Exception e){reply.setError(e.getMessage());return;}
  busy=true;draft=value;getSystemService(InputMethodManager.class).hideSoftInputFromWindow(reply.getWindowToken(),0);page("Your reply");progress("Saving and sending your reply…");
  App.IO.execute(()->{try{new State(this).draft(id,value);new State(this).queue(this,selected,choice,value);DecisionWidget.update(this);main.post(()->{busy=false;load();});App.sync(this);}catch(Exception e){main.post(()->failure(e));}});
 }
}
