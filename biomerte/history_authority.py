"""Page-local memoization of exact, settled recovery reads; never cache writes."""
import copy
from .store import encode

class AuthorityPage:
    def __init__(self,remote):
        self.remote=remote;self.values={};self.bytes=0
    def __getattr__(self,name):return getattr(self.remote,name)
    def call(self,name,args):
        eligible=(name=='cap.status' and isinstance(args,dict) and set(args)=={'operation','recovery'}
                  and args.get('recovery') is True and isinstance(args.get('operation'),str))
        operation=args['operation'] if eligible else None
        if eligible and operation in self.values:return copy.deepcopy(self.values[operation])
        value=self.remote.call(name,args)
        if (eligible and isinstance(value,dict) and value.get('id')==operation and
                value.get('provider')==self.remote.node and value.get('state') in ('succeeded','failed') and
                len(self.values)<64):
            size=len(encode(value).encode('utf-8'))
            if self.bytes+size<=8388608:
                self.values[operation]=copy.deepcopy(value);self.bytes+=size
        return value

def scoped_worker(worker):
    clone=copy.copy(worker)
    clone.remote=AuthorityPage(worker.remote)
    return clone
