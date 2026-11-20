#!/usr/bin/env python3
"""Capture rollback evidence after the native solver has stopped."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
from verify import native_pids


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("deployment",type=Path)
    args=p.parse_args();plan=json.loads((args.deployment/"plan.json").read_text())
    if native_pids(plan["native"]["program"]):
        raise RuntimeError("Stop the native solver before capturing its final state")
    output=args.deployment/"rollback/state"
    output.mkdir(mode=0o700,exist_ok=False)
    evidence=[]
    for kind,names in plan["state_files"].items():
        directory=Path(plan["state_paths"][kind])
        if kind=="vision":
            names=list(names)+[p.name for p in directory.glob("attitude-world-reference*.json")]
        (output/kind).mkdir(mode=0o700)
        for name in names:
            source=directory/name
            if not source.is_file():continue
            raw=source.read_bytes();json.loads(raw)
            target=output/kind/name;target.write_bytes(raw);target.chmod(0o600)
            evidence.append(dict(role=kind,file=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw)))
    receipt=dict(schema="biomerte.tof-native-state-capture/1",captured_at=time.time(),
                 native_pids=[],state_reused_in_place=True,cross_file_atomic=False,
                 note="Native solver stopped; other vision inputs may continue updating. This is rollback evidence, not a distributed checkpoint.",
                 files=evidence)
    (output/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
    (output/"receipt.json").chmod(0o600)
    print(json.dumps(receipt))


if __name__=="__main__":main()
