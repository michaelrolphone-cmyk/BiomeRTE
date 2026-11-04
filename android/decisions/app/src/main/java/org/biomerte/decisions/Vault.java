package org.biomerte.decisions;
import android.content.Context;
import android.security.keystore.*;
import android.util.Base64;
import org.json.*;
import java.security.*;
import javax.crypto.*;
import javax.crypto.spec.GCMParameterSpec;
import java.nio.charset.StandardCharsets;
import java.util.*;

public final class Vault {
 private final Context c;
 public Vault(Context c){this.c=c.getApplicationContext();}
 private KeyStore keys()throws Exception{KeyStore k=KeyStore.getInstance("AndroidKeyStore");k.load(null);return k;}
 private SecretKey aes()throws Exception{
  Key key=keys().getKey("biome-decisions-cache",null);if(key!=null)return (SecretKey)key;
  KeyGenerator g=KeyGenerator.getInstance("AES","AndroidKeyStore");g.init(new KeyGenParameterSpec.Builder("biome-decisions-cache",KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());return g.generateKey();
 }
 public static String b64(byte[] v){return Base64.encodeToString(v,Base64.NO_WRAP);}
 public static byte[] unb64(String v){return Base64.decode(v,Base64.NO_WRAP);}
 public synchronized String get(String key)throws Exception{
  String raw=c.getSharedPreferences("biome-private",0).getString(key,null);if(raw==null)return null;
  String[] parts=raw.split(":",2);Cipher a=Cipher.getInstance("AES/GCM/NoPadding");a.init(Cipher.DECRYPT_MODE,aes(),new GCMParameterSpec(128,unb64(parts[0])));return new String(a.doFinal(unb64(parts[1])),StandardCharsets.UTF_8);
 }
 public synchronized void put(String key,String value)throws Exception{
  Cipher a=Cipher.getInstance("AES/GCM/NoPadding");a.init(Cipher.ENCRYPT_MODE,aes());String sealed=b64(a.getIV())+":"+b64(a.doFinal(value.getBytes(StandardCharsets.UTF_8)));
  if(!c.getSharedPreferences("biome-private",0).edit().putString(key,sealed).commit())throw new Exception("Could not save encrypted decisions");
 }
 public String publicKey()throws Exception{
  if(!keys().containsAlias("biome-decisions-phone")){
   KeyPairGenerator g=KeyPairGenerator.getInstance("RSA","AndroidKeyStore");g.initialize(new KeyGenParameterSpec.Builder("biome-decisions-phone",KeyProperties.PURPOSE_SIGN|KeyProperties.PURPOSE_VERIFY).setKeySize(2048).setDigests(KeyProperties.DIGEST_SHA256).setSignaturePaddings(KeyProperties.SIGNATURE_PADDING_RSA_PKCS1).build());g.generateKeyPair();
  }
  return "-----BEGIN PUBLIC KEY-----\n"+Base64.encodeToString(keys().getCertificate("biome-decisions-phone").getPublicKey().getEncoded(),Base64.NO_WRAP)+"\n-----END PUBLIC KEY-----\n";
 }
 public String sign(JSONObject envelope)throws Exception{
  publicKey();Signature s=Signature.getInstance("SHA256withRSA");s.initSign((PrivateKey)keys().getKey("biome-decisions-phone",null));s.update(canonical(envelope).getBytes(StandardCharsets.UTF_8));return b64(s.sign());
 }
 // Envelopes have ASCII fields; all user text is carried losslessly in base64 payload.
 public static String canonical(JSONObject object)throws Exception{
  ArrayList<String> keys=new ArrayList<>();object.keys().forEachRemaining(keys::add);Collections.sort(keys);StringJoiner out=new StringJoiner(",","{","}");for(String k:keys)out.add(JSONObject.quote(k)+":"+JSONObject.quote(object.getString(k)));return out.toString();
 }
}
