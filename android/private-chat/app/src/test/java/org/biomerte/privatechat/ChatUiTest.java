package org.biomerte.privatechat;
import android.graphics.*;
import android.view.*;
import android.widget.*;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import java.io.*;
import java.lang.reflect.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class)
@Config(sdk=28,qualifiers="w360dp-h640dp-xhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
public class ChatUiTest {
 MainActivity activity;
 Object get(String name)throws Exception{Field f=MainActivity.class.getDeclaredField(name);f.setAccessible(true);return f.get(activity);}
 void set(String name,Object value)throws Exception{Field f=MainActivity.class.getDeclaredField(name);f.setAccessible(true);f.set(activity,value);}
 void call(String name)throws Exception{Method m=MainActivity.class.getDeclaredMethod(name);m.setAccessible(true);m.invoke(activity);}
 @Before public void setup()throws Exception{
  activity=Robolectric.buildActivity(MainActivity.class).setup().get();
  JSONObject data=new JSONObject().put("pairing",new JSONObject()).put("oauth",new JSONObject()).put("messages",new JSONArray()
   .put(new JSONObject().put("role","user").put("content","Can we talk through an idea?"))
   .put(new JSONObject().put("role","assistant").put("model","Qwen3.5 9B").put("content","Of course. What are you working on?")));
  set("data",data);set("vaultKey",new byte[32]);set("salt",new byte[32]);call("chat");
 }
 View draw(int height,String name)throws Exception{
  View root=activity.getWindow().getDecorView();
  root.measure(View.MeasureSpec.makeMeasureSpec(720,View.MeasureSpec.EXACTLY),View.MeasureSpec.makeMeasureSpec(height,View.MeasureSpec.EXACTLY));root.layout(0,0,720,height);
  Bitmap b=Bitmap.createBitmap(720,height,Bitmap.Config.ARGB_8888);root.draw(new Canvas(b));
  File dir=new File("build/ui-previews");dir.mkdirs();try(FileOutputStream out=new FileOutputStream(new File(dir,name))){b.compress(Bitmap.CompressFormat.PNG,100,out);}b.recycle();return root;
 }
 @Test public void chatHasBubblesAndDockedComposer()throws Exception{
  draw(1280,"chat.png");LinearLayout list=(LinearLayout)get("messageList");assertEquals(2,list.getChildCount());
  EditText composer=(EditText)get("composer");composer.setText("Hello");assertTrue(((Button)get("sendButton")).isEnabled());
  assertEquals(View.GONE,((Button)get("retryButton")).getVisibility());
  assertTrue(composer.getHeight()>=96);assertTrue(((ScrollView)get("chatScroll")).getHeight()>300);
 }
 @Test public void keyboardSizeKeepsComposerAndModesVisible()throws Exception{
  draw(720,"compact.png");EditText composer=(EditText)get("composer");
  assertTrue(composer.getHeight()>=96);assertTrue(((ScrollView)get("chatScroll")).getHeight()>0);
  ((Button)get("imageModeButton")).performClick();assertEquals("Describe an image…",composer.getHint().toString());assertEquals(View.GONE,((Button)get("modelButton")).getVisibility());
  ((Button)get("chatModeButton")).performClick();assertEquals(View.VISIBLE,((Button)get("modelButton")).getVisibility());
 }
 @Test public void pendingRequestDisablesDuplicateSendAndExposesRetry()throws Exception{
  ((JSONObject)get("data")).put("pending",new JSONObject());((EditText)get("composer")).setText("another");
  call("updateControls");assertFalse(((Button)get("sendButton")).isEnabled());assertFalse(((Button)get("modelButton")).isEnabled());assertEquals(View.VISIBLE,((Button)get("retryButton")).getVisibility());
 }
 @Test public void connectionExplainsBothStepsAndBackReturnsToChat()throws Exception{
  call("settings");draw(1280,"connection.png");assertTrue((Boolean)get("connectionScreen"));activity.onBackPressed();assertFalse((Boolean)get("connectionScreen"));
 }
 @Test public void lockingClearsVisibleMessagesAndDraftStaysEncrypted()throws Exception{
  ((EditText)get("composer")).setText("synthetic draft");call("lock");assertNull(get("data"));assertNull(get("vaultKey"));assertNull(get("messageList"));draw(1280,"locked.png");
 }
 @Test public void imageStartsCollapsedAndTogglePersistsVisibility()throws Exception{
  JSONObject d=(JSONObject)get("data");Bitmap bitmap=Bitmap.createBitmap(2,2,Bitmap.Config.ARGB_8888);ByteArrayOutputStream out=new ByteArrayOutputStream();bitmap.compress(Bitmap.CompressFormat.PNG,100,out);bitmap.recycle();
  JSONObject image=new JSONObject().put("prompt","test image").put("after_message",1).put("images",new JSONArray().put(new JSONObject().put("data",Vault.b64(out.toByteArray()))));d.getJSONArray("images").put(image);call("render");
  assertEquals(0,((java.util.List<?>)get("bitmaps")).size());View toggle=findDescription((LinearLayout)get("messageList"),"Expand image");assertNotNull(toggle);toggle.performClick();assertTrue(image.getBoolean("expanded"));assertEquals(1,((java.util.List<?>)get("bitmaps")).size());
  findDescription((LinearLayout)get("messageList"),"Collapse image").performClick();assertFalse(image.getBoolean("expanded"));assertEquals(0,((java.util.List<?>)get("bitmaps")).size());
 }
 @Test public void creatingThreadPreservesOldHistoryAndDraft()throws Exception{
  JSONObject d=(JSONObject)get("data");String id=d.getString("active_thread");((EditText)get("composer")).setText("original draft");call("newConversation");assertEquals(2,d.getJSONArray("threads").length());assertEquals(0,d.getJSONArray("messages").length());
  Method select=MainActivity.class.getDeclaredMethod("selectConversation",String.class);select.setAccessible(true);select.invoke(activity,id);assertEquals(2,d.getJSONArray("messages").length());assertEquals("original draft",((EditText)get("composer")).getText().toString());
 }
 @Test public void threadSettingsSaveNameInstructionsAndPin()throws Exception{
  call("renameConversation");Shadows.shadowOf(android.os.Looper.getMainLooper()).idle();android.app.AlertDialog dialog=(android.app.AlertDialog)get("threadDialog");View form=dialog.getWindow().getDecorView();
  ((EditText)findDescription(form,"Conversation name")).setText("Writing");((EditText)findDescription(form,"Conversation instructions")).setText("Use concise prose.");((CheckBox)findDescription(form,"Pin conversation")).setChecked(true);
  dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE).performClick();JSONObject thread=Conversations.current((JSONObject)get("data"));assertEquals("Writing",thread.getString("title"));assertEquals("Use concise prose.",thread.getString("instructions"));assertTrue(thread.getBoolean("pinned"));assertTrue(((TextView)get("screenTitle")).getText().toString().contains("Writing"));assertFalse(dialog.isShowing());
 }
 @Test public void lockingDismissesInstructionsEditor()throws Exception{
  call("renameConversation");android.app.AlertDialog dialog=(android.app.AlertDialog)get("threadDialog");assertTrue(dialog.isShowing());call("lock");assertFalse(dialog.isShowing());assertNull(get("threadDialog"));assertNull(get("data"));
 }
 @Test public void dedicatedConversationScreenOpensThreadsAndKeepsDrafts()throws Exception{
  JSONObject d=(JSONObject)get("data");String original=d.getString("active_thread");JSONObject other=Conversations.create(d);other.put("title","Writing").put("pinned",true);Conversations.select(d,original);call("chat");((EditText)get("composer")).setText("unfinished idea");call("chooseConversation");
  assertTrue((Boolean)get("conversationsScreen"));assertNull(get("composer"));assertNotNull(findDescription(activity.getWindow().getDecorView(),"New conversation"));draw(1280,"conversations.png");
  View writing=findDescription(activity.getWindow().getDecorView(),"Open conversation Writing");assertNotNull(writing);writing.performClick();assertFalse((Boolean)get("conversationsScreen"));assertEquals(other.getString("id"),d.getString("active_thread"));
  ((Button)get("threadButton")).performClick();View first=findDescription(activity.getWindow().getDecorView(),"Open conversation Conversation 1");assertNotNull(first);first.performClick();assertEquals("unfinished idea",((EditText)get("composer")).getText().toString());
 }
 @Test public void conversationSearchFiltersRowsAndBackFromChatShowsList()throws Exception{
  activity.onBackPressed();assertTrue((Boolean)get("conversationsScreen"));EditText search=(EditText)findDescription(activity.getWindow().getDecorView(),"Search conversations");assertNotNull(search);search.setText("does not exist");assertNull(findDescription(activity.getWindow().getDecorView(),"Open conversation Conversation 1"));search.setText("Conversation");assertNotNull(findDescription(activity.getWindow().getDecorView(),"Open conversation Conversation 1"));
 }
 static View findDescription(View view,String description){if(description.contentEquals(view.getContentDescription()==null?"":view.getContentDescription()))return view;if(view instanceof ViewGroup){ViewGroup group=(ViewGroup)view;for(int i=0;i<group.getChildCount();i++){View found=findDescription(group.getChildAt(i),description);if(found!=null)return found;}}return null;}
}

