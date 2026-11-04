package org.biomerte.privatechat;
final class WireCrypto {
 private final boolean image;
 WireCrypto(boolean image){this.image=image;}
 byte[] clientTranscript(String id,String sid,byte[] identity,byte[] ephemeral,byte[] nonce)throws Exception{return image?ImageCrypto.clientTranscript(id,sid,identity,ephemeral,nonce):Crypto.clientTranscript(id,sid,identity,ephemeral,nonce);}
 byte[] serverTranscript(byte[] client,byte[] ephemeral,byte[] nonce,byte[] identity)throws Exception{return image?ImageCrypto.serverTranscript(client,ephemeral,nonce,identity):Crypto.serverTranscript(client,ephemeral,nonce,identity);}
 byte[] traffic(byte[] shared,byte[] hash,String direction)throws Exception{return image?ImageCrypto.traffic(shared,hash,direction):Crypto.traffic(shared,hash,direction);}
 byte[] sessionAAD(String kind,String sid,String id,byte[] hash)throws Exception{return image?ImageCrypto.sessionAAD(kind,sid,id,hash):Crypto.sessionAAD(kind,sid,id,hash);}
}
