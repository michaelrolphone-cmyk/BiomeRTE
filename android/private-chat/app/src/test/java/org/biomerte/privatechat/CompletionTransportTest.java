package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class CompletionTransportTest {
 @Test public void completionUsesOneRequestAndOriginalIdentity()throws Exception{
  int[] requests={0};
  Biome b=new Biome(){
   @Override public JSONObject completion(String t,String r,JSONObject e,boolean image,java.util.function.BooleanSupplier active)throws Exception{
    requests[0]++;assertEquals("original",e.getString("id"));
    return new JSONObject().put("id","original").put("state","succeeded").put("result",new JSONObject().put("envelope",new JSONObject().put("kind","response")));
   }
   @Override public JSONObject invoke(String t,String op,JSONObject args,String rid){throw new AssertionError("No acquire/status/release polling from Android");}
  };
  assertEquals("response",b.exchange("token","resource",new JSONObject().put("id","original"),false,()->true).getString("kind"));
  assertEquals(1,requests[0]);
 }
 @Test public void uncertainCompletionDoesNotResubmit()throws Exception{
  Biome b=new Biome(){@Override public JSONObject completion(String t,String r,JSONObject e,boolean image,java.util.function.BooleanSupplier active)throws Exception{
   return new JSONObject().put("state","uncertain");
  }};
  try{b.exchange("token","resource",new JSONObject().put("id","same"),false,()->true);fail();}
  catch(java.io.IOException expected){assertTrue(expected.getMessage().contains("reconciliation"));}
 }
}
