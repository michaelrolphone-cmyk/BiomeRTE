package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import android.widget.*;
import java.lang.reflect.*;
import java.util.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class PendingRecoveryTest {
 @Test public void imageCanBeParkedWithoutLosingKeysOrHistory()throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();
  JSONObject p=new JSONObject().put("image",true).put("stage","message").put("response_key","synthetic-key").put("envelope",new JSONObject().put("id","original-operation"));
  JSONObject d=new JSONObject().put("pending",p).put("messages",new JSONArray().put("history"));
  set(a,"data",d);set(a,"vaultKey",new byte[32]);set(a,"salt",new byte[32]);set(a,"busy",false);
  call(a,"chat");((EditText)get(a,"composer")).setText("new chat");call(a,"updateControls");
  assertTrue(((Button)get(a,"modelButton")).isEnabled());assertTrue(((Button)get(a,"sendButton")).isEnabled());
  assertEquals(Boolean.TRUE,call(a,"parkPending"));
  assertFalse(d.has("pending"));assertEquals("synthetic-key",d.getJSONArray("requests").getJSONObject(0).getString("response_key"));
  assertEquals("history",d.getJSONArray("messages").getString(0));assertFalse((Boolean)get(a,"busy"));
  assertTrue(((Button)get(a,"modelButton")).isEnabled());
 }
 @Test public void completedRequestResumesWithoutReacquiringReleasedLease()throws Exception{
  List<String> ops=new ArrayList<>();
  Biome b=new Biome(){@Override public JSONObject invoke(String t,String op,JSONObject args,String rid)throws Exception{
   ops.add(op);
   if(op.equals("cap.status"))return new JSONObject().put("state","succeeded").put("lease","released-lease").put("result",new JSONObject().put("envelope",new JSONObject().put("kind","response")));
   if(op.equals("cap.release"))return new JSONObject();
   throw new AssertionError("Must not acquire or invoke completed inference");
  }};
  assertEquals("response",b.exchangeLegacy("token","resource",new JSONObject().put("id","same-id"),true,()->true).getString("kind"));
  assertEquals(Arrays.asList("cap.status","cap.release"),ops);
 }
 @Test public void releasedLeaseDoesNotPollForever()throws Exception{
  Biome b=new Biome(){@Override public JSONObject invoke(String t,String op,JSONObject args,String rid)throws Exception{
   if(op.equals("cap.status"))return new JSONObject().put("state","missing");
   if(op.equals("cap.acquire"))return new JSONObject().put("state","released").put("id","closed");
   throw new AssertionError("Must not poll a terminal lease");
  }};
  try{b.exchangeLegacy("token","resource",new JSONObject().put("id","same-id"),true,()->true);fail();}
  catch(Biome.RequestFailed expected){}
 }
 static Object get(Object a,String n)throws Exception{Field f=MainActivity.class.getDeclaredField(n);f.setAccessible(true);return f.get(a);}
 static void set(Object a,String n,Object v)throws Exception{Field f=MainActivity.class.getDeclaredField(n);f.setAccessible(true);f.set(a,v);}
 static Object call(Object a,String n)throws Exception{Method m=MainActivity.class.getDeclaredMethod(n);m.setAccessible(true);return m.invoke(a);}
}

