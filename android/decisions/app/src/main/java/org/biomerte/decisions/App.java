package org.biomerte.decisions;
import android.app.*;
import android.content.*;
import android.os.*;
import java.util.concurrent.*;
public class App extends Application {
 public static final ExecutorService IO=Executors.newSingleThreadExecutor();
 public static Runnable changed;
 public interface Work {void run()throws Exception;}
 public static void work(Context c,Work task){IO.execute(()->{try{task.run();}catch(Exception e){error(c,e);}});}
 public static void error(Context c,Exception e){try{new State(c).status(e.getMessage()==null?"Connection interrupted; saved replies retained":e.getMessage());DecisionWidget.update(c);}catch(Exception ignored){}notifyUi();}
 public static void notifyUi(){new Handler(Looper.getMainLooper()).post(()->{if(changed!=null)changed.run();});}
 public static void sync(Context c){work(c,()->{Client.sync(c);DecisionWidget.update(c);notifyUi();});}
 public static void start(Context c){c.startForegroundService(new Intent(c,SyncService.class));}
 @Override public void onCreate(){super.onCreate();NotificationManager m=getSystemService(NotificationManager.class);m.createNotificationChannel(new NotificationChannel("biome-decisions-sync","Live decision connection",NotificationManager.IMPORTANCE_LOW));m.createNotificationChannel(new NotificationChannel("biome-decisions-pending","Decisions waiting",NotificationManager.IMPORTANCE_DEFAULT));IntentFilter f=new IntentFilter();f.addAction(Intent.ACTION_USER_PRESENT);f.addAction(Intent.ACTION_SCREEN_OFF);BroadcastReceiver screen=new BroadcastReceiver(){public void onReceive(Context c,Intent i){work(c,()->DecisionWidget.update(c));}};if(Build.VERSION.SDK_INT>=33)registerReceiver(screen,f,Context.RECEIVER_NOT_EXPORTED);else registerReceiver(screen,f);}
}
