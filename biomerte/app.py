"""Site initialization, service context, enrollment and signed calls."""
import contextlib
import hmac
import json
import os
import time
from .api import Context, OPERATIONS, command, op, ident
from .store import Fault, Store, Etcd, uid, encode, mkdir, digest, text
from .identity import secret, write_private, generate, fingerprint, verify
from . import knowledge, work, process, providers, exchange, extensions


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def initialize(directory, site=None):
    """Create private node material once. Each clone gets a new identity."""
    mkdir(directory)
    path = os.path.join(directory, 'node.json')
    if os.path.isfile(path):
        return read_json(path)
    key = os.path.join(directory, 'node.key')
    if os.path.exists(key):
        # Recover interrupted init using the same private identity.
        from .identity import openssl
        pub = openssl(['rsa','-in',key,'-pubout']).decode('ascii')
    else:
        pub = generate(key)
    conf = {'format':1,'site':ident(site or uid()),'node':fingerprint(pub),'public_key':pub,
            'incarnation':uid(),'admin_token':secret(),'listen':'127.0.0.1','port':8740}
    write_private(path, (json.dumps(conf, indent=2)+'\n').encode())
    return conf


def seed(tx):
    if not tx.get('_meta','initialized'):
        ctx = Context(None)
        providers.define(tx,ctx,'core.echo',{'value':'any'},{'value':'any'},description='Return the input value.')
        providers.offer(tx,ctx,'builtin','core.echo:1','builtin.echo')
        tx.put('_meta','initialized',1)


class Application:
    def __init__(self,directory):
        self.directory = os.path.abspath(directory)
        self.config = read_json(os.path.join(directory,'node.json'))
        self.node,self.site = self.config['node'],self.config['site']
        self.key = os.path.join(directory,'node.key')
        self.stores = {'local':Store(directory,'local'),
                       'site':Store(directory,'cluster-'+self.site if self.config.get('etcd') else 'site',Etcd(self.config['etcd'],self.site)
                                    if self.config.get('etcd') else None)}
        self.timer_anchors = {}
        self.health = {}
        for scope, store in self.stores.items():
            try:
                store.execute(seed)
                self.health[scope]='ready'
            except Fault as e:
                self.health[scope]=str(e)
                if scope=='local' or e.status!=503:raise

    def call(self,name,args=None,request_id=None,realm='site',auth=None,cached=False):
        if realm not in self.stores:raise Fault('Unknown realm')
        args = {} if args is None else args
        def transaction(tx):
            ctx=Context(self,scope=realm)
            if auth is not None:
                peer=tx.need('peers',auth['actor'])
                if not peer['enabled']:raise Fault('Revoked credential',403)
                envelope={'site':self.site,'realm':realm,'operation':name,'arguments':args,'id':request_id}
                verify(peer['key'],auth['signature'],envelope)
                ctx=Context(self,peer['id'],realm,peer['scopes'])
                if name.startswith('peer.') or name.startswith('pair.') or name=='system.snapshot':
                    raise Fault('Operator action',403)
            return command(tx,ctx,name,args,request_id)
        return self.stores[realm].execute(transaction,cached=cached)

    def tick(self,realm):
        def run(tx):
            seed(tx)
            return process.tick(tx,Context(self,scope=realm))
        return self.stores[realm].execute(run)

    def begin_pair(self,pub,label):
        key=fingerprint(pub)
        def run(tx):
            prior=tx.get('candidates',key)
            if prior and prior['state']=='pending':
                return {'challenge':prior['challenge'],'site':self.site,'key':key}
            if len([r for r in tx.rows('candidates') if r['state']=='pending'])>=64:
                raise Fault('Commissioning inbox full',429)
            record={'id':key,'key':pub,'label':text(label),'challenge':secret(),'state':'pending'}
            tx.put('candidates',key,record)
            return {'challenge':record['challenge'],'site':self.site,'key':key}
        return self.stores['site'].execute(run)

    def finish_pair(self,proof):
        def run(tx):
            p = tx.need('candidates',proof['key'])
            if p['state'] not in ('approved','enrolled'):
                raise Fault('Verify device fingerprint on the commissioning link first',403)
            msg={'challenge':p['challenge'],'site':self.site,'key':p['id']}
            verify(p['key'],proof['signature'],msg)
            if p['state']=='approved':
                exchange.trust(tx,Context(self),p['key'],p['label'],p['scopes'])
                p['state']='enrolled';tx.put('candidates',p['id'],p)
            if not tx.need('peers',p['id'])['enabled']:raise Fault('Revoked',403)
            return {'site':self.site,'credential':p['id'],'scopes':p['scopes']}
        return self.stores['site'].execute(run)


@op('pair.list',write=False)
def candidates(tx,ctx):
    return [{k:r[k] for k in ('id','label','state')} for r in tx.rows('candidates')]


@op('pair.approve','fingerprint:str scopes:list')
def approve(tx,ctx,fingerprint,scopes):
    r=tx.need('candidates',fingerprint)
    if r['state']!='pending':raise Fault('Candidate no longer pending',409)
    if not all(isinstance(s,str) for s in scopes):raise Fault('Invalid scope')
    r.update(state='approved',scopes=scopes)
    tx.put('candidates',fingerprint,r)
    return {'id':fingerprint,'state':'approved'}


@op('system.info',write=False)
def info(tx,ctx):
    return {'node':ctx.app.node,'site':ctx.app.site,'realm':ctx.scope,'version':'0.1.0',
            'shared_backend':'etcd' if ctx.app.stores['site'].remote else 'sqlite',
            'position':tx.db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0]}


@op('system.snapshot',write=False)
def snapshot(tx,ctx):
    return {'site':info(tx,ctx),'work':work.listing(tx,ctx),'process':process.listing(tx,ctx),
            'waits':process.waits(tx,ctx),'capabilities':providers.discover(tx,ctx),
            'leases':tx.rows('leases'),'documents':tx.rows('documents'),
            'mission':tx.get('mission','current',{}),'inbox':tx.rows('messages')[-50:],
            'subscriptions':tx.rows('subscriptions'),'candidates':candidates(tx,ctx)}


@op('demo.run','message?:str')
def demo(tx,ctx,message='Hello from BiomeRTE'):
    """Create a full goal-to-task example, pin a routine, start it and return real IDs."""
    g=work.create(tx,ctx,'goal','Demonstrate a portable work cycle')
    m=work.create(tx,ctx,'milestone','Collect a provider result',parent=g['id'])
    w=work.create(tx,ctx,'workflow','Run and retain',parent=m['id'])
    t=work.create(tx,ctx,'task','Echo a message and save it',parent=w['id'],criteria=['stored result'])
    steps=[{'type':'service','contract':'core.echo:1','args':{'value':{'$':'input.message'}},'save':'reply'},
           {'type':'service','operation':'knowledge.put','args':{'key':{'$':'input.key'},
              'body':{'$':'reply.value'},'links':[t['id']]},'save':'doc'}]
    process.routine(tx,ctx,'demo.echo-and-save',steps,{'message':'str','key':'str'},{'revision':'str'},
                    {'revision':{'$':'doc.rev'}})
    r=process.start(tx,ctx,'demo.echo-and-save',{'message':message,'key':'demo-'+t['id']},t['id'])
    return {'goal':g['id'],'task':t['id'],'run':r['id'],'document':'demo-'+t['id']}
