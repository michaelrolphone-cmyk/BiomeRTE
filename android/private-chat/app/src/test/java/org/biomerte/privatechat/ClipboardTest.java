package org.biomerte.privatechat;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.*;
import org.robolectric.annotation.*;
import android.content.*;
import android.net.Uri;
import android.graphics.Bitmap;
import android.view.View;
import java.io.*;
import java.util.*;
import javax.crypto.spec.SecretKeySpec;
import static org.junit.Assert.*;
@RunWith(RobolectricTestRunner.class) @Config(sdk=28)
public class ClipboardTest {
 MainActivity a;ClipboardImages.Keys original;
 @Before public void setup(){a=Robolectric.buildActivity(MainActivity.class).setup().get();original=ClipboardImages.keys;ClipboardImages.keys=()->new SecretKeySpec(new byte[32],"AES");}
 @After public void cleanup(){ClipboardImages.keys=original;}
 byte[] png()throws Exception{Bitmap b=Bitmap.createBitmap(2,2,Bitmap.Config.ARGB_8888);ByteArrayOutputStream out=new ByteArrayOutputStream();b.compress(Bitmap.CompressFormat.PNG,100,out);b.recycle();return out.toByteArray();}
 @Test public void encryptedImageExportRoundTripsAndRejectsTamperingAndTraversal()throws Exception{
  byte[] image=png();Uri uri=ClipboardImages.store(a,image);byte[] disk=java.nio.file.Files.readAllBytes(ClipboardImages.file(a,uri).toPath());assertFalse(Arrays.equals(image,disk));assertArrayEquals(image,ClipboardImages.read(a,uri));
  disk[disk.length-1]^=1;java.nio.file.Files.write(ClipboardImages.file(a,uri).toPath(),disk);try{ClipboardImages.read(a,uri);fail();}catch(javax.crypto.AEADBadTagException expected){}
  try{ClipboardImages.file(a,Uri.parse("content://"+a.getPackageName()+".clipboard/../vault"));fail();}catch(FileNotFoundException expected){}
 }
 @Test public void providerServesPngReadOnlyAndReportsMime()throws Exception{
  byte[] image=png();Uri uri=ClipboardImages.store(a,image);ClipboardImageProvider provider=Robolectric.buildContentProvider(ClipboardImageProvider.class).create().get();assertEquals("image/png",provider.getType(uri));
  try(InputStream in=new android.os.ParcelFileDescriptor.AutoCloseInputStream(provider.openFile(uri,"r"))){assertArrayEquals(image,read(in));}
  try{provider.openFile(uri,"w");fail();}catch(FileNotFoundException expected){}
 }
 @Test public void directCopyActionsKeepTextExactAndCopyCollapsedImages()throws Exception{
  JSONObject d=new JSONObject().put("messages",new JSONArray().put(new JSONObject().put("role","user").put("content","prompt\nexact")).put(new JSONObject().put("role","assistant").put("content","response\nexact")));PendingRecoveryTest.set(a,"data",d);PendingRecoveryTest.set(a,"vaultKey",new byte[32]);PendingRecoveryTest.set(a,"salt",new byte[32]);PendingRecoveryTest.call(a,"chat");
  View list=(View)PendingRecoveryTest.get(a,"messageList");ClipboardManager clipboard=(ClipboardManager)a.getSystemService(Context.CLIPBOARD_SERVICE);ChatUiTest.findDescription(list,"Copy prompt").performClick();assertEquals("prompt\nexact",clipboard.getPrimaryClip().getItemAt(0).getText().toString());assertTrue(clipboard.getPrimaryClip().getDescription().getExtras().getBoolean("android.content.extra.IS_SENSITIVE"));ChatUiTest.findDescription(list,"Copy response").performClick();assertEquals("response\nexact",clipboard.getPrimaryClip().getItemAt(0).getText().toString());
  byte[] image=png();JSONObject entry=new JSONObject().put("prompt","image prompt").put("expanded",false).put("images",new JSONArray().put(new JSONObject().put("data",Vault.b64(image))));d.getJSONArray("images").put(entry);PendingRecoveryTest.call(a,"render");list=(View)PendingRecoveryTest.get(a,"messageList");ChatUiTest.findDescription(list,"Copy image prompt").performClick();assertEquals("image prompt",clipboard.getPrimaryClip().getItemAt(0).getText().toString());ChatUiTest.findDescription(list,"Copy generated image").performClick();Uri uri=clipboard.getPrimaryClip().getItemAt(0).getUri();assertNotNull(uri);assertArrayEquals(image,ClipboardImages.read(a,uri));assertFalse(entry.getBoolean("expanded"));assertTrue(clipboard.getPrimaryClipDescription().hasMimeType("image/png"));
 }
 static byte[] read(InputStream in)throws IOException{ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[4096];int n;while((n=in.read(b))!=-1)out.write(b,0,n);return out.toByteArray();}
}
