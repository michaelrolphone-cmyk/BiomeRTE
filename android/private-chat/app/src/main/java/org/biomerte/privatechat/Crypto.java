package org.biomerte.privatechat;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import javax.crypto.*;
import javax.crypto.spec.*;
public final class Crypto {
 public static final int ITERATIONS=600000;
 public static byte[] random(int n){byte[] b=new byte[n];new SecureRandom().nextBytes(b);return b;}
 public static byte[] derive(char[] password,byte[] salt)throws Exception{
  PBEKeySpec spec=new PBEKeySpec(password,salt,ITERATIONS,256);
  try{return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).getEncoded();}
  finally{spec.clearPassword();}
 }
 public static byte[] direction(byte[] master,String direction)throws Exception{
  Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(master,"HmacSHA256"));
  return mac.doFinal(("Biome Private Chat v1 "+direction).getBytes(StandardCharsets.UTF_8));
 }
 public static byte[] crypt(boolean encrypt,byte[] key,byte[] nonce,byte[] aad,byte[] data)throws Exception{
  if(key.length!=32||nonce.length!=12)throw new IllegalArgumentException();
  Cipher c=Cipher.getInstance("AES/GCM/NoPadding");c.init(encrypt?Cipher.ENCRYPT_MODE:Cipher.DECRYPT_MODE,new SecretKeySpec(key,"AES"),new GCMParameterSpec(128,nonce));
  c.updateAAD(aad);return c.doFinal(data);
 }
 public static byte[] aad(String direction,String id){return ("biome-private-chat|1|"+direction+"|"+id).getBytes(StandardCharsets.UTF_8);}
 public static void wipe(byte[] key){if(key!=null)Arrays.fill(key,(byte)0);}
 private Crypto(){}
}
