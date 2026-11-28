package org.biomerte.decisions;
import android.app.*;
import android.content.*;
import android.os.*;
import org.json.*;
import java.util.concurrent.*;

public class SyncService extends Service {
 private ScheduledExecutorService timer;
 @Override public void onCreate(){
  super.onCreate();
  Notification n=new Notification.Builder(this,"biome-decisions-sync").setSmallIcon(R.drawable.ic_notification).setContentTitle("Biome Decisions").setContentText("Decision inbox connection enabled").setOngoing(true).setContentIntent(DecisionWidget.open(this,"")).build();
  startForeground(81,n);
  timer=Executors.newSingleThreadScheduledExecutor();
  timer.scheduleWithFixedDelay(()->App.sync(this),0,10,TimeUnit.SECONDS);
 }
 static void notifyPending(Context c)throws Exception {
  JSONObject s=new State(c).read();JSONArray pending=State.pending(s);
  int count=pending.length();String digest=pending.toString();
  Vault v=new Vault(c);String old=v.get("notified");
  if(count>0&&!digest.equals(old)){
   c.getSystemService(NotificationManager.class).notify(82,new Notification.Builder(c,"biome-decisions-pending").setSmallIcon(R.drawable.ic_notification).setContentTitle("Biome needs a decision").setContentText("Open to review "+count+" pending decision"+(count==1?"":"s")).setContentIntent(DecisionWidget.open(c,"")).setVisibility(Notification.VISIBILITY_PRIVATE).setAutoCancel(true).build());
   v.put("notified",digest);
  }
  if(count==0)c.getSystemService(NotificationManager.class).cancel(82);
 }
 @Override public int onStartCommand(Intent i,int flags,int id){return START_STICKY;}
 @Override public IBinder onBind(Intent i){return null;}
 @Override public void onDestroy(){if(timer!=null)timer.shutdownNow();super.onDestroy();}
}
