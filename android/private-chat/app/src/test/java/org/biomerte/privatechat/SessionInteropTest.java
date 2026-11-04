package org.biomerte.privatechat;
import org.junit.Test;
import static org.junit.Assert.*;
import java.util.Arrays;
import java.util.HexFormat;
public class SessionInteropTest {
 private byte[] range(int start,int count){byte[] r=new byte[count];for(int i=0;i<count;i++)r[i]=(byte)(start+i);return r;}
 private String hex(byte[] x){return HexFormat.of().formatHex(x);}
 @Test public void pythonFixedHandshakeAndCiphertext()throws Exception{
  byte[] phone=range(0,32),server=range(32,32),clientPrivate=range(64,32),serverPrivate=range(96,32);
  assertEquals("03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",hex(Crypto.edPublic(phone)));
  assertEquals("79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a",hex(Crypto.xPublic(clientPrivate)));
  assertEquals("675dd574ed7789310b3d2e7681f3790b466c773b1521fecf36577958371ea52f",hex(Crypto.xPublic(serverPrivate)));
  String hello="00000000-0000-4000-8000-000000000001",sid="00000000-0000-4000-8000-000000000002",op="00000000-0000-4000-8000-000000000003";
  byte[] client=Crypto.clientTranscript(hello,sid,Crypto.edPublic(phone),Crypto.xPublic(clientPrivate),range(128,32));
  assertEquals("820e4b12920adb04ee0c5b4283bea1630cde78424e797c8989534e58cf782f54a19fdb90fc888a7e11643b086ce5bebe5d2e011fbeea8756a9637331852e2009",hex(Crypto.sign(phone,client)));
  byte[] transcript=Crypto.serverTranscript(client,Crypto.xPublic(serverPrivate),range(160,32),Crypto.edPublic(server));
  assertEquals("b636f4b83dba2b259a9cc6319d257cf011a6406961553d7a924ad98ba6cbec6055ab3aa89d34735eff6543c49df6a1ece59799f6594e18bb6971e8c91e30f502",hex(Crypto.sign(server,transcript)));
  Crypto.verify(Crypto.edPublic(server),transcript,Crypto.sign(server,transcript));
  byte[] hash=Crypto.hash(transcript),shared=Crypto.shared(clientPrivate,Crypto.xPublic(serverPrivate));
  assertArrayEquals(shared,Crypto.shared(serverPrivate,Crypto.xPublic(clientPrivate)));
  assertEquals("993979b14b05225a4a2cddf4d6cf5d5f58df35070bd06dee7ba5904b8b7c42be",hex(hash));
  byte[] request=Crypto.traffic(shared,hash,"request");
  assertEquals("eb0efeb52d6a9f0c4fd635db7d2ddc5c2ac15f7bf5a4c9ee03f95852cfe3f411",hex(request));
  assertEquals("6351365b53c20386c7939fe1a7a8d7ae980da5a75e1cdbb98b1cde5559a07fbf",hex(Crypto.traffic(shared,hash,"response")));
  assertEquals("f58fa1e2636fb8768322a671b6263f06255472bb0243df37892ef9c76618ef8a4005a625406d13bd9b7389f2a62560",hex(Crypto.crypt(true,request,range(0,12),Crypto.sessionAAD("request",sid,op,hash),Crypto.utf8("synthetic interoperability test"))));
 }
 @Test public void signatureTamperingAndWrongPinRejected()throws Exception{
  byte[] key=range(0,32),message=Crypto.utf8("authenticated ephemeral transcript"),sig=Crypto.sign(key,message);
  message[0]^=1;try{Crypto.verify(Crypto.edPublic(key),message,sig);fail();}catch(java.security.GeneralSecurityException expected){}
  message[0]^=1;try{Crypto.verify(Crypto.edPublic(range(32,32)),message,sig);fail();}catch(java.security.GeneralSecurityException expected){}
 }
 @Test public void lowOrderPeerRejected(){
  try{Crypto.shared(range(0,32),new byte[32]);fail();}catch(IllegalStateException|IllegalArgumentException expected){}
 }
}
