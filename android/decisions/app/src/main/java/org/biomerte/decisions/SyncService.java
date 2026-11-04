package org.biomerte.decisions;
import android.app.*;
import android.content.*;
import android.os.*;
import org.json.*;
import java.util.concurrent.*;
public class SyncService extends Service {
 private ScheduledExecutorService timer;
 private volatile boolean pending=false;
 @Override public void onCreate(){super.onCreate();Notification n=new Notification.Builder(this,"biome-decisions-sync").setSmallIcon(R.drawable.ic_notification).setContentTitle("Biome Decisions").setContentText("Decision inbox is connected").setOngoing(true).setContentIntent(DecisionWidget.open(this,"")).build();startForeground(81,n);timer=Executors.newSingleThreadScheduledExecutor();timer.scheduleWithFixedDelay(()->{if(pending)return;pending=true;App.IO.execute(()->{try{Client.sync(this);DecisionWidget.update(this);App.notifyUi();JSONObject s=new State(this).read();int count=State.pending(s).length();String digest=State.pending(s).toString();Vault v=new Vault(this);String old=v.get("notified");if(count>0&&!digest.equals(old)){getSystemService(NotificationManager.class).notify(82,new Notification.Builder(this,"biome-decisions-pending").setSmallIcon(R.drawable.ic_notification).setContentTitle("Biome needs a decision").setContentText("Open to review "+count+" pending decision"+(count==1?"":"s")).setContentIntent(DecisionWidget.open(this,"")).setVisibility(Notification.VISIBILITY_PRIVATE).setAutoCancel(true).build());v.put("notified",digest);}if(count==0)getSystemService(NotificationManager.class).cancel(82);}catch(Exception e){App.error(this,e);}finally{pending=false;}});},0,10,TimeUnit.SECONDS);}
 @Override public int onStartCommand(Intent i,int flags,int id){return START_STICKY;}
 @Override public IBinder onBind(Intent i){return null;}
 @Override public void onDestroy(){if(timer!=null)timer.shutdownNow();super.onDestroy();}
}
