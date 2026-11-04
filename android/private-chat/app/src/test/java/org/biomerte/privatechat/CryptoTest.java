package org.biomerte.privatechat;
import org.junit.Test;
import static org.junit.Assert.*;
import java.nio.charset.StandardCharsets;
import java.util.HexFormat;
import javax.crypto.AEADBadTagException;
public class CryptoTest {
 @Test public void directionVector() throws Exception {
  byte[] master=new byte[32];for(int i=0;i<32;i++)master[i]=(byte)i;
  assertEquals("0917155f8db00bad47e91a1a2db6e284a863ab763161b704823f71cc3d5833ed",HexFormat.of().formatHex(Crypto.direction(master,"request")));
 }
 @Test public void tamperingRejected() throws Exception {
  byte[] k=Crypto.random(32),n=Crypto.random(12),a=Crypto.aad("request","00000000-0000-4000-8000-000000000001"),p="test".getBytes(StandardCharsets.UTF_8);
  byte[] c=Crypto.crypt(true,k,n,a,p);assertArrayEquals(p,Crypto.crypt(false,k,n,a,c));
  c[0]^=1;try{Crypto.crypt(false,k,n,a,c);fail();}catch(AEADBadTagException expected){}
 }
 @Test public void wrongPasswordChangesKey() throws Exception {
  byte[] salt=Crypto.random(32);assertFalse(java.util.Arrays.equals(Crypto.derive("correct passphrase".toCharArray(),salt),Crypto.derive("wrong passphrase".toCharArray(),salt)));
 }
}
