package org.biomerte.privatechat;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import javax.crypto.*;
import javax.crypto.spec.*;
public final class ImageCrypto {
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
 public static byte[] edPublic(byte[] key){return new org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters(key,0).generatePublicKey().getEncoded();}
 public static byte[] sign(byte[] key,byte[] message){org.bouncycastle.crypto.signers.Ed25519Signer s=new org.bouncycastle.crypto.signers.Ed25519Signer();s.init(true,new org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters(key,0));s.update(message,0,message.length);return s.generateSignature();}
 public static void verify(byte[] key,byte[] message,byte[] signature)throws Exception{
  if(key.length!=32||signature.length!=64)throw new java.security.GeneralSecurityException();
  org.bouncycastle.crypto.signers.Ed25519Signer s=new org.bouncycastle.crypto.signers.Ed25519Signer();s.init(false,new org.bouncycastle.crypto.params.Ed25519PublicKeyParameters(key,0));s.update(message,0,message.length);if(!s.verifySignature(signature))throw new java.security.GeneralSecurityException("Untrusted identity");
 }
 public static byte[] xPublic(byte[] key){return new org.bouncycastle.crypto.params.X25519PrivateKeyParameters(key,0).generatePublicKey().getEncoded();}
 public static byte[] shared(byte[] key,byte[] other){if(key.length!=32||other.length!=32)throw new IllegalArgumentException();byte[] out=new byte[32];new org.bouncycastle.crypto.params.X25519PrivateKeyParameters(key,0).generateSecret(new org.bouncycastle.crypto.params.X25519PublicKeyParameters(other,0),out,0);return out;}
 public static byte[] join(byte[]... values)throws Exception{java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream();for(byte[] value:values)out.write(value);return out.toByteArray();}
 public static byte[] hash(byte[] value)throws Exception{return java.security.MessageDigest.getInstance("SHA-256").digest(value);}
 public static byte[] utf8(String value){return value.getBytes(StandardCharsets.UTF_8);}
 public static byte[] traffic(byte[] shared,byte[] transcriptHash,String direction)throws Exception{
  if(shared.length!=32||transcriptHash.length!=32||!(direction.equals("request")||direction.equals("response")))throw new IllegalArgumentException();
  Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(transcriptHash,"HmacSHA256"));byte[] prk=mac.doFinal(shared);
  try{mac.init(new SecretKeySpec(prk,"HmacSHA256"));return mac.doFinal(join(utf8("biome-private-image/v2/"+direction),new byte[]{1}));}finally{wipe(prk);}
 }
 public static byte[] clientTranscript(String helloId,String sid,byte[] identity,byte[] ephemeral,byte[] nonce)throws Exception{
  canonicalId(helloId);canonicalId(sid);if(identity.length!=32||ephemeral.length!=32||nonce.length!=32)throw new IllegalArgumentException();
  return join(utf8("biome-private-image/v2/client-hello\0"),utf8(helloId),utf8(sid),identity,ephemeral,nonce);
 }
 public static byte[] serverTranscript(byte[] client,byte[] ephemeral,byte[] nonce,byte[] identity)throws Exception{
  if(ephemeral.length!=32||nonce.length!=32||identity.length!=32)throw new IllegalArgumentException();
  return join(utf8("biome-private-image/v2/server-hello\0"),client,ephemeral,nonce,identity);
 }
 public static void canonicalId(String id){if(!java.util.UUID.fromString(id).toString().equals(id))throw new IllegalArgumentException();}
 public static byte[] sessionAAD(String kind,String sid,String operation,byte[] hash)throws Exception{
  canonicalId(sid);canonicalId(operation);if(hash.length!=32||!(kind.equals("request")||kind.equals("response")||kind.equals("progress")))throw new IllegalArgumentException();
  return join(utf8("biome-private-image|2|"+kind+"|"+sid+"|"+operation+"|"),hash);
 }
 public static byte[] aad(String direction,String id){return ("biome-private-image|1|"+direction+"|"+id).getBytes(StandardCharsets.UTF_8);}
 public static void wipe(byte[] key){if(key!=null)Arrays.fill(key,(byte)0);}
 private ImageCrypto(){}
}

