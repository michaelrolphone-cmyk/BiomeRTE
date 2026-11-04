package org.biomerte.privatechat;
import android.os.Looper;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;
import java.lang.reflect.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class MultitaskingTest {
 static Object get(Object a,String n)throws Exception{Field f=MainActivity.class.getDeclaredField(n);f.setAccessible(true);return f.get(a);}
 static void set(Object a,String n,Object v)throws Exception{Field f=MainActivity.class.getDeclaredField(n);f.setAccessible(true);f.set(a,v);}
 static void call(Object a,String n)throws Exception{Method m=MainActivity.class.getDeclaredMethod(n);m.setAccessible(true);m.invoke(a);}
 @Test public void backgroundForegroundPreservesUnlockedSessionAndPendingRequest()throws Exception{
  ActivityController<MainActivity> c=Robolectric.buildActivity(MainActivity.class).setup();
  MainActivity a=c.get();byte[] key=new byte[32];
  JSONObject pending=new JSONObject().put("image",true).put("stage","message");
  JSONObject data=new JSONObject().put("messages",new JSONArray()).put("pending",pending);
  set(a,"data",data);set(a,"vaultKey",key);set(a,"salt",new byte[32]);call(a,"chat");
  set(a,"busy",true);int epoch=(Integer)get(a,"epoch");
  c.pause().stop().start().resume().visible();
  assertSame(data,get(a,"data"));assertSame(key,get(a,"vaultKey"));
  assertSame(pending,data.getJSONObject("pending"));assertTrue((Boolean)get(a,"busy"));
  assertEquals(epoch,get(a,"epoch"));
  call(a,"lock");assertNull(get(a,"data"));assertNull(get(a,"vaultKey"));
 }
 @Test public void freshActivityStillRequiresPassword()throws Exception{
  MainActivity a=Robolectric.buildActivity(MainActivity.class).setup().get();
  assertNull(get(a,"data"));assertNull(get(a,"vaultKey"));
 }
 @Test public void externalBrowserReturnKeepsSessionAndRestoresConnectionScreen()throws Exception{
  ActivityController<MainActivity> c=Robolectric.buildActivity(MainActivity.class).setup();MainActivity a=c.get();
  JSONObject data=new JSONObject().put("messages",new JSONArray());byte[] key=new byte[32];
  set(a,"data",data);set(a,"vaultKey",key);set(a,"salt",new byte[32]);call(a,"settings");set(a,"returnToConnection",true);
  c.pause().stop().start().resume().visible();
  assertSame(data,get(a,"data"));assertSame(key,get(a,"vaultKey"));
  assertTrue((Boolean)get(a,"connectionScreen"));assertFalse((Boolean)get(a,"returnToConnection"));
 }
}
