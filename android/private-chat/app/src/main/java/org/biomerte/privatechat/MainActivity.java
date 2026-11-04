package org.biomerte.privatechat;
import android.app.*;
import android.os.*;
import android.content.*;
import android.net.Uri;
import android.text.InputType;
import android.view.*;
import android.view.inputmethod.EditorInfo;
import android.widget.*;
import org.json.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
public final class MainActivity extends Activity {
 private Vault vault; private JSONObject data; private byte[] vaultKey,salt;
 private Button threadButton,modelButton,sendButton,retryButton,chatModeButton,imageModeButton; private LinearLayout messageList,toolbar; private ScrollView chatScroll; private AlertDialog modelDialog,threadDialog,infoDialog; private boolean imageMode=false,connectionScreen=false,conversationsScreen=false,returnToConnection=false; private LinearLayout root,imageHistory; private final java.util.List<android.graphics.Bitmap> bitmaps=new java.util.ArrayList<>(); private TextView status,history,screenTitle; private LinearLayout conversationRows; private EditText composer;
 private final ExecutorService executor=Executors.newCachedThreadPool(); private Future<?> transport;
 private final java.util.Map<String,Biome> requestClients=new java.util.HashMap<>(); private final java.util.Map<String,Future<?>> requestJobs=new java.util.HashMap<>(); private final Handler recoveryHandler=new Handler(Looper.getMainLooper());
 private final Biome biome=new Biome(); private volatile int epoch=0; private boolean busy=false; private Uri pairingUri;
 private static final String REDIRECT=Biome.ORIGIN+"/android/private-chat-callback";
 @Override public void onCreate(Bundle b){super.onCreate(b);getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);vault=new Vault(this);locked();}
 @Override protected void onStop(){
  if(data!=null&&vaultKey!=null&&composer!=null)try{data.put("draft",composer.getText().toString());save();}catch(Exception ignored){}
  super.onStop();
 }
 @Override protected void onResume(){super.onResume();if(returnToConnection&&data!=null){returnToConnection=false;settings();}if(data!=null)resume();}
 @Override public void onBackPressed(){if(connectionScreen&&data!=null){chat();}else if(data!=null&&!conversationsScreen){chooseConversation();}else{lock();super.onBackPressed();}}
 @Override protected void onDestroy(){lock();executor.shutdownNow();super.onDestroy();}
 private int dp(float n){return Math.round(n*getResources().getDisplayMetrics().density);}
 private android.graphics.drawable.GradientDrawable surface(int color,int radius){android.graphics.drawable.GradientDrawable d=new android.graphics.drawable.GradientDrawable();d.setColor(color);d.setCornerRadius(dp(radius));return d;}
 private TextView text(String value,int size,int color){TextView t=new TextView(this);t.setText(value);t.setTextSize(size);t.setTextColor(color);t.setLineSpacing(dp(3),1);t.setSaveEnabled(false);return t;}
 private void space(LinearLayout parent,int height){View v=new View(this);parent.addView(v,new LinearLayout.LayoutParams(1,dp(height)));}
 private void base(String title){
  root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);root.setBackgroundColor(0xff17231e);
  root.setPadding(dp(16),dp(12),dp(16),dp(10));
  getWindow().setStatusBarColor(0xff17231e);getWindow().setNavigationBarColor(0xff17231e);
  if(Build.VERSION.SDK_INT>=30){getWindow().setDecorFitsSystemWindows(false);root.setOnApplyWindowInsetsListener((v,insets)->{android.graphics.Insets bars=insets.getInsets(WindowInsets.Type.systemBars()),ime=insets.getInsets(WindowInsets.Type.ime());v.setPadding(dp(16)+bars.left,dp(8)+bars.top,dp(16)+bars.right,dp(8)+Math.max(bars.bottom,ime.bottom));return insets;});}else root.setFitsSystemWindows(true);
  toolbar=new LinearLayout(this);toolbar.setGravity(Gravity.CENTER_VERTICAL);
  ImageView mark=new ImageView(this);mark.setImageResource(R.drawable.biome_mark);mark.setContentDescription("Biome");toolbar.addView(mark,new LinearLayout.LayoutParams(dp(40),dp(40)));
  TextView heading=text(title,21,0xffe9ede4);screenTitle=heading;heading.setMaxLines(1);heading.setEllipsize(android.text.TextUtils.TruncateAt.END);heading.setTypeface(null,android.graphics.Typeface.BOLD);heading.setPadding(dp(10),0,0,0);toolbar.addView(heading,new LinearLayout.LayoutParams(0,dp(48),1));heading.setGravity(Gravity.CENTER_VERTICAL);root.addView(toolbar);
  status=text("",12,0xffcad9c5);status.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);status.setPadding(dp(2),dp(4),0,dp(10));root.addView(status);setContentView(root);
 }
 private void formArea(){LinearLayout shell=root;ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);LinearLayout form=new LinearLayout(this);form.setOrientation(LinearLayout.VERTICAL);scroll.addView(form);shell.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));root=form;}
 private Button action(String label,Runnable task,boolean primary){
  Button b=new Button(this);b.setText(label);b.setAllCaps(false);b.setTextSize(15);b.setTextColor(primary?0xff17231e:0xffe9ede4);b.setBackground(surface(primary?0xffbfdfb6:0xff294033,14));b.setMinHeight(dp(48));b.setMinimumHeight(dp(48));b.setPadding(dp(14),dp(8),dp(14),dp(8));b.setOnClickListener(v->task.run());b.setSaveEnabled(false);return b;
 }
 private Button button(String label,Runnable task){Button b=action(label,task,label.equals("Unlock")||label.startsWith("Create encrypted")||label.startsWith("Paste"));LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,dp(50));p.bottomMargin=dp(10);root.addView(b,p);return b;}
 private EditText input(String hint,boolean secret){
  EditText e=new EditText(this);e.setHint(hint);e.setTextSize(16);e.setTextColor(0xffe9ede4);e.setHintTextColor(0xffa7b6a2);e.setSaveEnabled(false);e.setBackground(surface(0xff21362b,14));e.setPadding(dp(14),dp(12),dp(14),dp(12));e.setMinHeight(dp(52));
  e.setInputType(secret?InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD:InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_FLAG_MULTI_LINE|InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
  e.setImeOptions(EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING);return e;
 }
 private EditText field(String hint,boolean secret){EditText e=input(hint,secret);LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=dp(12);root.addView(e,p);return e;}
 private void note(String title,String body){TextView h=text(title,18,0xffe9ede4);h.setTypeface(null,android.graphics.Typeface.BOLD);root.addView(h);space(root,6);root.addView(text(body,14,0xffcad9c5));space(root,16);}
 private String modelName(String id){JSONArray a=data==null?null:data.optJSONArray("models");if(a!=null)for(int i=0;i<a.length();i++){JSONObject m=a.optJSONObject(i);if(m!=null&&id.equals(m.optString("id")))return m.optString("name",id);}return id.isEmpty()?"Mac default":id;}
 private void updateControls(){
  if(data==null)return;
  if(sendButton!=null)sendButton.setEnabled(!busy&&composer!=null&&!composer.getText().toString().trim().isEmpty());
  if(sendButton!=null)sendButton.setAlpha(sendButton.isEnabled()?1f:0.45f);
  if(retryButton!=null)try{int count=Requests.all(data).length();retryButton.setVisibility(count>0?View.VISIBLE:View.GONE);retryButton.setText("Requests · "+count);}catch(Exception ignored){}
  if(modelButton!=null)modelButton.setEnabled(!busy);
 }
 private void switchMode(boolean image){imageMode=image;chatModeButton.setBackground(surface(image?0xff21362b:0xff406348,12));imageModeButton.setBackground(surface(image?0xff406348:0xff21362b,12));composer.setHint(image?"Describe an image…":"Message…");sendButton.setContentDescription(image?"Generate image":"Send message");modelButton.setVisibility(image?View.GONE:View.VISIBLE);}
 private void menu(View anchor){PopupMenu menu=new PopupMenu(this,anchor);boolean pinned=false;try{pinned=Conversations.current(data).optBoolean("pinned");}catch(Exception ignored){}for(String label:new String[]{"Conversations","New conversation","Conversation settings",pinned?"Unpin conversation":"Pin conversation","Connection","Lock"})menu.getMenu().add(label);menu.setOnMenuItemClickListener(item->{switch(item.getTitle().toString()){case "Conversations":chooseConversation();break;case "New conversation":newConversation();break;case "Conversation settings":renameConversation();break;case "Pin conversation":case "Unpin conversation":togglePin();break;case "Connection":settings();break;case "Lock":lock();break;}return true;});menu.show();}
 private String threadLabel()throws Exception{JSONObject t=Conversations.current(data);return (t.optBoolean("pinned")?"📌  ":"")+t.getString("title");}
 private void togglePin(){try{JSONObject thread=Conversations.current(data);thread.put("pinned",!thread.optBoolean("pinned"));save();if(!conversationsScreen)screenTitle.setText(threadLabel());status.setText(thread.optBoolean("pinned")?"Conversation pinned to the top.":"Conversation unpinned.");}catch(Exception e){fail();}}
 private void saveDraft()throws Exception{if(composer!=null)data.put("draft",composer.getText().toString());}
 private boolean leaveConversation(){try{saveDraft();if(!parkPending())return false;save();return true;}catch(Exception e){fail();return false;}}
 private void newConversation(){if(!leaveConversation())return;try{Conversations.create(data);save();chat();renameConversation();}catch(Exception e){fail();}}
 private void selectConversation(String id){if(id.equals(data.optString("active_thread"))){if(conversationsScreen)chat();return;}if(!leaveConversation())return;try{Conversations.select(data,id);save();chat();}catch(Exception e){fail();}}
 private void chooseConversation(){
  if(data==null||!leaveConversation())return;
  try{Conversations.ensure(data);if(messageList!=null)messageList.removeAllViews();clearBitmaps();messageList=null;imageHistory=null;composer=null;chatScroll=null;threadButton=null;modelButton=null;sendButton=null;retryButton=null;
   connectionScreen=false;conversationsScreen=true;base("Conversations");status.setText("Private threads · encrypted on this phone");
   Button more=action("⋮",()->{},false);more.setContentDescription("App options");more.setOnClickListener(v->{PopupMenu m=new PopupMenu(this,v);m.getMenu().add("Connection");m.getMenu().add("Lock");m.setOnMenuItemClickListener(item->{if(item.getTitle().toString().equals("Connection"))settings();else lock();return true;});m.show();});toolbar.addView(more,new LinearLayout.LayoutParams(dp(48),dp(44)));
   Button create=action("＋  New conversation",this::newConversation,true);create.setContentDescription("New conversation");root.addView(create,new LinearLayout.LayoutParams(-1,dp(48)));space(root,10);
   EditText search=input("Search conversations",false);search.setSingleLine(true);search.setContentDescription("Search conversations");root.addView(search,new LinearLayout.LayoutParams(-1,dp(48)));space(root,8);
   ScrollView scroll=new ScrollView(this);conversationRows=new LinearLayout(this);conversationRows.setOrientation(LinearLayout.VERTICAL);scroll.addView(conversationRows);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));renderConversationRows("");
   search.addTextChangedListener(new android.text.TextWatcher(){public void beforeTextChanged(CharSequence x,int a,int c,int z){}public void onTextChanged(CharSequence x,int a,int before,int count){renderConversationRows(x.toString());}public void afterTextChanged(android.text.Editable x){}});
  }catch(Exception e){fail();}
 }
 private String conversationPreview(JSONObject thread){
  String draft=thread.optString("draft");if(!draft.trim().isEmpty())return "Draft: "+draft;
  JSONArray messages=thread.optJSONArray("messages"),images=thread.optJSONArray("images");int count=messages==null?0:messages.length();
  JSONObject image=images==null||images.length()==0?null:images.optJSONObject(images.length()-1);
  if(image!=null&&image.optInt("after_message",count)>=count)return "Image: "+image.optString("prompt");
  JSONObject last=count==0?null:messages.optJSONObject(count-1);return last==null?"No messages yet":last.optString("content");
 }
 private void renderConversationRows(String query){
  if(conversationRows==null||data==null)return;conversationRows.removeAllViews();
  try{JSONArray threads=Conversations.ordered(data);String filter=query.trim().toLowerCase(java.util.Locale.ROOT);boolean pinnedHeading=false,otherHeading=false;int shown=0;
   for(int i=0;i<threads.length();i++){
    JSONObject thread=threads.getJSONObject(i);String title=thread.getString("title"),preview=conversationPreview(thread),id=thread.getString("id");
    if(!filter.isEmpty()&&!(title+" "+preview).toLowerCase(java.util.Locale.ROOT).contains(filter))continue;
    boolean pinned=thread.optBoolean("pinned"),active=id.equals(data.optString("active_thread"));
    if(pinned&&!pinnedHeading){conversationRows.addView(text("Pinned",13,0xffb6cbb0));space(conversationRows,8);pinnedHeading=true;}
    if(!pinned&&!otherHeading){if(pinnedHeading)space(conversationRows,8);conversationRows.addView(text("Conversations",13,0xffb6cbb0));space(conversationRows,8);otherHeading=true;}
    LinearLayout row=new LinearLayout(this);row.setGravity(Gravity.CENTER_VERTICAL);row.setPadding(dp(14),dp(10),dp(6),dp(10));row.setBackground(surface(active?0xff294033:0xff21362b,16));
    LinearLayout body=new LinearLayout(this);body.setOrientation(LinearLayout.VERTICAL);body.setPadding(0,dp(4),dp(8),dp(4));body.setClickable(true);body.setFocusable(true);body.setContentDescription("Open conversation "+title);body.setOnClickListener(v->selectConversation(id));
    TextView name=text((pinned?"📌  ":"")+title,17,0xffe9ede4);name.setTypeface(null,android.graphics.Typeface.BOLD);name.setMaxLines(1);name.setEllipsize(android.text.TextUtils.TruncateAt.END);body.addView(name);space(body,4);
    TextView snippet=text(preview,14,0xffcad9c5);snippet.setMaxLines(2);snippet.setEllipsize(android.text.TextUtils.TruncateAt.END);body.addView(snippet);
    int pendingCount=Requests.count(data,id);String detail=(pendingCount>0?pendingCount+" request"+(pendingCount==1?"":"s")+" pending · ":"")+(active?"Current thread":"")+(!thread.optString("instructions").isEmpty()?(active?" · ":"")+"Instructions set":"");if(!detail.isEmpty()){space(body,4);body.addView(text(detail,11,0xffb6cbb0));}
    row.addView(body,new LinearLayout.LayoutParams(0,-2,1));Button options=action("⋮",()->{},false);options.setContentDescription("Settings for "+title);options.setOnClickListener(v->{PopupMenu m=new PopupMenu(this,v);m.getMenu().add("Conversation settings");m.getMenu().add(thread.optBoolean("pinned")?"Unpin":"Pin");m.setOnMenuItemClickListener(item->{if(item.getTitle().toString().equals("Conversation settings"))editConversation(thread);else try{thread.put("pinned",!thread.optBoolean("pinned"));save();renderConversationRows(query);}catch(Exception e){fail();}return true;});m.show();});row.addView(options,new LinearLayout.LayoutParams(dp(44),dp(44)));
    LinearLayout.LayoutParams layout=new LinearLayout.LayoutParams(-1,-2);layout.bottomMargin=dp(10);conversationRows.addView(row,layout);shown++;
   }
   if(shown==0){space(conversationRows,24);conversationRows.addView(text("No conversations match your search.",16,0xffcad9c5));}
  }catch(Exception e){fail();}
 }
 private void renameConversation(){try{editConversation(Conversations.current(data));}catch(Exception e){fail();}}
 private void editConversation(JSONObject thread){try{
  LinearLayout form=new LinearLayout(this);form.setOrientation(LinearLayout.VERTICAL);form.setPadding(dp(20),dp(8),dp(20),dp(8));
  form.addView(text("Name",14,0xffcad9c5));EditText name=input("Conversation name",false);name.setSingleLine(true);name.setText(thread.getString("title"));name.setContentDescription("Conversation name");form.addView(name,new LinearLayout.LayoutParams(-1,-2));space(form,14);
  form.addView(text("Conversation instructions",14,0xffcad9c5));EditText instructions=input("How should the assistant respond in this thread?",false);instructions.setMinLines(4);instructions.setMaxLines(8);instructions.setText(thread.optString("instructions"));instructions.setContentDescription("Conversation instructions");form.addView(instructions,new LinearLayout.LayoutParams(-1,-2));space(form,8);
  form.addView(text("Applied to new chat and image requests in this thread. Saved requests keep the instructions they started with.",13,0xffcad9c5));CheckBox pinned=new CheckBox(this);pinned.setText("Pin conversation to top");pinned.setContentDescription("Pin conversation");pinned.setTextColor(0xffe9ede4);pinned.setChecked(thread.optBoolean("pinned"));pinned.setMinHeight(dp(48));form.addView(pinned);
  ScrollView scroll=new ScrollView(this);scroll.addView(form);AlertDialog dialog=new AlertDialog.Builder(this).setTitle("Conversation settings").setView(scroll).setPositiveButton("Save",null).setNegativeButton("Cancel",null).create();
  threadDialog=dialog;dialog.setOnDismissListener(d->{if(threadDialog==dialog)threadDialog=null;});dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{String title=name.getText().toString().trim();if(title.isEmpty()){name.setError("Enter a name");return;}try{thread.put("title",title).put("instructions",instructions.getText().toString().trim()).put("pinned",pinned.isChecked());save();dialog.dismiss();if(conversationsScreen){renderConversationRows("");}else{screenTitle.setText(threadLabel());}status.setText("Thread settings saved · applied to new requests.");}catch(Exception e){fail();}}));dialog.show();
 }catch(Exception e){fail();}}
 private void responseInfo(JSONObject message){try{
  JSONObject info=message.optJSONObject("info");LinearLayout content=new LinearLayout(this);content.setOrientation(LinearLayout.VERTICAL);content.setPadding(dp(20),dp(8),dp(20),dp(12));
  TextView details=text(ResponseInfo.summary(message),14,0xffe9ede4);details.setTextIsSelectable(true);content.addView(details);space(content,12);
  JSONArray context=info==null?null:info.optJSONArray("context");
  if(context!=null){Button expand=action("Show sent context · "+context.length()+" messages",()->{},false);content.addView(expand,new LinearLayout.LayoutParams(-1,-2));TextView messages=text(ResponseInfo.contextText(context),14,0xffe9ede4);messages.setTextIsSelectable(true);messages.setVisibility(View.GONE);content.addView(messages);expand.setOnClickListener(v->{boolean show=messages.getVisibility()!=View.VISIBLE;messages.setVisibility(show?View.VISIBLE:View.GONE);expand.setText(show?"Hide sent context":"Show sent context · "+context.length()+" messages");});space(content,8);content.addView(text("These are the instructions and messages Biome sent for this response. LM Studio's internal chat template and any internal truncation are not captured.",12,0xffcad9c5));}
  ScrollView scroll=new ScrollView(this);scroll.addView(content);AlertDialog dialog=new AlertDialog.Builder(this).setTitle("Response info").setView(scroll).setPositiveButton("Close",null).create();infoDialog=dialog;dialog.setOnDismissListener(d->{if(infoDialog==dialog)infoDialog=null;});dialog.show();dialog.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
 }catch(Exception e){status.setText("Response details are unavailable.");}}
 private void deleteMessage(JSONObject message){try{
  JSONArray messages=data.getJSONArray("messages");for(int i=0;i<messages.length();i++)if(messages.optJSONObject(i)==message){Conversations.deleteMessage(data,i);save();render(false);return;}
 }catch(Exception e){fail();}}
 private void confirmDeleteMessage(JSONObject message){new AlertDialog.Builder(this).setTitle("Delete message?").setMessage("Remove this message from this conversation and future chat context? Saved response-context snapshots in this thread will also be removed. A request already sent may still include it.").setPositiveButton("Delete",(d,w)->deleteMessage(message)).setNegativeButton("Cancel",null).show();}
 private void confirmDeleteImage(JSONObject entry,int index){new AlertDialog.Builder(this).setTitle(index<0?"Delete image result?":"Delete image?").setMessage("Remove it from this conversation's encrypted history on this phone?").setPositiveButton("Delete",(d,w)->{try{Conversations.deleteImage(data,entry,index);save();render(false);}catch(Exception e){fail();}}).setNegativeButton("Cancel",null).show();}
 private void locked(){connectionScreen=false;conversationsScreen=false;conversationRows=null;base("Biome Chat");status.setText("Private conversations on your devices");formArea();space(root,32);note(vault.exists()?"Welcome back":"Your private space",vault.exists()?"Unlock to continue your conversations.":"Choose a passphrase to protect your conversations on this phone. It cannot be recovered.");
  EditText password=field("Passphrase",true);EditText confirm=vault.exists()?null:field("Confirm passphrase",true);
  button(vault.exists()?"Unlock":"Create encrypted vault",()->{
   char[] chars=new char[password.length()];password.getText().getChars(0,password.length(),chars,0);
   try{
    if(!vault.exists()){
     if(chars.length<12||!password.getText().toString().equals(confirm.getText().toString())){status.setText("Use at least 12 characters and matching passphrases.");return;}
     salt=Crypto.random(32);vaultKey=Crypto.derive(chars,salt);data=new JSONObject().put("messages",new JSONArray());save();
    }else{byte[][] keys=new byte[2][];data=vault.unlock(chars,keys);vaultKey=keys[0];salt=keys[1];}
    password.setText("");if(confirm!=null)confirm.setText("");if(returnToConnection){returnToConnection=false;settings();}else chooseConversation();resume();
   }catch(Exception e){Crypto.wipe(vaultKey);vaultKey=null;data=null;status.setText("Could not unlock vault. Check your passphrase.");}
   finally{Arrays.fill(chars,'\0');}
  });
 }
 private void clearBitmaps(){if(imageHistory!=null)imageHistory.removeAllViews();for(android.graphics.Bitmap b:bitmaps)b.recycle();bitmaps.clear();}
 private void lock(){if(infoDialog!=null){infoDialog.dismiss();infoDialog=null;}if(threadDialog!=null){threadDialog.dismiss();threadDialog=null;}if(modelDialog!=null){modelDialog.dismiss();modelDialog=null;}if(data!=null&&vaultKey!=null&&composer!=null)try{data.put("draft",composer.getText().toString());save();}catch(Exception ignored){}if(messageList!=null)messageList.removeAllViews();messageList=null;clearBitmaps();imageHistory=null;epoch++;recoveryHandler.removeCallbacksAndMessages(null);for(Biome client:requestClients.values())client.cancel();for(Future<?> job:requestJobs.values())job.cancel(true);requestClients.clear();requestJobs.clear();biome.cancel();if(transport!=null)transport.cancel(true);busy=false;if(composer!=null)composer.setText("");if(history!=null)history.setText("");data=null;Crypto.wipe(vaultKey);vaultKey=null;Crypto.wipe(salt);salt=null;composer=null;history=null;if(!isFinishing())locked();}
 private void save()throws Exception{vault.save(Conversations.snapshot(data),vaultKey,salt);}
 private void fail(){busy=false;if(status!=null)status.setText(data!=null&&data.has("pending")?"Connection interrupted. Your request is saved and will reconnect automatically.":"Could not connect. Check Connection and try again.");updateControls();}
 private void chat(){
  connectionScreen=false;conversationsScreen=false;conversationRows=null;
  try{Conversations.ensure(data);}catch(Exception e){fail();return;}
  try{base(threadLabel());}catch(Exception e){fail();return;}
  threadButton=action("‹",this::chooseConversation,false);threadButton.setContentDescription("All conversations");toolbar.addView(threadButton,0,new LinearLayout.LayoutParams(dp(44),dp(44)));
  Button more=action("⋮",()->{},false);more.setOnClickListener(this::menu);more.setContentDescription("Conversation options");toolbar.addView(more,new LinearLayout.LayoutParams(dp(48),dp(44)));
  boolean connected=data.has("pairing")&&data.has("oauth");
  status.setText(connected?"Ready · Encrypted on this phone":"Connect your Mac to start");
  if(pairingUri!=null){Uri uri=pairingUri;pairingUri=null;try{try(java.io.InputStream in=getContentResolver().openInputStream(uri)){java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream();byte[] buf=new byte[4096];int n;while((n=in.read(buf))!=-1){if(out.size()+n>4096)throw new Exception();out.write(buf,0,n);}savePairing(new String(out.toByteArray(),StandardCharsets.UTF_8));Toast.makeText(this,"Mac pairing imported",Toast.LENGTH_SHORT).show();}}catch(Exception e){status.setText("Pairing import failed. Open Connection to try again.");}}

  modelButton=action("Model · "+modelName(data.optString("selected_model")),this::chooseModel,false);modelButton.setMaxLines(1);modelButton.setHorizontallyScrolling(false);modelButton.setGravity(Gravity.LEFT|Gravity.CENTER_VERTICAL);modelButton.setEllipsize(android.text.TextUtils.TruncateAt.END);modelButton.setContentDescription("Choose chat model");root.addView(modelButton,new LinearLayout.LayoutParams(-1,dp(44)));space(root,8);
  if(!connected)button("Set up connection",this::settings);
  chatScroll=new ScrollView(this);chatScroll.setFillViewport(true);chatScroll.setClipToPadding(false);
  LinearLayout content=new LinearLayout(this);content.setOrientation(LinearLayout.VERTICAL);
  messageList=new LinearLayout(this);messageList.setOrientation(LinearLayout.VERTICAL);content.addView(messageList);
  imageHistory=new LinearLayout(this);imageHistory.setOrientation(LinearLayout.VERTICAL);chatScroll.addView(content);root.addView(chatScroll,new LinearLayout.LayoutParams(-1,0,1));
  retryButton=action("Pending requests",this::pendingRequests,false);retryButton.setVisibility(View.GONE);root.addView(retryButton,new LinearLayout.LayoutParams(-1,dp(44)));
  LinearLayout modes=new LinearLayout(this);modes.setPadding(0,dp(8),0,dp(8));
  chatModeButton=action("Chat",()->switchMode(false),false);imageModeButton=action("Image",()->switchMode(true),false);
  LinearLayout.LayoutParams chip=new LinearLayout.LayoutParams(dp(92),dp(40));chip.rightMargin=dp(8);modes.addView(chatModeButton,chip);modes.addView(imageModeButton,new LinearLayout.LayoutParams(dp(92),dp(40)));root.addView(modes);
  LinearLayout dock=new LinearLayout(this);dock.setGravity(Gravity.BOTTOM);
  composer=input("Message…",false);composer.setMinLines(1);composer.setMaxLines(5);composer.setMinHeight(dp(52));composer.setContentDescription("Message or image prompt");
  LinearLayout.LayoutParams edit=new LinearLayout.LayoutParams(0,-2,1);edit.rightMargin=dp(8);dock.addView(composer,edit);
  sendButton=action("↑",()->send(imageMode),true);sendButton.setTextSize(24);dock.addView(sendButton,new LinearLayout.LayoutParams(dp(52),dp(52)));root.addView(dock);
  composer.setText(data.optString("draft"));composer.addTextChangedListener(new android.text.TextWatcher(){public void beforeTextChanged(CharSequence s,int start,int count,int after){}public void onTextChanged(CharSequence s,int start,int before,int count){updateControls();}public void afterTextChanged(android.text.Editable e){}});
  switchMode(imageMode);render();updateControls();
  showRequestStatus();
 }
 private void render(){render(true);}
 private void render(boolean bottom){
  if(messageList==null)return;final int previous=chatScroll==null?0:chatScroll.getScrollY();messageList.removeAllViews();clearBitmaps();
  try{if(Timeline.pinLegacyImages(data))save();}catch(Exception e){status.setText("Could not save image order.");}
  JSONArray events=Timeline.items(data);
  if(events.length()==0){LinearLayout empty=new LinearLayout(this);empty.setOrientation(LinearLayout.VERTICAL);empty.setGravity(Gravity.CENTER);empty.setPadding(dp(24),dp(40),dp(24),dp(32));
   TextView title=text("Start a conversation",22,0xffe9ede4);title.setTypeface(null,android.graphics.Typeface.BOLD);title.setGravity(Gravity.CENTER);empty.addView(title);space(empty,10);TextView hint=text("Choose a model, then write a message.\nSwitch to Image to create something visual.",15,0xffcad9c5);hint.setGravity(Gravity.CENTER);empty.addView(hint);messageList.addView(empty);
  }else for(int i=0;i<events.length();i++){
   JSONObject event=events.optJSONObject(i),message=event.optJSONObject("value");if(message==null)continue;
   if(event.optString("kind").equals("image")){renderImage(message);continue;}
   boolean mine=message.optString("role").equals("user");
   LinearLayout row=new LinearLayout(this);row.setGravity(mine?Gravity.RIGHT:Gravity.LEFT);row.setPadding(mine?dp(30):0,dp(4),mine?0:dp(24),dp(10));
   LinearLayout bubble=new LinearLayout(this);bubble.setOrientation(LinearLayout.VERTICAL);bubble.setPadding(dp(14),dp(11),dp(14),dp(12));bubble.setBackground(surface(mine?0xff36533e:0xff21362b,18));
   TextView who=text(mine?"You":(message.optString("model").isEmpty()?"Assistant":modelName(message.optString("model"))),11,0xffb6cbb0);who.setTypeface(null,android.graphics.Typeface.BOLD);LinearLayout header=new LinearLayout(this);header.setGravity(Gravity.CENTER_VERTICAL);header.addView(who,new LinearLayout.LayoutParams(0,-2,1));if(!mine){Button info=action("ⓘ",()->responseInfo(message),false);info.setContentDescription("Response info");header.addView(info,new LinearLayout.LayoutParams(dp(44),dp(44)));}Button options=action("⋮",()->confirmDeleteMessage(message),false);options.setContentDescription("Delete message");header.addView(options,new LinearLayout.LayoutParams(dp(44),dp(44)));bubble.addView(header);bubble.setOnLongClickListener(v->{confirmDeleteMessage(message);return true;});space(bubble,4);bubble.addView(text(message.optString("content"),16,0xffe9ede4));row.addView(bubble,new LinearLayout.LayoutParams(-1,-2));messageList.addView(row);
  }
  if(chatScroll!=null)chatScroll.post(()->{if(bottom)chatScroll.fullScroll(View.FOCUS_DOWN);else chatScroll.scrollTo(0,previous);});
 }
 private void renderImage(JSONObject entry){
  try{
   LinearLayout group=new LinearLayout(this);group.setOrientation(LinearLayout.VERTICAL);group.setPadding(dp(12),dp(12),dp(12),dp(12));group.setBackground(surface(0xff21362b,14));
   TextView title=text(entry.getString("prompt"),16,0xffe9ede4);group.addView(title);
   JSONArray images=entry.getJSONArray("images");boolean expanded=entry.optBoolean("expanded",false);
   LinearLayout controls=new LinearLayout(this);Button toggle=action((expanded?"Hide":"Show")+(images.length()==1?" image":" images ("+images.length()+")"),()->{try{entry.put("expanded",!entry.optBoolean("expanded",false));save();render(false);}catch(Exception e){fail();}},false);toggle.setContentDescription(expanded?"Collapse image":"Expand image");controls.addView(toggle,new LinearLayout.LayoutParams(0,dp(44),1));Button remove=action("Delete",()->confirmDeleteImage(entry,-1),false);remove.setContentDescription("Delete image result");controls.addView(remove,new LinearLayout.LayoutParams(dp(80),dp(44)));group.addView(controls);
   if(expanded)for(int j=0;j<images.length();j++){
    byte[] png=Vault.unb64(images.getJSONObject(j).getString("data"));
    android.graphics.Bitmap bitmap;try{bitmap=android.graphics.BitmapFactory.decodeByteArray(png,0,png.length);}finally{Crypto.wipe(png);}
    if(bitmap==null)throw new Exception();bitmaps.add(bitmap);ImageView view=new ImageView(this);view.setAdjustViewBounds(true);view.setImageBitmap(bitmap);view.setContentDescription("Generated image "+(j+1));group.addView(view,new LinearLayout.LayoutParams(-1,-2));
    final int imageIndex=j;view.setOnLongClickListener(v->{confirmDeleteImage(entry,imageIndex);return true;});
    if(images.length()>1){Button delete=action("Delete image "+(j+1),()->confirmDeleteImage(entry,imageIndex),false);group.addView(delete);}
   }
   LinearLayout.LayoutParams layout=new LinearLayout.LayoutParams(-1,-2);layout.topMargin=dp(6);layout.bottomMargin=dp(10);messageList.addView(group,layout);
  }catch(Exception e){status.setText("An encrypted image could not be displayed.");}
 }

 private boolean parkImage(){return parkPending();}
 private boolean parkPending(){try{Requests.migrate(data);save();updateControls();return true;}catch(Exception e){fail();return false;}}
 private void pendingRequests(){
  if(data==null)return;resume();try{JSONArray saved=Requests.all(data);String[] labels=new String[saved.length()];
   for(int i=0;i<labels.length;i++){JSONObject p=saved.getJSONObject(i);labels[i]=requestThreadName(p)+" · "+(p.optBoolean("image")?"Image":"Chat")+" · "+p.optString("status",p.optBoolean("terminal_failed")?"Failed":"Reconnecting…");}
   new AlertDialog.Builder(this).setTitle("Requests continue automatically").setItems(labels,(d,w)->{try{JSONObject p=saved.getJSONObject(w);if(p.optBoolean("terminal_failed"))offerRestart(p);else selectConversation(p.getString("thread_id"));}catch(Exception e){fail();}}).setNegativeButton("Back",null).show();
  }catch(Exception e){fail();}
 }
 private void offerRestart(JSONObject old){new AlertDialog.Builder(this).setTitle("Start this request again?").setMessage("This operation failed. Starting again creates a new operation and may repeat work if its earlier result was lost.").setPositiveButton("Start new request",(d,w)->{try{
  saveDraft();Conversations.select(data,old.getString("thread_id"));JSONObject fresh=startSession(old.optBoolean("image"),old.optString("image_prompt"));
  for(String key:new String[]{"action","selected_model","request_messages","anchor_id","after_message"})if(old.has(key))fresh.put(key,old.get(key));
  Requests.remove(data,Requests.id(old));Requests.add(data,fresh);save();chat();resume();
 }catch(Exception e){fail();}}).setNegativeButton("Keep saved",null).show();}
 private String requestThreadName(JSONObject pending){try{return Conversations.find(data,pending.getString("thread_id")).getString("title");}catch(Exception e){return "Conversation";}}
 private void chooseModel(){
  if(!parkImage())return;
  if(busy)return;
  JSONArray models=Models.unique(data.optJSONArray("models"));if(models==null||models.length()==0){refreshModels();return;}
  String[] labels=new String[models.length()];int selected=-1;
  for(int i=0;i<labels.length;i++)try{JSONObject m=models.getJSONObject(i);labels[i]=m.getString("name")+(m.optBoolean("loaded")?" • loaded":"");if(m.getString("id").equals(data.optString("selected_model")))selected=i;}catch(Exception e){fail();return;}
  modelDialog=new AlertDialog.Builder(this).setTitle("Chat model").setSingleChoiceItems(labels,selected,(dialog,index)->{
   try{data.put("selected_model",models.getJSONObject(index).getString("id"));save();modelButton.setText("Model · "+models.getJSONObject(index).getString("name"));dialog.dismiss();status.setText("Model selected for your next message.");}catch(Exception e){fail();}
  }).setNeutralButton("Refresh",(d,w)->refreshModels()).setNegativeButton("Cancel",null).show();
 }
 private void refreshModels(){
  if(!parkImage())return;
  if(busy)return;
  try{if(!data.has("pairing")||!data.has("oauth")){settings();return;}
   Requests.add(data,startSession(false,"").put("action","models"));save();resume();
  }catch(Exception e){fail();}
 }
 private void settings(){
  if(busy){status.setText("Finish or lock the current request before opening Connection.");return;}
  if(composer!=null)try{data.put("draft",composer.getText().toString());save();}catch(Exception e){fail();return;}
  connectionScreen=true;conversationsScreen=false;base("Connection");status.setText("Two steps · your existing connection is kept");formArea();
  note("1 · Pair your Mac",data.has("pairing")?"✓ Mac identity imported":"Import the pairing JSON supplied by your Mac.");
  button(data.has("pairing")?"Replace Mac pairing file":"Import Mac pairing file",()->{Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT);i.setType("application/json");i.addCategory(Intent.CATEGORY_OPENABLE);startActivityForResult(i,41);});
  space(root,14);note("2 · Connect Biome",data.has("oauth")?"✓ Biome authorized":"Allow the app to send encrypted requests through Biome.");
  button(data.has("oauth")?"Reconnect Biome":"Authorize in browser",this::authorize);
  root.addView(text("After allowing access, copy the “Continue to your app” link. Return here, unlock, and paste it below.",14,0xffcad9c5));space(root,12);
  EditText callback=field("Authorization return link",false);callback.setMaxLines(3);
  button("Paste & finish connection",()->{if(callback.getText().toString().trim().isEmpty()){android.content.ClipboardManager c=(android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);if(c.hasPrimaryClip()&&c.getPrimaryClip().getItemCount()>0)callback.setText(c.getPrimaryClip().getItemAt(0).coerceToText(this));}finishAuthorization(callback.getText().toString().trim());});
  space(root,12);button("Back to chat",this::chat);
 }
 private void savePairing(String text)throws Exception{if(Requests.all(data).length()>0)throw new Exception();JSONObject p=new JSONObject(text);if(p.getInt("v")!=2||Vault.unb64(p.getString("server_identity")).length!=32||!p.getString("resource").startsWith("privatechat.mac."))throw new Exception();data.put("pairing",p);save();}
 @Override protected void onActivityResult(int request,int result,Intent intent){super.onActivityResult(request,result,intent);if(request==41&&result==RESULT_OK&&intent!=null){pairingUri=intent.getData();if(data!=null)chat();}}
 private void authorize(){
  if(busy)return;busy=true;final int g=epoch;
  executor.execute(()->{try{
   JSONObject reg=biome.json("/oauth/register",new JSONObject().put("client_name","Biome Private Chat Android").put("redirect_uris",new JSONArray().put(REDIRECT)).put("token_endpoint_auth_method","none"),null);
   String verifier=android.util.Base64.encodeToString(Crypto.random(48),android.util.Base64.URL_SAFE|android.util.Base64.NO_WRAP|android.util.Base64.NO_PADDING);
   String state=UUID.randomUUID().toString(),challenge=android.util.Base64.encodeToString(java.security.MessageDigest.getInstance("SHA-256").digest(verifier.getBytes(StandardCharsets.US_ASCII)),11);
   JSONObject pending=new JSONObject().put("client_id",reg.getString("client_id")).put("verifier",verifier).put("state",state);
   Uri url=Uri.parse(Biome.ORIGIN+"/oauth/authorize").buildUpon().appendQueryParameter("client_id",reg.getString("client_id")).appendQueryParameter("response_type","code").appendQueryParameter("redirect_uri",REDIRECT).appendQueryParameter("code_challenge",challenge).appendQueryParameter("code_challenge_method","S256").appendQueryParameter("state",state).appendQueryParameter("scope","biomerte offline_access").appendQueryParameter("resource",Biome.RESOURCE).build();
   runOnUiThread(()->{if(g!=epoch)return;try{data.put("oauth_pending",pending);save();busy=false;returnToConnection=true;startActivity(new Intent(Intent.ACTION_VIEW,url));}catch(Exception e){fail();}});
  }catch(Exception e){runOnUiThread(()->{if(g==epoch)fail();});}});
 }
 private void finishAuthorization(String callback){
  if(busy)return;try{
   Uri u=Uri.parse(callback);JSONObject p=data.getJSONObject("oauth_pending");
   if(!callback.startsWith(REDIRECT+"?")||!p.getString("state").equals(u.getQueryParameter("state"))||!Biome.ORIGIN.equals(u.getQueryParameter("iss"))||u.getQueryParameter("code")==null||u.getQueryParameter("error")!=null)throw new Exception();
   JSONObject form=new JSONObject().put("grant_type","authorization_code").put("client_id",p.getString("client_id")).put("redirect_uri",REDIRECT).put("code",u.getQueryParameter("code")).put("code_verifier",p.getString("verifier")).put("resource",Biome.RESOURCE);
   busy=true;int g=epoch;executor.execute(()->{try{JSONObject token=biome.form("/oauth/token",form);token.put("client_id",p.getString("client_id")).put("expires_at",System.currentTimeMillis()+token.getLong("expires_in")*1000-90000);
    runOnUiThread(()->{if(g!=epoch)return;try{data.put("oauth",token);data.remove("oauth_pending");save();busy=false;chat();resume();}catch(Exception e){fail();}});
   }catch(Exception e){runOnUiThread(()->{if(g==epoch)fail();});}});
  }catch(Exception e){status.setText("Callback did not match this connection attempt.");}
 }
 private JSONObject startSession(boolean image,String prompt)throws Exception{
  Conversations.ensure(data);
  WireCrypto wire=new WireCrypto(image);
  if(!data.has("phone_identity"))data.put("phone_identity",Vault.b64(Crypto.random(32)));
  byte[] identity=Vault.unb64(data.getString("phone_identity")),ephemeral=Crypto.random(32),nonce=Crypto.random(32);
  String sid=UUID.randomUUID().toString(),helloId=UUID.randomUUID().toString(),requestId=UUID.randomUUID().toString();
  try{
   byte[] pub=Crypto.edPublic(identity),eph=Crypto.xPublic(ephemeral),transcript=wire.clientTranscript(helloId,sid,pub,eph,nonce);
   JSONObject hello=new JSONObject().put("v",2).put("kind","hello").put("id",helloId).put("sid",sid).put("client_identity",Vault.b64(pub)).put("client_ephemeral",Vault.b64(eph)).put("client_nonce",Vault.b64(nonce)).put("signature",Vault.b64(Crypto.sign(identity,transcript)));
   return new JSONObject().put("thread_id",data.getString("active_thread")).put("request_messages",image?Conversations.imageContext(prompt,Conversations.instructions(data,data.getString("active_thread"))):Conversations.requestContext(data)).put("submitted_at",System.currentTimeMillis()).put("stage","hello").put("hello",hello).put("client_private",Vault.b64(ephemeral)).put("request_id",requestId).put("selected_model",data.optString("selected_model","")).put("image",image).put("image_prompt",image?prompt:"");
  }finally{Crypto.wipe(identity);Crypto.wipe(ephemeral);}
 }
 private JSONObject finishSession(JSONObject pending,JSONObject reply)throws Exception{
  JSONObject hello=pending.getJSONObject("hello"),pairing=data.getJSONObject("pairing");
  WireCrypto wire=new WireCrypto(pending.optBoolean("image"));
  if(reply.getInt("v")!=2||!reply.getString("kind").equals("hello")||!reply.getString("id").equals(hello.getString("id"))||!reply.getString("sid").equals(hello.getString("sid")))throw new Exception();
  byte[] identity=Vault.unb64(pairing.getString("server_identity")),client=wire.clientTranscript(hello.getString("id"),hello.getString("sid"),Vault.unb64(hello.getString("client_identity")),Vault.unb64(hello.getString("client_ephemeral")),Vault.unb64(hello.getString("client_nonce")));
  byte[] whole=wire.serverTranscript(client,Vault.unb64(reply.getString("server_ephemeral")),Vault.unb64(reply.getString("server_nonce")),identity);
  Crypto.verify(identity,whole,Vault.unb64(reply.getString("signature")));
  byte[] ephemeral=Vault.unb64(pending.getString("client_private")),shared=null,requestKey=null,responseKey=null,clear=null;
  try{
   shared=Crypto.shared(ephemeral,Vault.unb64(reply.getString("server_ephemeral")));byte[] hash=Crypto.hash(whole);
   requestKey=wire.traffic(shared,hash,"request");responseKey=wire.traffic(shared,hash,"response");
   JSONObject body=new JSONObject().put("messages",pending.optJSONArray("request_messages")!=null?pending.getJSONArray("request_messages"):pending.optBoolean("image")?Conversations.imageContext(pending.getString("image_prompt"),Conversations.instructions(data,pending.getString("thread_id"))):Conversations.withInstructions(Conversations.context(Conversations.find(data,pending.getString("thread_id")).getJSONArray("messages")),Conversations.instructions(data,pending.getString("thread_id"))));
   if(pending.optString("action").equals("models"))body=new JSONObject().put("action","models");
   else if(!pending.optBoolean("image")&&!pending.optString("selected_model").isEmpty())body.put("model",pending.getString("selected_model"));
   if(pairing.has("enrollment_token"))body.put("enrollment_token",pairing.getString("enrollment_token"));
   clear=body.toString().getBytes(StandardCharsets.UTF_8);byte[] nonce=Crypto.random(12);String sid=hello.getString("sid"),id=pending.getString("request_id");
   JSONObject envelope=new JSONObject().put("v",2).put("kind","request").put("id",id).put("sid",sid).put("nonce",Vault.b64(nonce)).put("ct",Vault.b64(Crypto.crypt(true,requestKey,nonce,wire.sessionAAD("request",sid,id,hash),clear)));
   if(envelope.toString().getBytes(StandardCharsets.UTF_8).length>60000)throw new Exception("Request too large");
   JSONObject next=new JSONObject().put("request_id",Requests.id(pending)).put("thread_id",pending.getString("thread_id")).put("stage","message").put("envelope",envelope).put("response_key",Vault.b64(responseKey)).put("transcript_hash",Vault.b64(hash)).put("action",pending.optString("action")).put("selected_model",pending.optString("selected_model")).put("image",pending.optBoolean("image")).put("image_prompt",pending.optString("image_prompt"));
   if(body.has("messages")&&!pending.optBoolean("context_redacted"))next.put("request_messages",new JSONArray(body.getJSONArray("messages").toString()));
   for(String field:new String[]{"anchor_id","after_message","submitted_at","context_redacted"})if(pending.has(field))next.put(field,pending.get(field));
   return next;
  }finally{Crypto.wipe(ephemeral);Crypto.wipe(shared);Crypto.wipe(requestKey);Crypto.wipe(responseKey);Crypto.wipe(clear);}
 }
 private void send(){send(false);}
 private void send(boolean image){
  if(!image&&!parkImage())return;
  if(busy)return;
  try{if(!data.has("pairing")||data.getJSONObject("pairing").optInt("v")!=2||!data.has("oauth")){settings();return;}String text=composer.getText().toString();if(text.trim().isEmpty())return;
   JSONArray messages=image?new JSONArray():data.getJSONArray("messages");String anchor=UUID.randomUUID().toString();messages.put(new JSONObject().put("role","user").put("content",text).put("id",anchor));
   if(image){JSONArray timeline=data.getJSONArray("messages");for(int i=0;i<timeline.length();i++){JSONObject m=timeline.getJSONObject(i);if(!m.has("id"))m.put("id",UUID.randomUUID().toString());}anchor=timeline.length()==0?"":timeline.getJSONObject(timeline.length()-1).getString("id");}
   JSONObject encrypted=startSession(image,text).put("anchor_id",anchor).put("after_message",data.getJSONArray("messages").length());
   if(new JSONObject().put("messages",encrypted.getJSONArray("request_messages")).toString().getBytes(StandardCharsets.UTF_8).length>44000){messages.remove(messages.length()-1);status.setText("Conversation and instructions exceed Biome request size. Shorten the instructions or start a new conversation.");return;}
   Requests.add(data,encrypted);data.remove("draft");save();composer.setText("");render();resume();
  }catch(Exception e){fail();}
 }
 static String modelError(String code){
  switch(code){
   case "selected_model_required":return "Choose a model before sending your message.";
   case "selected_model_unavailable":return "Selected model is unavailable. Refresh models and choose it again.";
   case "model_load_failed":return "Your Mac could not load the selected model. It may need more free memory. Choose a smaller model or retry.";
   case "model_response_mismatch":return "Your Mac answered with a different model. The reply was rejected; retry your selection.";
   default:return "Your selected model could not finish this request. Retry or choose another model.";
  }
 }
 static String imageProgressLabel(JSONObject value){
  String stage=value.optString("stage");switch(stage){
   case "preparing":return "Preparing image request…";
   case "queued":return "Image queued on your Mac…";
   case "loading_model":return "Loading image model…";
   case "loading_text_encoders":return "Loading text encoders…";
   case "encoding_prompt":return "Encoding image prompt…";
   case "sampling":int step=value.optInt("step",-1),total=value.optInt("total",0);return step>=0&&total>0&&step<=total?"Generating image · Step "+step+" of "+total:"Sampling image…";
   case "loading_decoder":return "Loading image decoder…";
   case "decoding":return "Decoding image…";
   case "delivery":return "Encrypting and delivering image…";
   default:return "Generating image on your Mac…";
  }
 }
 private void showImageProgress(JSONObject frame,JSONObject pending,int generation,long[] sequence){
  runOnUiThread(()->{if(generation!=epoch||data==null)return;byte[] key=null,clear=null;try{
   JSONObject request=pending.getJSONObject("envelope");String id=request.getString("id"),sid=request.getString("sid");
   if(frame.getInt("v")!=2||!frame.getString("kind").equals("progress")||!id.equals(frame.getString("id"))||!sid.equals(frame.getString("sid")))return;
   key=Vault.unb64(pending.getString("response_key"));clear=Crypto.crypt(false,key,Vault.unb64(frame.getString("nonce")),new WireCrypto(true).sessionAAD("progress",sid,id,Vault.unb64(pending.getString("transcript_hash"))),Vault.unb64(frame.getString("ct")));
   JSONObject update=new JSONObject(new String(clear,StandardCharsets.UTF_8));long next=update.getLong("seq");if(next<=sequence[0])return;sequence[0]=next;pending.put("status",imageProgressLabel(update));
   if(!pending.has("thread_id")||pending.optString("thread_id").equals(data.optString("active_thread")))status.setText(imageProgressLabel(update));
  }catch(Exception ignored){}finally{Crypto.wipe(key);Crypto.wipe(clear);}});
 }
 private void showRequestStatus(){if(data==null)return;try{JSONArray requests=Requests.all(data);int count=0;String label="";for(int i=0;i<requests.length();i++){JSONObject p=requests.getJSONObject(i);if(p.optString("thread_id").equals(data.optString("active_thread"))){count++;label=p.optString("status",p.optBoolean("terminal_failed")?"Request failed — open Requests for details":"Reconnecting saved request…");}}if(count>0&&!conversationsScreen&&!connectionScreen)status.setText(label+(count>1?" · "+count+" requests":""));}catch(Exception ignored){}}
 // All vault mutations and completion routing run on the UI thread. Each network
 // exchange owns its connection; navigation never cancels another conversation.
 private void resume(){
  if(data==null||busy||!data.has("oauth")||!data.has("pairing"))return;
  try{Requests.migrate(data);save();JSONObject auth=data.getJSONObject("oauth");
   if(auth.optBoolean("refresh_pending")){status.setText("Connection refresh was interrupted. Reconnect Biome.");return;}
   if(!auth.has("expires_at")||!auth.has("access_token"))return;
   if(System.currentTimeMillis()>=auth.getLong("expires_at")){refresh(auth);return;}
   JSONArray requests=Requests.all(data);for(int i=0;i<requests.length();i++){JSONObject p=requests.getJSONObject(i);String id=Requests.id(p);if(!p.optBoolean("terminal_failed")&&!requestClients.containsKey(id))watch(p,auth.getString("access_token"));}
   updateControls();showRequestStatus();
  }catch(Exception e){fail();}
 }
 private java.util.function.Supplier<Biome> clientFactory=Biome::new;
 Biome requestClient(){return clientFactory.get();}
 private void watch(JSONObject pending,String token)throws Exception{
  String id=Requests.id(pending),resource=data.getJSONObject("pairing").getString("resource");boolean image=pending.optBoolean("image"),hello=pending.getString("stage").equals("hello");
  if(image)resource=resource.replace("privatechat.mac.","image.mac.");final String target=resource;JSONObject outbound=pending.getJSONObject(hello?"hello":"envelope");
  final long[] progressSequence={0};int g=epoch;Biome client=requestClient();requestClients.put(id,client);pending.put("status",hello?"Connecting securely…":pending.optString("action").equals("models")?"Loading models…":image?"Generating image on your Mac…":"Waiting for your model…");
  Future<?> job=executor.submit(()->{try{JSONObject response=client.exchange(token,target,outbound,image,()->g==epoch&&!Thread.currentThread().isInterrupted(),frame->showImageProgress(frame,pending,g,progressSequence));
   runOnUiThread(()->{if(g!=epoch||data==null)return;try{
    if(hello){JSONObject next=finishSession(pending,response);Requests.replace(data,id,next);save();requestClients.remove(id);requestJobs.remove(id);resume();return;}
    WireCrypto wire=new WireCrypto(image);String sid=outbound.getString("sid"),oid=outbound.getString("id");
    if(response.getInt("v")!=2||!response.getString("kind").equals("response")||!oid.equals(response.getString("id"))||!sid.equals(response.getString("sid")))throw new Exception();
    byte[] key=Vault.unb64(pending.getString("response_key")),clear=null;JSONObject reply;
    try{clear=Crypto.crypt(false,key,Vault.unb64(response.getString("nonce")),wire.sessionAAD("response",sid,oid,Vault.unb64(pending.getString("transcript_hash"))),Vault.unb64(response.getString("ct")));reply=new JSONObject(new String(clear,StandardCharsets.UTF_8));}finally{Crypto.wipe(key);Crypto.wipe(clear);}
    if(image&&!reply.has("error")){JSONArray images=reply.getJSONArray("images");for(int j=0;j<images.length();j++){JSONObject item=images.getJSONObject(j);byte[] png=Vault.unb64(item.getString("data"));try{if(!Biome.hex(Crypto.hash(png)).equals(item.getString("sha256")))throw new Exception();}finally{Crypto.wipe(png);}}}
    if(reply.has("error")){pending.put("terminal_failed",true).put("status",modelError(reply.optString("error")));}
    else{Requests.complete(data,pending,reply);data.getJSONObject("pairing").remove("enrollment_token");}
    saveDraft();save();requestClients.remove(id);requestJobs.remove(id);
    if(conversationsScreen)renderConversationRows("");else if(!connectionScreen){saveDraft();render(false);status.setText("Ready · Encrypted history saved");}
    updateControls();showRequestStatus();resume();
   }catch(Exception e){requestInterrupted(pending,g,e);}});
  }catch(Exception e){runOnUiThread(()->requestInterrupted(pending,g,e));}});requestJobs.put(id,job);
 }
 private void requestInterrupted(JSONObject pending,int generation,Exception error){
  if(generation!=epoch||data==null)return;
  try{String id=Requests.id(pending);requestJobs.remove(id);
   if(error instanceof Biome.RequestFailed){requestClients.remove(id);pending.put("terminal_failed",true).put("status","Request failed — open Requests to start again");save();updateControls();showRequestStatus();return;}
   pending.put("status","Connection interrupted · reconnecting automatically…");showRequestStatus();
   recoveryHandler.postDelayed(()->{if(generation!=epoch||data==null)return;requestClients.remove(id);resume();},3000);
  }catch(Exception ignored){fail();}
 }
 private void refresh(JSONObject auth)throws Exception{
  auth.put("refresh_pending",true);save();int g=epoch;busy=true;
  JSONObject form=new JSONObject().put("grant_type","refresh_token").put("client_id",auth.getString("client_id")).put("refresh_token",auth.getString("refresh_token")).put("resource",Biome.RESOURCE);
  executor.execute(()->{try{JSONObject token=biome.form("/oauth/token",form);token.put("client_id",form.getString("client_id")).put("expires_at",System.currentTimeMillis()+token.getLong("expires_in")*1000-90000);
   runOnUiThread(()->{if(g!=epoch)return;try{data.put("oauth",token);save();busy=false;resume();}catch(Exception e){fail();}});
  }catch(Exception e){runOnUiThread(()->{if(g==epoch)fail();});}});
 }
}
