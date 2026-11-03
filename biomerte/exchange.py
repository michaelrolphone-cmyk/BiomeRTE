"""Authenticated portable packages, bounded catch-up and scoped offline child work."""
from .api import op, ident
from .store import Fault, uid, digest
from .identity import verify, signed, fingerprint
from .knowledge import revision
from . import process
from .work import ready


@op('peer.trust', 'public_key:str label:str scopes:list')
def trust(tx, ctx, public_key, label, scopes):
    """Commission an independently verified peer key with explicit scopes."""
    fp = fingerprint(public_key)
    if not all(isinstance(x,str) for x in scopes):
        raise Fault('Invalid peer scopes')
    return tx.put('peers',fp,{'id':fp,'key':public_key,'label':label,'scopes':scopes,'enabled':True})


@op('peer.revoke', 'peer:str')
def revoke(tx, ctx, peer):
    p=tx.need('peers',peer)
    p['enabled']=False
    return tx.put('peers',peer,p)


def bundle(ctx, kind, to, data):
    p={'format':1,'site':ctx.app.site,'sender':ctx.app.node,'to':to,'kind':kind,'data':data}
    return signed(ctx.app.key,p)


def unpack(tx,ctx,packet,kind,scope):
    p=packet['payload']
    if p['format']!=1 or p['kind']!=kind or p['to']!=ctx.app.node:
        raise Fault('Wrong package destination or type')
    peer=tx.need('peers',p['sender'])
    if not peer['enabled'] or scope not in peer['scopes']:
        raise Fault('Peer lacks exchange authority',403)
    verify(peer['key'],packet['signature'],p)
    return p


@op('sync.export', 'to:str cursors?:obj limit?:int')
def export(tx,ctx,to,cursors=None,limit=32):
    """Export knowledge revisions in causal order. Receiver persists its own writer cursors."""
    cursors=dict(cursors or {})
    rows=tx.rows('revisions')
    rows.sort(key=lambda r:(r['writer'],r['seq']))
    remaining=[r for r in rows if r['seq']>cursors.get(r['writer'],0)]
    byid={r['rev']:r for r in rows}
    page=[]
    while remaining and len(page)<max(1,min(64,limit)):
        for i,r in enumerate(remaining):
            if r['seq']!=cursors.get(r['writer'],0)+1:
                continue
            if all(byid[k]['seq']<=cursors.get(byid[k]['writer'],0) for k in r['parents']):
                page.append(r);cursors[r['writer']]=r['seq'];remaining.pop(i)
                break
        else:
            raise Fault('Missing writer range or causal prerequisite',409)
    return bundle(ctx,'revisions',to,{'records':page,'more':bool(remaining)})


@op('sync.cursors',write=False)
def cursors(tx,ctx):
    result={}
    for r in tx.rows('revisions'):
        result[r['writer']]=max(result.get(r['writer'],0),r['seq'])
    return result


@op('sync.import','packet:obj')
def receive(tx,ctx,packet):
    p=unpack(tx,ctx,packet,'revisions','sync')
    counts=cursors(tx,ctx);applied=0
    for r in p['data']['records']:
        if tx.get('revisions',r['rev']):
            continue
        author = r['writer'].split(':')[0]
        source = tx.need('peers', author) if author != ctx.app.node else {'key':ctx.app.config['public_key'],'enabled':True,'scopes':['sync']}
        if not source['enabled'] or 'sync' not in source['scopes']:
            raise Fault('Untrusted revision author',403)
        verify(source['key'], r['signature'], {'revision':r['rev'],'writer':r['writer']})
        if r['seq']!=counts.get(r['writer'],0)+1:
            raise Fault('Writer sequence gap, fork, or local writer impersonation',409)
        revision(tx,r);counts[r['writer']]=r['seq'];applied+=1
        data=r['body']
        if r['type']=='mail' and 'sync.mail' in source['scopes'] and r['key']=='message.'+data['id']:
            key=digest([author,data['id']]);msg=dict(data,id=key,origin=author,read=False)
            if not tx.get('messages',key):
                tx.put('messages',key,msg);tx.emit('message.received',{'message':key,'to':msg['to']})
        if r['type']=='bus_event' and 'sync.events' in source['scopes'] and r['key']=='event.'+data['id'] and data['topic'].startswith('user.'):
            tx.emit(data['topic'],{'origin':author,'event':data['id'],'body':data['payload']})
    if applied:
        tx.emit('sync.received',{'peer':p['sender'],'count':applied})
    return {'applied':applied,'cursors':counts,'more':p['data']['more']}


@op('offline.delegate','task:str process:str inputs:obj to:str contracts:list')
def delegate(tx,ctx,task,process,inputs,to,contracts):
    """Reserve a task and package executable revisions for one identified offline node."""
    item=tx.need('work',task)
    if not ready(tx,item):
        raise Fault('Task unavailable',409)
    peer=tx.need('peers',to)
    if not peer['enabled'] or 'return' not in peer['scopes']:
        raise Fault('Offline worker not commissioned',403)
    definition=tx.need('definitions',process)
    versions={}
    def collect(rev):
        if rev in versions:
            return
        v=tx.need('process_versions',rev)
        versions[rev]=v
        for n in v['graph']['nodes'].values():
            if n['type']=='service' and n.get('contract') and n['contract'] not in contracts:
                raise Fault('Undelegated capability: '+n['contract'])
            if n['type']=='service' and n.get('operation'):
                raise Fault('Offline packages use declared capability calls')
            if n['type']=='select':
                raise Fault('An offline package cannot select unrelated work')
        for sub in v['calls'].values():collect(sub)
    collect(definition['rev'])
    item.update(state='delegated',revision=item['revision']+1)
    validate_inputs = tx.need('process_versions',definition['rev'])['graph'].get('inputs',{})
    from .api import schema,validate
    validate(schema(validate_inputs),inputs)
    grant={'id':uid(),'task':task,'revision':item['revision'],'rev':definition['rev'],
           'name':process,'inputs':inputs,'contracts':contracts,'versions':list(versions.values())}
    tx.put('grants',grant['id'],{'id':grant['id'],'to':to,'task':task,'state':'issued','rev':grant['rev']})
    tx.put('work',task,item)
    return bundle(ctx,'grant',to,grant)


@op('offline.accept','packet:obj')
def accept(tx,ctx,packet):
    if ctx.scope!='local':
        raise Fault('Use the local realm for disconnected execution')
    p=unpack(tx,ctx,packet,'grant','delegate')
    g=p['data']
    permitted=tx.need('peers',p['sender'])['scopes']
    if any('delegate:'+cap not in permitted for cap in g['contracts']):
        raise Fault('Issuer lacks authority to delegate these local capabilities',403)
    key=digest([p['sender'],g['id']]);old=tx.get('incoming',key)
    if old:return old
    for v in g['versions']:
        process.validate_graph(v['graph'])
        if digest([v['name'],v['graph'],v['calls']])!=v['id']:
            raise Fault('Invalid packaged definition')
        for n in v['graph']['nodes'].values():
            if n['type']=='select' or (n['type']=='service' and
                    (n.get('operation') or n['contract'] not in g['contracts'])):
                raise Fault('Offline package exceeds allowed local actions',403)
        tx.put('process_versions',v['id'],v)
    r=process.new_run(tx,g['name'],g['inputs'],pin=g['rev'])
    rec={'id':key,'grant':g['id'],'issuer':p['sender'],'run':r['id'],'rev':g['rev']}
    return tx.put('incoming',key,rec)


@op('offline.result','incoming:str')
def report(tx,ctx,incoming):
    rec=tx.need('incoming',incoming)
    r=tx.need('runs',rec['run'])
    if r['state']=='running':raise Fault('Run is still active',409)
    return bundle(ctx,'outcome',rec['issuer'],{'grant':rec['grant'],'rev':r['rev'],
                  'run':r['id'],'state':r['state'],'result':r['result'],'trace':r['trace']})


@op('offline.reconcile','packet:obj')
def reconcile(tx,ctx,packet):
    p=unpack(tx,ctx,packet,'outcome','return');data=p['data'];g=tx.need('grants',data['grant'])
    if g['to']!=p['sender'] or g['rev']!=data['rev']:raise Fault('Grant binding mismatch')
    if g['state']=='returned':
        if g['outcome']!=data:raise Fault('Conflicting outcome',409)
        return g
    item=tx.need('work',g['task'])
    item['attempts'].append(data['run'])
    item.update(state='review' if data['state']=='completed' else 'open',revision=item['revision']+1)
    tx.put('work',item['id'],item)
    g.update(state='returned',outcome=data)
    tx.emit('offline.returned',{'grant':g['id'],'task':g['task']})
    return tx.put('grants',g['id'],g)
