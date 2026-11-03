"""Small authenticated HTTP adapter. Plain HTTP is limited to loopback."""
import hmac
import json
import logging
import os
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from .store import Fault, encode

log=logging.getLogger('biomerte')


class Server(ThreadingMixIn,HTTPServer):
    daemon_threads=True
    allow_reuse_address=True

    def get_request(self):
        conn,addr=HTTPServer.get_request(self)
        conn.settimeout(20)
        return conn,addr


class Handler(BaseHTTPRequestHandler):
    server_version='BiomeRTE/0.1'
    def send(self,status,body,kind='application/json'):
        if kind=='application/json':body=encode(body).encode()
        self.send_response(status)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path=='/health':
            return self.send(200,{'runtime':'up','realms':dict(self.server.app.health)})
        files={'/':('index.html','text/html'),'/ui.js':('ui.js','application/javascript'),
               '/ui.css':('ui.css','text/css')}
        if self.path not in files:return self.send(404,{'error':'Unknown path'})
        name,typ=files[self.path]
        with open(os.path.join(os.path.dirname(__file__),'static',name),'rb') as f:
            self.send(200,f.read(),typ)

    def do_POST(self):
        try:
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                raise Fault('Expected application/json',415)
            size=int(self.headers.get('Content-Length','0'))
            if size<=0 or size>1500000:raise Fault('Invalid body size',413)
            data=json.loads(self.rfile.read(size).decode('utf-8'))
            if not isinstance(data,dict):raise Fault('Expected a JSON object')
            app=self.server.app
            if self.path=='/mcp':
                from .mcp import respond
                admin=hmac.compare_digest(self.headers.get('Authorization','').encode(),('Bearer '+app.config['admin_token']).encode())
                reply=respond(app,data,admin)
                return self.send(202 if reply is None else 200,reply)
            if self.path=='/v1/pair/begin':
                result=app.begin_pair(data['public_key'],data['label'])
            elif self.path=='/v1/pair/finish':
                result=app.finish_pair(data)
            elif self.path=='/v1/call':
                token=self.headers.get('Authorization','')
                admin=hmac.compare_digest(token.encode(),('Bearer '+app.config['admin_token']).encode())
                auth=None if admin else data.get('auth')
                if not admin and not isinstance(auth,dict):raise Fault('Credentials required',401)
                result=app.call(data['operation'],data.get('arguments',{}),data.get('id'),
                                data.get('realm','site'),auth, bool(data.get('cached',False) and admin))
            else:raise Fault('Unknown path',404)
            self.send(200,{'result':result})
        except Fault as e:self.send(e.status,{'error':str(e)})
        except (KeyError,ValueError,TypeError,IndexError) as e:self.send(400,{'error':'Invalid request: '+str(e)})
        except Exception:
            log.exception('request failure')
            self.send(500,{'error':'Internal service error'})

    def log_message(self,fmt,*args):
        log.debug(fmt,*args)


def server(app,host,port,cert=None,key=None):
    if host not in ('127.0.0.1','localhost','::1') and not (cert and key):
        raise Fault('Remote listeners require --cert and --key; loopback also supports an SSH tunnel')
    cls=Server
    if ':' in host:
        class IPv6Server(Server):
            address_family=socket.AF_INET6
        cls=IPv6Server
    http=cls((host,port),Handler)
    http.app=app
    if cert:
        context=ssl.SSLContext(getattr(ssl,'PROTOCOL_TLS_SERVER',ssl.PROTOCOL_SSLv23))
        context.options |= getattr(ssl,'OP_NO_SSLv2',0) | getattr(ssl,'OP_NO_SSLv3',0)
        context.load_cert_chain(cert,key);http.socket=context.wrap_socket(http.socket,server_side=True)
    return http


def background(app,stop,realm):
    while not stop.is_set():
        try:
            moved=app.tick(realm)
            app.health[realm]='ready'
            stop.wait(0.03 if moved else 0.6)
        except Exception as e:
            if app.health.get(realm)!=str(e):log.warning('%s: %s',realm,e)
            app.health[realm]=str(e)
            stop.wait(3)
