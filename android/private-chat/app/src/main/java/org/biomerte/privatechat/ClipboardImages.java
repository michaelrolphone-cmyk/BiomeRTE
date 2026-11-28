package org.biomerte.privatechat;
import android.content.Context;
import android.net.Uri;
import android.security.keystore.*;
import java.io.*;
import java.security.KeyStore;
import java.util.*;
import javax.crypto.*;
import javax.crypto.spec.GCMParameterSpec;

/** Clipboard exports stay encrypted at rest, independently from conversation history. */
final class ClipboardImages {
 interface Keys {javax.crypto.SecretKey get()throws Exception;}
 static Keys keys=ClipboardImages::platformKey;
 private static synchronized javax.crypto.SecretKey platformKey()throws Exception{
  String alias="biome-clipboard-images-v1";KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
  if(store.containsAlias(alias))return (javax.crypto.SecretKey)store.getKey(alias,null);
  KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");generator.init(new KeyGenParameterSpec.Builder(alias,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT).setKeySize(256).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());return generator.generateKey();
 }
 static File directory(Context context){return new File(context.getCacheDir(),"clipboard-images");}
 static File file(Context context,Uri uri)throws FileNotFoundException{
  if(!"content".equals(uri.getScheme())||!(context.getPackageName()+".clipboard").equals(uri.getAuthority())||uri.getPathSegments().size()!=1)throw new FileNotFoundException();
  String token=uri.getLastPathSegment();if(token==null||!token.matches("[a-f0-9]{32}\\.png"))throw new FileNotFoundException();return new File(directory(context),token);
 }
 static synchronized Uri store(Context context,byte[] png)throws Exception{
  byte[] signature={(byte)137,80,78,71,13,10,26,10};if(png.length<8)throw new IOException("Invalid image");for(int i=0;i<8;i++)if(png[i]!=signature[i])throw new IOException("Invalid image");
  Uri uri=new Uri.Builder().scheme("content").authority(context.getPackageName()+".clipboard").appendPath(UUID.randomUUID().toString().replace("-","")+".png").build();
  File dir=directory(context);if(!dir.isDirectory()&&!dir.mkdirs())throw new IOException();File target=file(context,uri),temp=new File(dir,target.getName()+".next");
  Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,keys.get());cipher.updateAAD(uri.getLastPathSegment().getBytes(java.nio.charset.StandardCharsets.UTF_8));byte[] encrypted=cipher.doFinal(png);
  try{try(FileOutputStream out=new FileOutputStream(temp)){out.write(cipher.getIV());out.write(encrypted);}if(!temp.renameTo(target))throw new IOException();return uri;}finally{Crypto.wipe(encrypted);if(temp.exists())temp.delete();}
 }
 static synchronized byte[] read(Context context,Uri uri)throws Exception{
  File source=file(context,uri);byte[] encrypted=java.nio.file.Files.readAllBytes(source.toPath());try{if(encrypted.length<28)throw new IOException();Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.DECRYPT_MODE,keys.get(),new GCMParameterSpec(128,Arrays.copyOf(encrypted,12)));cipher.updateAAD(uri.getLastPathSegment().getBytes(java.nio.charset.StandardCharsets.UTF_8));return cipher.doFinal(encrypted,12,encrypted.length-12);}finally{Crypto.wipe(encrypted);}
 }
 static synchronized void prune(Context context,Uri keep){File[] files=directory(context).listFiles();if(files!=null)for(File file:files)if(!file.getName().equals(keep.getLastPathSegment()))file.delete();}
}
