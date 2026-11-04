package org.biomerte.decisions;
import android.content.*;
public class BootReceiver extends BroadcastReceiver {public void onReceive(Context c,Intent i){if(!Intent.ACTION_BOOT_COMPLETED.equals(i.getAction()))return;App.work(c,()->{if(new State(c).read().has("config"))App.start(c);});}}
