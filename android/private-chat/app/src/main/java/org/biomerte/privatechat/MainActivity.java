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
 private LinearLayout root; private TextView status,history; private EditText composer;
 private final ExecutorService executor=Executors.newSingleThreadExecutor();
 private final Biome biome=new Biome(); private volatile int epoch=0; private boolean busy=false; private Uri pairingUri;
 private static final String REDIRECT=Biome.ORIGIN+"/android/private-chat-callback";
 @Override public void onCreate(Bundle b){super.onCreate(b);getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);vault=new Vault(this);locked();}
 @Override protected void onStop(){super.onStop();lock();}
 @Override protected void onDestroy(){lock();executor.shutdownNow();super.onDestroy();}
 private void base(String title){
  root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);root.setPadding(24,24,24,24);root.setBackgroundColor(0xff17231e);
  TextView heading=new TextView(this);heading.setText(title);heading.setTextSize(26);heading.setTextColor(0xffe9ede4);root.addView(heading);
  status=new TextView(this);status.setTextColor(0xffcad9c5);status.setPadding(0,12,0,12);root.addView(status);setContentView(root);
 }
 private Button button(String label,Runnable action){Button b=new Button(this);b.setText(label);b.setOnClickListener(v->action.run());root.addView(b);return b;}
 private EditText field(String hint,boolean secret){EditText e=new EditText(this);e.setHint(hint);e.setTextColor(0xffe9ede4);e.setHintTextColor(0xffa7b6a2);e.setSaveEnabled(false);
  e.setInputType(secret?InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD:InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_FLAG_MULTI_LINE|InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
  e.setImeOptions(EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING);root.addView(e);return e;}
 private void locked(){base("Biome Private Chat");status.setText(vault.exists()?"Enter your encryption passphrase.":"Create your encryption passphrase. It cannot be recovered.");
  EditText password=field("Passphrase",true);EditText confirm=vault.exists()?null:field("Confirm passphrase",true);
  button(vault.exists()?"Unlock":"Create encrypted vault",()->{
   char[] chars=new char[password.length()];password.getText().getChars(0,password.length(),chars,0);
   try{
    if(!vault.exists()){
     if(chars.length<12||!password.getText().toString().equals(confirm.getText().toString())){status.setText("Use at least 12 characters and matching passphrases.");return;}
     salt=Crypto.random(32);vaultKey=Crypto.derive(chars,salt);data=new JSONObject().put("messages",new JSONArray());save();
    }else{byte[][] keys=new byte[2][];data=vault.unlock(chars,keys);vaultKey=keys[0];salt=keys[1];}
    password.setText("");if(confirm!=null)confirm.setText("");chat();
   }catch(Exception e){Crypto.wipe(vaultKey);vaultKey=null;data=null;status.setText("Could not unlock vault. Check your passphrase.");}
   finally{Arrays.fill(chars,'\0');}
  });
 }
 private void lock(){epoch++;biome.cancel();busy=false;if(composer!=null)composer.setText("");if(history!=null)history.setText("");data=null;Crypto.wipe(vaultKey);vaultKey=null;Crypto.wipe(salt);salt=null;composer=null;history=null;if(!isFinishing())locked();}
 private void save()throws Exception{vault.save(data,vaultKey,salt);}
 private void fail(){if(status!=null)status.setText("Operation did not finish. Unlock and retry to resume the same encrypted request.");busy=false;}
 private void chat(){
  base("Biome Private Chat");status.setText("Encrypted on this phone • unlocked");
  ScrollView scroll=new ScrollView(this);history=new TextView(this);history.setTextColor(0xffe9ede4);history.setTextSize(17);history.setTextIsSelectable(false);history.setSaveEnabled(false);scroll.addView(history);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
  if(pairingUri!=null){Uri uri=pairingUri;pairingUri=null;try{try(java.io.InputStream in=getContentResolver().openInputStream(uri)){java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream();byte[] buf=new byte[4096];int n;while((n=in.read(buf))!=-1){if(out.size()+n>4096)throw new Exception();out.write(buf,0,n);}savePairing(new String(out.toByteArray(),StandardCharsets.UTF_8));}}catch(Exception e){status.setText("Could not import pairing file.");}}
  render();composer=field("Message",false);
  button("Send",this::send);button("Resume pending request",this::resume);
  button("Pair Mac / connection",this::settings);button("Lock",this::lock);
  button("Clear conversation",()->new AlertDialog.Builder(this).setMessage("Delete encrypted conversation history from this app?").setPositiveButton("Delete",(d,w)->{try{if(data.has("pending")){status.setText("Resolve the pending request first.");return;}data.put("messages",new JSONArray());save();render();}catch(Exception e){fail();}}).setNegativeButton("Cancel",null).show());
 }
 private void render(){StringBuilder s=new StringBuilder();JSONArray a=data.optJSONArray("messages");if(a!=null)for(int i=0;i<a.length();i++){JSONObject m=a.optJSONObject(i);s.append(m.optString("role").equals("user")?"You\n":"LM Studio\n").append(m.optString("content")).append("\n\n");}history.setText(s);}
 private void settings(){
  base("Private connection");status.setText("Import the pairing JSON directly from your Mac. Keep it private. Biome receives only encrypted chat.");
  button("Import Mac pairing file",()->{Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT);i.setType("application/json");i.addCategory(Intent.CATEGORY_OPENABLE);startActivityForResult(i,41);});
  button("Connect Biome",this::authorize);
  EditText callback=field("Paste Continue to your app link",false);
  button("Finish connection",()->finishAuthorization(callback.getText().toString()));
  button("Back",this::chat);
 }
 private void savePairing(String text)throws Exception{if(data.has("pending"))throw new Exception();JSONObject p=new JSONObject(text);if(p.getInt("v")!=1||Vault.unb64(p.getString("key")).length!=32||!p.getString("resource").startsWith("privatechat.mac."))throw new Exception();data.put("pairing",p);save();}
 @Override protected void onActivityResult(int request,int result,Intent intent){super.onActivityResult(request,result,intent);if(request==41&&result==RESULT_OK&&intent!=null)pairingUri=intent.getData();}
 private void authorize(){
  if(busy)return;busy=true;final int g=epoch;
  executor.execute(()->{try{
   JSONObject reg=biome.json("/oauth/register",new JSONObject().put("client_name","Biome Private Chat Android").put("redirect_uris",new JSONArray().put(REDIRECT)).put("token_endpoint_auth_method","none"),null);
   String verifier=android.util.Base64.encodeToString(Crypto.random(48),android.util.Base64.URL_SAFE|android.util.Base64.NO_WRAP|android.util.Base64.NO_PADDING);
   String state=UUID.randomUUID().toString(),challenge=android.util.Base64.encodeToString(java.security.MessageDigest.getInstance("SHA-256").digest(verifier.getBytes(StandardCharsets.US_ASCII)),11);
   JSONObject pending=new JSONObject().put("client_id",reg.getString("client_id")).put("verifier",verifier).put("state",state);
   Uri url=Uri.parse(Biome.ORIGIN+"/oauth/authorize").buildUpon().appendQueryParameter("client_id",reg.getString("client_id")).appendQueryParameter("response_type","code").appendQueryParameter("redirect_uri",REDIRECT).appendQueryParameter("code_challenge",challenge).appendQueryParameter("code_challenge_method","S256").appendQueryParameter("state",state).appendQueryParameter("scope","biomerte offline_access").appendQueryParameter("resource",Biome.RESOURCE).build();
   runOnUiThread(()->{if(g!=epoch)return;try{data.put("oauth_pending",pending);save();busy=false;startActivity(new Intent(Intent.ACTION_VIEW,url));}catch(Exception e){fail();}});
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
 private JSONObject seal(JSONObject plain)throws Exception{
  String id=UUID.randomUUID().toString();byte[] key=Crypto.direction(Vault.unb64(data.getJSONObject("pairing").getString("key")),"request"),nonce=Crypto.random(12),bytes=plain.toString().getBytes(StandardCharsets.UTF_8);
  try{return new JSONObject().put("v",1).put("id",id).put("nonce",Vault.b64(nonce)).put("ct",Vault.b64(Crypto.crypt(true,key,nonce,Crypto.aad("request",id),bytes)));}
  finally{Crypto.wipe(key);Crypto.wipe(bytes);}
 }
 private void send(){
  if(busy||data.has("pending")){status.setText("Resume the pending request first.");return;}
  try{if(!data.has("pairing")||!data.has("oauth")){settings();return;}String text=composer.getText().toString();if(text.trim().isEmpty())return;
   JSONArray messages=data.getJSONArray("messages");messages.put(new JSONObject().put("role","user").put("content",text));
   JSONObject encrypted=seal(new JSONObject().put("messages",messages));
   if(encrypted.toString().getBytes(StandardCharsets.UTF_8).length>60000){messages.remove(messages.length()-1);status.setText("Conversation exceeds the Biome request size. Start a new conversation.");return;}
   data.put("pending",encrypted);save();composer.setText("");render();resume();
  }catch(Exception e){fail();}
 }
 private void resume(){
  if(busy||!data.has("pending"))return;
  try{
   JSONObject auth=data.getJSONObject("oauth");
   if(auth.optBoolean("refresh_pending")){status.setText("Connection refresh was interrupted. Reconnect Biome.");return;}
   if(System.currentTimeMillis()>=auth.getLong("expires_at")){refresh(auth);return;}
   String token=auth.getString("access_token"),resource=data.getJSONObject("pairing").getString("resource");JSONObject pending=new JSONObject(data.getJSONObject("pending").toString());
   int g=epoch;busy=true;status.setText("Encrypted request in progress…");
   executor.execute(()->{try{JSONObject response=biome.exchange(token,resource,pending,()->g==epoch&&!Thread.currentThread().isInterrupted());
    runOnUiThread(()->{if(g!=epoch)return;try{
     String id=pending.getString("id");if(response.getInt("v")!=1||!id.equals(response.getString("id")))throw new Exception();
     byte[] key=Crypto.direction(Vault.unb64(data.getJSONObject("pairing").getString("key")),"response"),clear=null;
     JSONObject reply;try{clear=Crypto.crypt(false,key,Vault.unb64(response.getString("nonce")),Crypto.aad("response",id),Vault.unb64(response.getString("ct")));reply=new JSONObject(new String(clear,StandardCharsets.UTF_8));}finally{Crypto.wipe(key);Crypto.wipe(clear);}
     if(reply.has("error")){status.setText("Mac could not complete private inference.");data.remove("pending");save();busy=false;return;}
     data.getJSONArray("messages").put(new JSONObject().put("role","assistant").put("content",reply.getString("text")));data.remove("pending");save();busy=false;render();status.setText("Encrypted history saved");
    }catch(Exception e){fail();}});
   }catch(Exception e){runOnUiThread(()->{if(g==epoch)fail();});}});
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
