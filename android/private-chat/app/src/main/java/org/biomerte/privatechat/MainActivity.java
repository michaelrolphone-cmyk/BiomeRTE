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
 private Button modelButton,sendButton,retryButton,chatModeButton,imageModeButton; private LinearLayout messageList,toolbar; private ScrollView chatScroll; private AlertDialog modelDialog; private boolean imageMode=false,connectionScreen=false,returnToConnection=false; private LinearLayout root,imageHistory; private final java.util.List<android.graphics.Bitmap> bitmaps=new java.util.ArrayList<>(); private TextView status,history; private EditText composer;
 private final ExecutorService executor=Executors.newSingleThreadExecutor(); private Future<?> transport;
 private final Biome biome=new Biome(); private volatile int epoch=0; private boolean busy=false; private Uri pairingUri;
 private static final String REDIRECT=Biome.ORIGIN+"/android/private-chat-callback";
 @Override public void onCreate(Bundle b){super.onCreate(b);getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);vault=new Vault(this);locked();}
 @Override protected void onStop(){
  if(data!=null&&vaultKey!=null&&composer!=null)try{data.put("draft",composer.getText().toString());save();}catch(Exception ignored){}
  super.onStop();
 }
 @Override protected void onResume(){super.onResume();if(returnToConnection&&data!=null){returnToConnection=false;settings();}}
 @Override public void onBackPressed(){if(connectionScreen&&data!=null){chat();}else{lock();super.onBackPressed();}}
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
  TextView heading=text(title,21,0xffe9ede4);heading.setTypeface(null,android.graphics.Typeface.BOLD);heading.setPadding(dp(10),0,0,0);toolbar.addView(heading,new LinearLayout.LayoutParams(0,dp(48),1));heading.setGravity(Gravity.CENTER_VERTICAL);root.addView(toolbar);
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
  if(data==null)return;boolean pending=data.has("pending");boolean imagePending=pending&&data.optJSONObject("pending").optBoolean("image");
  if(sendButton!=null)sendButton.setEnabled((!busy||imagePending)&&(!pending||imagePending)&&composer!=null&&!composer.getText().toString().trim().isEmpty());
  if(sendButton!=null)sendButton.setAlpha(sendButton.isEnabled()?1f:0.45f);
  if(retryButton!=null)retryButton.setVisibility(pending||data.optJSONArray("suspended_requests")!=null&&data.optJSONArray("suspended_requests").length()>0?View.VISIBLE:View.GONE);
  if(modelButton!=null)modelButton.setEnabled((!busy||imagePending)&&(!pending||imagePending));
 }
 private void switchMode(boolean image){imageMode=image;chatModeButton.setBackground(surface(image?0xff21362b:0xff406348,12));imageModeButton.setBackground(surface(image?0xff406348:0xff21362b,12));composer.setHint(image?"Describe an image…":"Message…");sendButton.setContentDescription(image?"Generate image":"Send message");modelButton.setVisibility(image?View.GONE:View.VISIBLE);}
 private void menu(View anchor){PopupMenu menu=new PopupMenu(this,anchor);menu.getMenu().add("Connection");menu.getMenu().add("New conversation");menu.getMenu().add("Lock");menu.setOnMenuItemClickListener(item->{switch(item.getTitle().toString()){case "Connection":settings();break;case "New conversation":clearConversation();break;case "Lock":lock();break;}return true;});menu.show();}
 private void clearConversation(){new AlertDialog.Builder(this).setTitle("New conversation?").setMessage("This deletes the current encrypted chat and image history on this phone.").setPositiveButton("Delete & start new",(d,w)->{try{if(data.has("pending")||busy){status.setText("Finish the pending request first.");return;}data.put("messages",new JSONArray());data.remove("images");save();render();status.setText("Ready for a new conversation.");}catch(Exception e){fail();}}).setNegativeButton("Keep conversation",null).show();}
 private void locked(){connectionScreen=false;base("Biome Chat");status.setText("Private conversations on your devices");formArea();space(root,32);note(vault.exists()?"Welcome back":"Your private space",vault.exists()?"Unlock to continue your conversations.":"Choose a passphrase to protect your conversations on this phone. It cannot be recovered.");
  EditText password=field("Passphrase",true);EditText confirm=vault.exists()?null:field("Confirm passphrase",true);
  button(vault.exists()?"Unlock":"Create encrypted vault",()->{
   char[] chars=new char[password.length()];password.getText().getChars(0,password.length(),chars,0);
   try{
    if(!vault.exists()){
     if(chars.length<12||!password.getText().toString().equals(confirm.getText().toString())){status.setText("Use at least 12 characters and matching passphrases.");return;}
     salt=Crypto.random(32);vaultKey=Crypto.derive(chars,salt);data=new JSONObject().put("messages",new JSONArray());save();
    }else{byte[][] keys=new byte[2][];data=vault.unlock(chars,keys);vaultKey=keys[0];salt=keys[1];}
    password.setText("");if(confirm!=null)confirm.setText("");if(returnToConnection){returnToConnection=false;settings();}else chat();
   }catch(Exception e){Crypto.wipe(vaultKey);vaultKey=null;data=null;status.setText("Could not unlock vault. Check your passphrase.");}
   finally{Arrays.fill(chars,'\0');}
  });
 }
 private void clearBitmaps(){if(imageHistory!=null)imageHistory.removeAllViews();for(android.graphics.Bitmap b:bitmaps)b.recycle();bitmaps.clear();}
 private void lock(){if(modelDialog!=null){modelDialog.dismiss();modelDialog=null;}if(data!=null&&vaultKey!=null&&composer!=null)try{data.put("draft",composer.getText().toString());save();}catch(Exception ignored){}if(messageList!=null)messageList.removeAllViews();messageList=null;clearBitmaps();imageHistory=null;epoch++;biome.cancel();if(transport!=null)transport.cancel(true);busy=false;if(composer!=null)composer.setText("");if(history!=null)history.setText("");data=null;Crypto.wipe(vaultKey);vaultKey=null;Crypto.wipe(salt);salt=null;composer=null;history=null;if(!isFinishing())locked();}
 private void save()throws Exception{vault.save(data,vaultKey,salt);}
 private void fail(){busy=false;if(status!=null)status.setText(data!=null&&data.has("pending")?"Connection interrupted. Your request is saved — tap Retry.":"Could not connect. Check Connection and try again.");updateControls();}
 private void chat(){
  connectionScreen=false;
  base("Biome Chat");
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
  if(data.has("pending"))status.setText("Request saved. Resume it or return to chat.");
 }
 private void render(){
  if(messageList==null)return;messageList.removeAllViews();clearBitmaps();
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
   TextView who=text(mine?"You":modelName(message.optString("model")),11,0xffb6cbb0);who.setTypeface(null,android.graphics.Typeface.BOLD);bubble.addView(who);space(bubble,4);bubble.addView(text(message.optString("content"),16,0xffe9ede4));row.addView(bubble,new LinearLayout.LayoutParams(-1,-2));messageList.addView(row);
  }
  if(chatScroll!=null)chatScroll.post(()->chatScroll.fullScroll(View.FOCUS_DOWN));
 }
 private void renderImage(JSONObject entry){
  try{
   LinearLayout group=new LinearLayout(this);group.setOrientation(LinearLayout.VERTICAL);group.setPadding(0,dp(8),0,dp(12));
   TextView title=text(entry.getString("prompt"),16,0xffe9ede4);group.addView(title);
   JSONArray images=entry.getJSONArray("images");
   for(int j=0;j<images.length();j++){
    byte[] png=Vault.unb64(images.getJSONObject(j).getString("data"));
    android.graphics.Bitmap bitmap;try{bitmap=android.graphics.BitmapFactory.decodeByteArray(png,0,png.length);}finally{Crypto.wipe(png);}
    if(bitmap==null)throw new Exception();bitmaps.add(bitmap);ImageView view=new ImageView(this);view.setAdjustViewBounds(true);view.setImageBitmap(bitmap);group.addView(view,new LinearLayout.LayoutParams(-1,-2));
   }
   messageList.addView(group);
  }catch(Exception e){status.setText("An encrypted image could not be displayed.");}
 }

 private boolean parkImage(){
  if(data==null)return false;
  JSONObject p=data.optJSONObject("pending");
  if(p!=null&&p.optBoolean("image"))return parkPending();
  return true;
 }
 private boolean parkPending(){
  if(data==null||!data.has("pending"))return true;
  try{
   JSONObject p=data.getJSONObject("pending");
   JSONArray saved=data.optJSONArray("suspended_requests");
   if(saved==null)saved=new JSONArray();
   saved.put(p);data.put("suspended_requests",saved);data.remove("pending");
   try{save();}catch(Exception e){saved.remove(saved.length()-1);data.put("pending",p);throw e;}
   epoch++;biome.cancel();if(transport!=null)transport.cancel(true);busy=false;
   updateControls();status.setText("Request saved for later. You can chat now.");return true;
  }catch(Exception e){fail();return false;}
 }
 private void pendingRequests(){
  if(data==null)return;
  if(data.has("pending")){
   new AlertDialog.Builder(this).setTitle("Pending request")
    .setItems(new String[]{"Resume request","Return to chat — keep request"},(d,w)->{
     if(w==0){if(!busy)resume();else status.setText("Still waiting. You can return to chat.");}
     else{if(parkPending())switchMode(false);}
    }).show();return;
  }
  JSONArray saved=data.optJSONArray("suspended_requests");
  if(saved==null||saved.length()==0){status.setText("No pending requests.");return;}
  String[] labels=new String[saved.length()];
  for(int i=0;i<labels.length;i++){JSONObject p=saved.optJSONObject(i);labels[i]=(p.optBoolean("image")?"Image":"Chat")+" request "+(i+1);}
  new AlertDialog.Builder(this).setTitle("Saved requests").setItems(labels,(d,w)->{
   if(busy){status.setText("Wait for this chat request, then resume.");return;}
   try{JSONObject p=saved.getJSONObject(w);data.put("pending",p);saved.remove(w);save();updateControls();resume();}catch(Exception e){fail();}
  }).setNegativeButton("Back",null).show();
 }

 private void chooseModel(){
  if(!parkImage())return;
  if(busy||data.has("pending")){status.setText("Resume the pending request before changing models.");return;}
  JSONArray models=data.optJSONArray("models");if(models==null||models.length()==0){refreshModels();return;}
  String[] labels=new String[models.length()];int selected=-1;
  for(int i=0;i<labels.length;i++)try{JSONObject m=models.getJSONObject(i);labels[i]=m.getString("name")+(m.optBoolean("loaded")?" • loaded":"");if(m.getString("id").equals(data.optString("selected_model")))selected=i;}catch(Exception e){fail();return;}
  modelDialog=new AlertDialog.Builder(this).setTitle("Chat model").setSingleChoiceItems(labels,selected,(dialog,index)->{
   try{data.put("selected_model",models.getJSONObject(index).getString("id"));save();modelButton.setText("Model · "+models.getJSONObject(index).getString("name"));dialog.dismiss();status.setText("Model selected for your next message.");}catch(Exception e){fail();}
  }).setNeutralButton("Refresh",(d,w)->refreshModels()).setNegativeButton("Cancel",null).show();
 }
 private void refreshModels(){
  if(!parkImage())return;
  if(busy||data.has("pending")){status.setText("Resume the pending request first.");return;}
  try{if(!data.has("pairing")||!data.has("oauth")){settings();return;}
   data.put("pending",startSession(false,"").put("action","models"));save();resume();
  }catch(Exception e){fail();}
 }
 private void settings(){
  if(busy){status.setText("Finish or lock the current request before opening Connection.");return;}
  if(composer!=null)try{data.put("draft",composer.getText().toString());save();}catch(Exception e){fail();return;}
  connectionScreen=true;base("Connection");status.setText("Two steps · your existing connection is kept");formArea();
  note("1 · Pair your Mac",data.has("pairing")?"✓ Mac identity imported":"Import the pairing JSON supplied by your Mac.");
  button(data.has("pairing")?"Replace Mac pairing file":"Import Mac pairing file",()->{Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT);i.setType("application/json");i.addCategory(Intent.CATEGORY_OPENABLE);startActivityForResult(i,41);});
  space(root,14);note("2 · Connect Biome",data.has("oauth")?"✓ Biome authorized":"Allow the app to send encrypted requests through Biome.");
  button(data.has("oauth")?"Reconnect Biome":"Authorize in browser",this::authorize);
  root.addView(text("After allowing access, copy the “Continue to your app” link. Return here, unlock, and paste it below.",14,0xffcad9c5));space(root,12);
  EditText callback=field("Authorization return link",false);callback.setMaxLines(3);
  button("Paste & finish connection",()->{if(callback.getText().toString().trim().isEmpty()){android.content.ClipboardManager c=(android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);if(c.hasPrimaryClip()&&c.getPrimaryClip().getItemCount()>0)callback.setText(c.getPrimaryClip().getItemAt(0).coerceToText(this));}finishAuthorization(callback.getText().toString().trim());});
  space(root,12);button("Back to chat",this::chat);
 }
 private void savePairing(String text)throws Exception{if(data.has("pending"))throw new Exception();JSONObject p=new JSONObject(text);if(p.getInt("v")!=2||Vault.unb64(p.getString("server_identity")).length!=32||!p.getString("resource").startsWith("privatechat.mac."))throw new Exception();data.put("pairing",p);save();}
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
    runOnUiThread(()->{if(g!=epoch)return;try{data.put("oauth",token);data.remove("oauth_pending");save();busy=false;chat();}catch(Exception e){fail();}});
   }catch(Exception e){runOnUiThread(()->{if(g==epoch)fail();});}});
  }catch(Exception e){status.setText("Callback did not match this connection attempt.");}
 }
 private JSONObject startSession(boolean image,String prompt)throws Exception{
  WireCrypto wire=new WireCrypto(image);
  if(!data.has("phone_identity"))data.put("phone_identity",Vault.b64(Crypto.random(32)));
  byte[] identity=Vault.unb64(data.getString("phone_identity")),ephemeral=Crypto.random(32),nonce=Crypto.random(32);
  String sid=UUID.randomUUID().toString(),helloId=UUID.randomUUID().toString(),requestId=UUID.randomUUID().toString();
  try{
   byte[] pub=Crypto.edPublic(identity),eph=Crypto.xPublic(ephemeral),transcript=wire.clientTranscript(helloId,sid,pub,eph,nonce);
   JSONObject hello=new JSONObject().put("v",2).put("kind","hello").put("id",helloId).put("sid",sid).put("client_identity",Vault.b64(pub)).put("client_ephemeral",Vault.b64(eph)).put("client_nonce",Vault.b64(nonce)).put("signature",Vault.b64(Crypto.sign(identity,transcript)));
   return new JSONObject().put("stage","hello").put("hello",hello).put("client_private",Vault.b64(ephemeral)).put("request_id",requestId).put("selected_model",data.optString("selected_model","")).put("image",image).put("image_prompt",image?prompt:"");
  }finally{Crypto.wipe(identity);Crypto.wipe(ephemeral);}
 }
 private void finishSession(JSONObject reply)throws Exception{
  JSONObject pending=data.getJSONObject("pending"),hello=pending.getJSONObject("hello"),pairing=data.getJSONObject("pairing");
  WireCrypto wire=new WireCrypto(pending.optBoolean("image"));
  if(reply.getInt("v")!=2||!reply.getString("kind").equals("hello")||!reply.getString("id").equals(hello.getString("id"))||!reply.getString("sid").equals(hello.getString("sid")))throw new Exception();
  byte[] identity=Vault.unb64(pairing.getString("server_identity")),client=wire.clientTranscript(hello.getString("id"),hello.getString("sid"),Vault.unb64(hello.getString("client_identity")),Vault.unb64(hello.getString("client_ephemeral")),Vault.unb64(hello.getString("client_nonce")));
  byte[] whole=wire.serverTranscript(client,Vault.unb64(reply.getString("server_ephemeral")),Vault.unb64(reply.getString("server_nonce")),identity);
  Crypto.verify(identity,whole,Vault.unb64(reply.getString("signature")));
  byte[] ephemeral=Vault.unb64(pending.getString("client_private")),shared=null,requestKey=null,responseKey=null,clear=null;
  try{
   shared=Crypto.shared(ephemeral,Vault.unb64(reply.getString("server_ephemeral")));byte[] hash=Crypto.hash(whole);
   requestKey=wire.traffic(shared,hash,"request");responseKey=wire.traffic(shared,hash,"response");
   JSONObject body=new JSONObject().put("messages",pending.optBoolean("image")?new JSONArray().put(new JSONObject().put("role","user").put("content",pending.getString("image_prompt"))):data.getJSONArray("messages"));
   if(pending.optString("action").equals("models"))body=new JSONObject().put("action","models");
   else if(!pending.optBoolean("image")&&!pending.optString("selected_model").isEmpty())body.put("model",pending.getString("selected_model"));
   if(pairing.has("enrollment_token"))body.put("enrollment_token",pairing.getString("enrollment_token"));
   clear=body.toString().getBytes(StandardCharsets.UTF_8);byte[] nonce=Crypto.random(12);String sid=hello.getString("sid"),id=pending.getString("request_id");
   JSONObject envelope=new JSONObject().put("v",2).put("kind","request").put("id",id).put("sid",sid).put("nonce",Vault.b64(nonce)).put("ct",Vault.b64(Crypto.crypt(true,requestKey,nonce,wire.sessionAAD("request",sid,id,hash),clear)));
   if(envelope.toString().getBytes(StandardCharsets.UTF_8).length>60000)throw new Exception("Request too large");
   data.put("pending",new JSONObject().put("stage","message").put("envelope",envelope).put("response_key",Vault.b64(responseKey)).put("transcript_hash",Vault.b64(hash)).put("action",pending.optString("action")).put("selected_model",pending.optString("selected_model")).put("image",pending.optBoolean("image")).put("image_prompt",pending.optString("image_prompt")));
   save();
  }finally{Crypto.wipe(ephemeral);Crypto.wipe(shared);Crypto.wipe(requestKey);Crypto.wipe(responseKey);Crypto.wipe(clear);}
 }
 private void send(){send(false);}
 private void send(boolean image){
  if(!image&&!parkImage())return;
  if(busy||data.has("pending")){status.setText("Resume the pending request first.");return;}
  try{if(!data.has("pairing")||data.getJSONObject("pairing").optInt("v")!=2||!data.has("oauth")){settings();return;}String text=composer.getText().toString();if(text.trim().isEmpty())return;
   JSONArray messages=image?new JSONArray():data.getJSONArray("messages");messages.put(new JSONObject().put("role","user").put("content",text));
   JSONObject encrypted=startSession(image,text);
   if(new JSONObject().put("messages",messages).toString().getBytes(StandardCharsets.UTF_8).length>44000){messages.remove(messages.length()-1);status.setText("Conversation exceeds Biome request size. Start a new conversation.");return;}
   data.put("pending",encrypted);data.remove("draft");save();composer.setText("");render();resume();
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
 private void resume(){
  if(busy||!data.has("pending"))return;
  try{
   JSONObject auth=data.getJSONObject("oauth");
   if(auth.optBoolean("refresh_pending")){status.setText("Connection refresh was interrupted. Reconnect Biome.");return;}
   if(System.currentTimeMillis()>=auth.getLong("expires_at")){refresh(auth);return;}
   String token=auth.getString("access_token"),resource=data.getJSONObject("pairing").getString("resource");JSONObject pending=new JSONObject(data.getJSONObject("pending").toString());
   boolean image=pending.optBoolean("image");if(image)resource=resource.replace("privatechat.mac.","image.mac.");final String targetResource=resource;
   boolean isHello=pending.getString("stage").equals("hello");JSONObject outbound=pending.getJSONObject(isHello?"hello":"envelope");
   int g=epoch;busy=true;status.setText(isHello?"Connecting securely…":pending.optString("action").equals("models")?"Loading models from your Mac…":image?"Generating image on your Mac…":"Waiting for your model…");updateControls();
   transport=executor.submit(()->{try{JSONObject response=biome.exchange(token,targetResource,outbound,image,()->g==epoch&&!Thread.currentThread().isInterrupted());
    runOnUiThread(()->{if(g!=epoch)return;try{
     if(isHello){finishSession(response);busy=false;resume();return;}
     WireCrypto wire=new WireCrypto(image);
     String id=outbound.getString("id"),sid=outbound.getString("sid");
     if(response.getInt("v")!=2||!response.getString("kind").equals("response")||!id.equals(response.getString("id"))||!sid.equals(response.getString("sid")))throw new Exception();
     byte[] key=Vault.unb64(pending.getString("response_key")),clear=null;
     JSONObject reply;try{clear=Crypto.crypt(false,key,Vault.unb64(response.getString("nonce")),wire.sessionAAD("response",sid,id,Vault.unb64(pending.getString("transcript_hash"))),Vault.unb64(response.getString("ct")));reply=new JSONObject(new String(clear,StandardCharsets.UTF_8));}finally{Crypto.wipe(key);Crypto.wipe(clear);}
     data.getJSONObject("pairing").remove("enrollment_token");
     if(reply.has("error")){status.setText(modelError(reply.optString("error")));data.remove("pending");save();busy=false;updateControls();return;}
     if(pending.optString("action").equals("models")){
      JSONArray models=reply.getJSONArray("models");data.put("models",models);data.remove("pending");save();busy=false;
      updateControls();if(models.length()==0)status.setText("No chat models available on your Mac.");else chooseModel();return;
     }
     if(image){
      JSONArray images=reply.getJSONArray("images");
      for(int i=0;i<images.length();i++){JSONObject item=images.getJSONObject(i);byte[] png=Vault.unb64(item.getString("data"));try{if(!Biome.hex(Crypto.hash(png)).equals(item.getString("sha256")))throw new Exception();}finally{Crypto.wipe(png);}}
      JSONArray entries=data.optJSONArray("images");if(entries==null){entries=new JSONArray();data.put("images",entries);}
      entries.put(new JSONObject().put("prompt",pending.getString("image_prompt")).put("images",images).put("after_message",data.getJSONArray("messages").length()));
     }else data.getJSONArray("messages").put(new JSONObject().put("role","assistant").put("content",reply.getString("text")).put("model",reply.optString("model",pending.optString("selected_model"))));data.remove("pending");save();busy=false;render();status.setText("Ready · Encrypted history saved");updateControls();
    }catch(Exception e){fail();}});
   }catch(Exception e){runOnUiThread(()->{if(g!=epoch)return;if(e instanceof Biome.RequestFailed){try{data.remove("pending");save();busy=false;status.setText("Your Mac could not finish. You can send a new message.");updateControls();}catch(Exception ignored){fail();}}else fail();});}});
  }catch(Exception e){fail();}
 }
 private void refresh(JSONObject auth)throws Exception{
  auth.put("refresh_pending",true);save();int g=epoch;busy=true;
  JSONObject form=new JSONObject().put("grant_type","refresh_token").put("client_id",auth.getString("client_id")).put("refresh_token",auth.getString("refresh_token")).put("resource",Biome.RESOURCE);
  executor.execute(()->{try{JSONObject token=biome.form("/oauth/token",form);token.put("client_id",form.getString("client_id")).put("expires_at",System.currentTimeMillis()+token.getLong("expires_in")*1000-90000);
   runOnUiThread(()->{if(g!=epoch)return;try{data.put("oauth",token);save();busy=false;resume();}catch(Exception e){fail();}});
  }catch(Exception e){runOnUiThread(()->{if(g==epoch)fail();});}});
 }
}
