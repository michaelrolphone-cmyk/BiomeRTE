package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class PendingTransportTest {
 static class Gateway extends Biome {
  String state;boolean acquired,released;
  Gateway(String state){this.state=state;}
  @Override public JSONObject invoke(String token,String op,JSONObject args,String rid)throws Exception{
   if(op.equals("cap.status"))return new JSONObject().put("state",state).put("lease","old-lease").put("result",new JSONObject().put("envelope",new JSONObject().put("kind","response").put("id","request-id")));
   if(op.equals("cap.acquire")){acquired=true;throw new AssertionError("Completed request must not reacquire a lease");}
   if(op.equals("cap.release")){released=true;return new JSONObject();}
   throw new AssertionError(op);
  }
 }
 @Test public void completedReplyCollectedWithoutLeaseReacquisition()throws Exception{
  Gateway g=new Gateway("succeeded");JSONObject r=g.exchange("token","resource",new JSONObject().put("id","request-id"),false,()->true);
  assertEquals("response",r.getString("kind"));assertFalse(g.acquired);assertTrue(g.released);
 }
 @Test public void failedRequestDoesNotResubmit()throws Exception{
  Gateway g=new Gateway("failed");try{g.exchange("token","resource",new JSONObject().put("id","request-id"),false,()->true);fail();}catch(Biome.RequestFailed expected){}assertFalse(g.acquired);
 }
 @Test public void uncertainRequestRequiresReconciliation()throws Exception{
  Gateway g=new Gateway("uncertain");try{g.exchange("token","resource",new JSONObject().put("id","request-id"),false,()->true);fail();}catch(java.io.IOException expected){}assertFalse(g.acquired);
 }
}
