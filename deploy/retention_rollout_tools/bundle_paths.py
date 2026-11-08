"""Rebind only caller-verified private bundle roots, preserving production paths."""
import os
from retention_rollout import verify_bundle_paths

def rebase_spec(spec, old_roots, new_root):
    if not os.path.isabs(new_root) or any(not os.path.isabs(p) for p in old_roots):
        raise ValueError('Bundle roots must be absolute')
    roots=sorted(set(old_roots),key=len,reverse=True)
    def walk(value):
        if isinstance(value,str):
            for root in roots:
                if value==root or value.startswith(root+'/'):
                    return new_root+value[len(root):]
            return value
        if isinstance(value,list):return [walk(x) for x in value]
        if isinstance(value,dict):return {k:walk(v) for k,v in value.items()}
        return value
    result=walk(spec)
    verify_bundle_paths(result,new_root)
    return result

def transport_chunks(encoded, limit=3000):
    if limit<1 or limit>4096:raise ValueError('Invalid native argument bound')
    return [encoded[i:i+limit] for i in range(0,len(encoded),limit)]
