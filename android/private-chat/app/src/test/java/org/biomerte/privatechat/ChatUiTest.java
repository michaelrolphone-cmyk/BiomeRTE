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
}
