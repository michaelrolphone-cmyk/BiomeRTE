"""Patch custody policy into existing overlays without replacing other changes."""
import os,sys,tempfile

def main():
    source=sys.argv[1]
    edits=[
      ("    configuration = dict(configuration or {})\n    if lease_class:",
       "    key = digest([provider, contract, resource])\n    previous = tx.get('offers', key, {})\n    configuration = dict(previous.get('configuration', {}) if configuration is None else configuration)\n    if lease_class:"),
      ("        l.update(watch_until=a['watch_until'], last_progress_at=now, missed_heartbeats=0)",
       "        from .resource_recovery import protected\n        l.update(watch_until=a['watch_until'], last_progress_at=now, missed_heartbeats=0,\n                 execution_custody=protected(tx,l))")]
    prepared=[]
    for root in sys.argv[2:]:
        target=root+'/biomerte/providers.py'
        text=open(target).read()
        for old,new in edits:
            if new in text:continue
            if text.count(old)!=1:raise RuntimeError('Concurrent source conflict: '+target)
            text=text.replace(old,new)
        prepared.append((target,text.encode()))
        prepared.append((root+'/biomerte/resource_recovery.py',open(source+'/biomerte/resource_recovery.py','rb').read()))
    for path,data in prepared:compile(data,path,'exec')
    for path,data in prepared:
        fd,tmp=tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
        print('PATCHED',path,flush=True)

if __name__=='__main__':main()
