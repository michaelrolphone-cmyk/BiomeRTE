"""Opt-in LAN commissioning discovery. Announcements never establish trust."""
import json
import socket
import struct
import time

GROUP='239.255.42.73'
PORT=43739
PROBE=b'BIOMERTE-DISCOVER/1'


def respond(app,stop,port,tls):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        s.bind(('',PORT))
        s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,socket.inet_aton(GROUP)+socket.inet_aton('0.0.0.0'))
        s.settimeout(1)
        while not stop.is_set():
            try:data,addr=s.recvfrom(256)
            except socket.timeout:continue
            if data==PROBE:
                s.sendto(json.dumps({'protocol':'biomerte/1','site':app.site,'fingerprint':app.node,
                                     'port':port,'tls':tls}).encode(),addr)
    finally:s.close()


def discover(seconds):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
    found={}
    try:
        s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,1)
        s.sendto(PROBE,(GROUP,PORT))
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            s.settimeout(max(.01,end-time.monotonic()))
            try:data,addr=s.recvfrom(2048)
            except socket.timeout:break
            try:
                msg=json.loads(data.decode())
                if msg.get('protocol')=='biomerte/1':
                    msg['address']=addr[0];found[(addr[0],msg['port'])]=msg
            except (ValueError,TypeError,KeyError,AttributeError):continue
    finally:s.close()
    return list(found.values())
