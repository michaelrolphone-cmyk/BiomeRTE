"""Prove separately running OS services are outside a worker-only restart."""
import ctypes, json, os, stat, subprocess
import terminal_safety as safety

def dynamic_apple_signature(pid):
    cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec=ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    P=ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes=[P,ctypes.c_char_p,ctypes.c_uint32];cf.CFStringCreateWithCString.restype=P
    cf.CFNumberCreate.argtypes=[P,ctypes.c_int,P];cf.CFNumberCreate.restype=P
    cf.CFDictionaryCreateMutable.argtypes=[P,ctypes.c_long,P,P];cf.CFDictionaryCreateMutable.restype=P
    cf.CFDictionarySetValue.argtypes=[P,P,P];cf.CFRelease.argtypes=[P]
    sec.SecCodeCopyGuestWithAttributes.argtypes=[P,P,ctypes.c_uint32,ctypes.POINTER(P)]
    sec.SecRequirementCreateWithString.argtypes=[P,ctypes.c_uint32,ctypes.POINTER(P)]
    sec.SecCodeCheckValidity.argtypes=[P,ctypes.c_uint32,P]
    refs=[]
    try:
        key=cf.CFStringCreateWithCString(None,b'pid',0x08000100);refs.append(key)
        n=ctypes.c_int(pid);number=cf.CFNumberCreate(None,9,ctypes.byref(n));refs.append(number)
        d=cf.CFDictionaryCreateMutable(None,0,ctypes.byref(ctypes.c_byte.in_dll(cf,'kCFTypeDictionaryKeyCallBacks')),ctypes.byref(ctypes.c_byte.in_dll(cf,'kCFTypeDictionaryValueCallBacks')));refs.append(d)
        if not all(refs):return False
        cf.CFDictionarySetValue(d,key,number);code=P()
        if sec.SecCodeCopyGuestWithAttributes(None,d,0,ctypes.byref(code))!=0:return False
        refs.append(code)
        expr=cf.CFStringCreateWithCString(None,b'anchor apple',0x08000100);refs.append(expr);req=P()
        if not expr or sec.SecRequirementCreateWithString(expr,0,ctypes.byref(req))!=0:return False
        refs.append(req)
        return sec.SecCodeCheckValidity(code,0,req)==0
    finally:
        for ref in reversed(refs):
            if ref:cf.CFRelease(ref)

def executable(pid):
    lib=ctypes.CDLL('/usr/lib/libproc.dylib');lib.proc_pidpath.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32]
    buf=ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid,buf,len(buf))<=0:raise RuntimeError('Cannot identify loaded executable')
    return buf.value.decode('utf-8')

def proof_for(pid,snapshot,roots):
    p=snapshot['processes'].get(pid)
    if not p or p['ppid']!=1 or p['pgid']!=pid or p['state'].startswith('Z') or pid in roots:return None
    groups={snapshot['processes'][r]['pgid'] for r in roots if r in snapshot['processes']}
    if p['pgid'] in groups or [x['pid'] for x in snapshot['processes'].values() if x['pgid']==pid and not x['state'].startswith('Z')]!=[pid]:return None
    path=executable(pid)
    if not path.startswith('/System/Library/') or os.path.realpath(path)!=path:return None
    value=os.stat(path)
    if not stat.S_ISREG(value.st_mode) or value.st_uid!=0 or value.st_mode&0o022:return None
    uid=int(subprocess.check_output(['/bin/ps','-p',str(pid),'-o','uid=']).decode().strip())
    if not dynamic_apple_signature(pid):return None
    return {'pid':pid,'ppid':1,'pgid':pid,'started':p['started'],'uid':uid,'executable':path,
            'image_stat':[value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns],
            'validated_dynamic_apple':True}

def candidates(home,snapshot):
    db=safety.readonly(home+'/worker.sqlite');eligible=set();active=set()
    try:
        for state,body in db.execute('SELECT state,result FROM actions'):
            result=json.loads(body or '{}');evidence=result.get('evidence',{});identity=evidence.get('process_identity',{})
            if not isinstance(identity,dict):continue
            ids={identity.get('pid'),identity.get('pgid')}
            ids={n for n in ids if type(n) is int and n>0}
            matches={p['pid'] for p in snapshot['processes'].values() if p['pid'] in ids or p['pgid'] in ids}
            if state in ('succeeded','failed') and evidence.get('quiescent') is True:eligible.update(matches)
            else:active.update(matches)
    finally:db.close()
    return eligible-active

def collect(home,roots):
    snap=safety.process_snapshot('Darwin');out={}
    for pid in sorted(candidates(home,snap)):
        try:proof=proof_for(pid,snap,roots)
        except (OSError,ValueError,RuntimeError):proof=None
        if proof:out[pid]=proof
    return out

def verify(proofs,roots):
    snapshot=safety.process_snapshot('Darwin')
    for pid,expected in proofs.items():
        if proof_for(int(pid),snapshot,roots)!=expected:return False
    return True
