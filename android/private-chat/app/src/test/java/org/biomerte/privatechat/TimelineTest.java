package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class)
@Config(sdk=28)
public class TimelineTest {
 JSONObject msg(String s)throws Exception{return new JSONObject().put("role","user").put("content",s);}
 @Test public void imagesStayBetweenEarlierAndLaterMessagesAfterReload()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray().put(msg("before")).put(msg("after")))
   .put("images",new JSONArray().put(new JSONObject().put("prompt","image").put("after_message",1)));
  JSONArray a=Timeline.items(new JSONObject(d.toString()));
  assertEquals("before",a.getJSONObject(0).getJSONObject("value").getString("content"));
  assertEquals("image",a.getJSONObject(1).getString("kind"));
  assertEquals("after",a.getJSONObject(2).getJSONObject("value").getString("content"));
  d.getJSONArray("messages").put(msg("newest"));
  assertEquals("image",Timeline.items(d).getJSONObject(1).getString("kind"));
  assertFalse(d.getJSONArray("messages").toString().contains("after_message"));
 }
 @Test public void legacyImagesKeepTheirKnownPositionWhenMoreMessagesArrive()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray().put(msg("old")))
   .put("images",new JSONArray().put(new JSONObject().put("prompt","legacy")));
  assertTrue(Timeline.pinLegacyImages(d));assertFalse(Timeline.pinLegacyImages(d));
  d.getJSONArray("messages").put(msg("new"));
  assertEquals("image",Timeline.items(d).getJSONObject(1).getString("kind"));
 }
 @Test public void imagesAtSamePositionKeepCompletionOrder()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray())
   .put("images",new JSONArray().put(new JSONObject().put("prompt","first").put("after_message",0))
   .put(new JSONObject().put("prompt","second").put("after_message",0)));
  assertEquals("first",Timeline.items(d).getJSONObject(0).getJSONObject("value").getString("prompt"));
  assertEquals("second",Timeline.items(d).getJSONObject(1).getJSONObject("value").getString("prompt"));
 }
}
