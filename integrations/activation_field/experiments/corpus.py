import hashlib,json,random


def corpus(seed=42,count=200):
    rng=random.Random(seed);episodes=[]
    for i in range(count):
        pairs={f"item-{rng.getrandbits(48):012x}":f"value-{rng.getrandbits(32):08x}" for _ in range(10)}
        corrected=dict(pairs)
        for key in list(pairs)[:2]:corrected[key]=f"value-{rng.getrandbits(32):08x}"
        train='\n'.join(f"The value of {k} is {v}." for k,v in pairs.items())
        train+='\n'+ '\n'.join(f"Correction: the value of {k} is now {corrected[k]}." for k in list(pairs)[:2])
        probes=[dict(prompt=f"What is the value of {k}? Reply with just its value.",answer=v,corrected=k in list(pairs)[:2]) for k,v in corrected.items()]
        episodes.append(dict(id=i,split='development' if i<count//2 else 'heldout',training=train,answers=corrected,probes=probes))
    obj=dict(schema_version=1,seed=seed,episodes=episodes)
    obj['sha256']=hashlib.sha256(json.dumps(obj,sort_keys=True).encode()).hexdigest()
    return obj
