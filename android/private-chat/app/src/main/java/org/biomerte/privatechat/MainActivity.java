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
 private void savePairing(String text)throws Exception{if(data.has("pending"))throw new Exception();JSONObject p=new JSONObject(text);if(p.getInt("v")!=2||Vault.unb64(p.getString("server_identity")).length!=32||!p.getString("resource").startsWith("privatechat.mac."))throw new Exception();data.put("pairing",p);save();}
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
 private JSONObject startSession()throws Exception{
  if(!data.has("phone_identity"))data.put("phone_identity",Vault.b64(Crypto.random(32)));
  byte[] identity=Vault.unb64(data.getString("phone_identity")),ephemeral=Crypto.random(32),nonce=Crypto.random(32);
  String sid=UUID.randomUUID().toString(),helloId=UUID.randomUUID().toString(),requestId=UUID.randomUUID().toString();
  try{
   byte[] pub=Crypto.edPublic(identity),eph=Crypto.xPublic(ephemeral),transcript=Crypto.clientTranscript(helloId,sid,pub,eph,nonce);
   JSONObject hello=new JSONObject().put("v",2).put("kind","hello").put("id",helloId).put("sid",sid).put("client_identity",Vault.b64(pub)).put("client_ephemeral",Vault.b64(eph)).put("client_nonce",Vault.b64(nonce)).put("signature",Vault.b64(Crypto.sign(identity,transcript)));
   return new JSONObject().put("stage","hello").put("hello",hello).put("client_private",Vault.b64(ephemeral)).put("request_id",requestId);
  }finally{Crypto.wipe(identity);Crypto.wipe(ephemeral);}
 }
 private void finishSession(JSONObject reply)throws Exception{
  JSONObject pending=data.getJSONObject("pending"),hello=pending.getJSONObject("hello"),pairing=data.getJSONObject("pairing");
  if(reply.getInt("v")!=2||!reply.getString("kind").equals("hello")||!reply.getString("id").equals(hello.getString("id"))||!reply.getString("sid").equals(hello.getString("sid")))throw new Exception();
  byte[] identity=Vault.unb64(pairing.getString("server_identity")),client=Crypto.clientTranscript(hello.getString("id"),hello.getString("sid"),Vault.unb64(hello.getString("client_identity")),Vault.unb64(hello.getString("client_ephemeral")),Vault.unb64(hello.getString("client_nonce")));
  byte[] whole=Crypto.serverTranscript(client,Vault.unb64(reply.getString("server_ephemeral")),Vault.unb64(reply.getString("server_nonce")),identity);
  Crypto.verify(identity,whole,Vault.unb64(reply.getString("signature")));
  byte[] ephemeral=Vault.unb64(pending.getString("client_private")),shared=null,requestKey=null,responseKey=null,clear=null;
  try{
   shared=Crypto.shared(ephemeral,Vault.unb64(reply.getString("server_ephemeral")));byte[] hash=Crypto.hash(whole);
   requestKey=Crypto.traffic(shared,hash,"request");responseKey=Crypto.traffic(shared,hash,"response");
   JSONObject body=new JSONObject().put("messages",data.getJSONArray("messages"));
   if(pairing.has("enrollment_token"))body.put("enrollment_token",pairing.getString("enrollment_token"));
   clear=body.toString().getBytes(StandardCharsets.UTF_8);byte[] nonce=Crypto.random(12);String sid=hello.getString("sid"),id=pending.getString("request_id");
   JSONObject envelope=new JSONObject().put("v",2).put("kind","request").put("id",id).put("sid",sid).put("nonce",Vault.b64(nonce)).put("ct",Vault.b64(Crypto.crypt(true,requestKey,nonce,Crypto.sessionAAD("request",sid,id,hash),clear)));
   if(envelope.toString().getBytes(StandardCharsets.UTF_8).length>60000)throw new Exception("Request too large");
   data.put("pending",new JSONObject().put("stage","message").put("envelope",envelope).put("response_key",Vault.b64(responseKey)).put("transcript_hash",Vault.b64(hash)));
   save();
  }finally{Crypto.wipe(ephemeral);Crypto.wipe(shared);Crypto.wipe(requestKey);Crypto.wipe(responseKey);Crypto.wipe(clear);}
 }
 private void send(){
  if(busy||data.has("pending")){status.setText("Resume the pending request first.");return;}
  try{if(!data.has("pairing")||data.getJSONObject("pairing").optInt("v")!=2||!data.has("oauth")){settings();return;}String text=composer.getText().toString();if(text.trim().isEmpty())return;
   JSONArray messages=data.getJSONArray("messages");messages.put(new JSONObject().put("role","user").put("content",text));
   JSONObject encrypted=startSession();
   if(new JSONObject().put("messages",messages).toString().getBytes(StandardCharsets.UTF_8).length>44000){messages.remove(messages.length()-1);status.setText("Conversation exceeds Biome request size. Start a new conversation.");return;}
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
   boolean isHello=pending.getString("stage").equals("hello");JSONObject outbound=pending.getJSONObject(isHello?"hello":"envelope");
   int g=epoch;busy=true;status.setText("Encrypted request in progress…");
   executor.execute(()->{try{JSONObject response=biome.exchange(token,resource,outbound,()->g==epoch&&!Thread.currentThread().isInterrupted());
    runOnUiThread(()->{if(g!=epoch)return;try{
     if(isHello){finishSession(response);busy=false;resume();return;}
     String id=outbound.getString("id"),sid=outbound.getString("sid");
     if(response.getInt("v")!=2||!response.getString("kind").equals("response")||!id.equals(response.getString("id"))||!sid.equals(response.getString("sid")))throw new Exception();
     byte[] key=Vault.unb64(pending.getString("response_key")),clear=null;
     JSONObject reply;try{clear=Crypto.crypt(false,key,Vault.unb64(response.getString("nonce")),Crypto.sessionAAD("response",sid,id,Vault.unb64(pending.getString("transcript_hash"))),Vault.unb64(response.getString("ct")));reply=new JSONObject(new String(clear,StandardCharsets.UTF_8));}finally{Crypto.wipe(key);Crypto.wipe(clear);}
     data.getJSONObject("pairing").remove("enrollment_token");
     if(reply.has("error")){status.setText("Mac could not complete private inference.");data.remove("pending");save();busy=false;return;}
     data.getJSONArray("messages").put(new JSONObject().put("role","assistant").put("content",reply.getString("text")));data.remove("pending");save();busy=false;render();status.setText("Encrypted history saved");
    }catch(Exception e){fail();}});
   }catch(Exception e){runOnUiThread(()->{if(g!=epoch)return;if(e instanceof Biome.RequestFailed){try{data.remove("pending");save();busy=false;status.setText("Mac could not finish this request. You can send a new message.");}catch(Exception ignored){fail();}}else fail();});}});
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
