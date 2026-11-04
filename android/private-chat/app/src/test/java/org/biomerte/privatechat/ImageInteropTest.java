package org.biomerte.privatechat;
import org.junit.Test;
import static org.junit.Assert.*;
import java.util.Arrays;
import java.util.HexFormat;
public class ImageInteropTest {
 private byte[] range(int start,int count){byte[] r=new byte[count];for(int i=0;i<count;i++)r[i]=(byte)(start+i);return r;}
 private String hex(byte[] x){return HexFormat.of().formatHex(x);}
 @Test public void pythonFixedHandshakeAndCiphertext()throws Exception{
  byte[] phone=range(0,32),server=range(32,32),clientPrivate=range(64,32),serverPrivate=range(96,32);
  assertEquals("03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",hex(ImageCrypto.edPublic(phone)));
  assertEquals("79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a",hex(ImageCrypto.xPublic(clientPrivate)));
  assertEquals("675dd574ed7789310b3d2e7681f3790b466c773b1521fecf36577958371ea52f",hex(ImageCrypto.xPublic(serverPrivate)));
  String hello="00000000-0000-4000-8000-000000000001",sid="00000000-0000-4000-8000-000000000002",op="00000000-0000-4000-8000-000000000003";
  byte[] client=ImageCrypto.clientTranscript(hello,sid,ImageCrypto.edPublic(phone),ImageCrypto.xPublic(clientPrivate),range(128,32));
  assertEquals("d8c021a19d483104d9558f08d5e521a8781893d5f0a11546ff6dd2c1158537f0498aafa9c29a79f933934a322c7bd81771d12156b849fdcb5a79927579610905",hex(ImageCrypto.sign(phone,client)));
  byte[] transcript=ImageCrypto.serverTranscript(client,ImageCrypto.xPublic(serverPrivate),range(160,32),ImageCrypto.edPublic(server));
  assertEquals("a6d6a7737d46d10150a869a9c9b3d4491a7afac9a2797c8f46a39faa8e427f7dfc159aeaf811c6cf2a51ce68248483b5f1b5705ac6c15bc16096f556cd10ce04",hex(ImageCrypto.sign(server,transcript)));
  ImageCrypto.verify(ImageCrypto.edPublic(server),transcript,ImageCrypto.sign(server,transcript));
  byte[] hash=ImageCrypto.hash(transcript),shared=ImageCrypto.shared(clientPrivate,ImageCrypto.xPublic(serverPrivate));
  assertArrayEquals(shared,ImageCrypto.shared(serverPrivate,ImageCrypto.xPublic(clientPrivate)));
  assertEquals("1872637e16d14a137f190954fbd7c6c3e5ed2d2b3504f87d57b6f8149e9005de",hex(hash));
  byte[] request=ImageCrypto.traffic(shared,hash,"request");
  assertEquals("e192bb7cf8796f31588de7dd76605931c2168fbb57b9d855ea5b54414148fa4a",hex(request));
  assertEquals("19027cf61226d0b9035910e28e4aaf54f50271626a8467c2956ccd71a47e5233",hex(ImageCrypto.traffic(shared,hash,"response")));
  assertEquals("5c6830e25a970df05b31c0811dd9db71e2d3fc79236b644502604c9a748ebfdba64855c1f5d982567fb1e9ea589037",hex(ImageCrypto.crypt(true,request,range(0,12),ImageCrypto.sessionAAD("request",sid,op,hash),ImageCrypto.utf8("synthetic interoperability test"))));
 }
 @Test public void signatureTamperingAndWrongPinRejected()throws Exception{
  byte[] key=range(0,32),message=ImageCrypto.utf8("authenticated ephemeral transcript"),sig=ImageCrypto.sign(key,message);
  message[0]^=1;try{ImageCrypto.verify(ImageCrypto.edPublic(key),message,sig);fail();}catch(java.security.GeneralSecurityException expected){}
  message[0]^=1;try{ImageCrypto.verify(ImageCrypto.edPublic(range(32,32)),message,sig);fail();}catch(java.security.GeneralSecurityException expected){}
 }
 @Test public void lowOrderPeerRejected(){
  try{ImageCrypto.shared(range(0,32),new byte[32]);fail();}catch(IllegalStateException|IllegalArgumentException expected){}
 }
}
