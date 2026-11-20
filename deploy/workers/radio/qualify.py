#!/usr/bin/env python3
"""Qualify an existing immutable radio image without devices, credentials or network."""
import argparse
import json
import re
import subprocess

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--docker",default="docker")
    p.add_argument("--image",required=True)
    p.add_argument("--kind",choices=("sdr","fm"),required=True)
    a=p.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}",a.image):
        p.error("qualify an immutable local image ID")
    prefix=[a.docker,"run","--rm","--network","none","--read-only",
            "--cap-drop","ALL","--security-opt","no-new-privileges",
            "--tmpfs","/tmp:rw,nosuid,noexec,size=64m,mode=1777"]
    checks=[
      ("dependency-and-source-check",prefix+[a.image,"check"]),
      ("real-worker-fixtures",prefix+["--entrypoint","python3",a.image,"-m","unittest","discover","-s","/app/providers/pi-"+a.kind,"-p","test*.py"]),
      ("container-boundaries",prefix+["--entrypoint","python3",a.image,"-m","unittest","discover","-s","/app/deploy/workers/radio/tests","-p","test*.py"]),
      ("real-daemon-and-receipts",prefix+["--entrypoint","python3",a.image,"/app/deploy/workers/radio/tests/daemon_smoke.py"]),
      ("provider-runtime",prefix+["--entrypoint","python3",a.image,"-m","biomerte","--help"])
    ]
    results=[]
    for name,argv in checks:
        print("Checking "+name,flush=True)
        result=subprocess.run(argv,text=True,capture_output=True)
        results.append({"check":name,"exit_code":result.returncode,
                        "stdout":result.stdout,"stderr":result.stderr})
        if result.returncode:
            print(json.dumps({"ok":False,"kind":a.kind,"image":a.image,"checks":results},indent=2))
            raise SystemExit(result.returncode)
    print(json.dumps({"ok":True,"kind":a.kind,"image":a.image,"checks":results,
                      "network":"none","devices":[],"hardware_qualified":False},indent=2))

if __name__=="__main__":
    main()
