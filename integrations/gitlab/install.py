#!/usr/bin/env python3
"""Install the already-authorized Mac GitLab capability provider; preserve credentials/data."""
import json,os,pathlib,plistlib,shutil,subprocess,sys
root=pathlib.Path.home()/".local/share/biome-gitlab";root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
src=pathlib.Path(__file__).resolve().parent
shutil.copy2(src/"proxy.py",root/"proxy.py")
url="https://Micahels-Mac-mini.local:8443"
(root/"config.json").write_text(json.dumps({"url":url,"ca":str(root/"tls/gitlab.crt")}))
python=sys.executable
(root/"credential_helper.sh").write_text('#!/bin/sh\n[ "$1" = get ] || exit 0\nexec '+__import__('shlex').quote(python)+' '+__import__('shlex').quote(str(root/"proxy.py"))+' credential\n')
(root/"credential_helper.sh").chmod(0o700)
label="org.biomerte.gitlab-provider"
plist={"Label":label,"ProgramArguments":[python,str(root/"proxy.py"),"worker"],"RunAtLoad":True,"KeepAlive":True,"WorkingDirectory":str(root),"EnvironmentVariables":{"PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},"StandardOutPath":str(root/"provider.log"),"StandardErrorPath":str(root/"provider-error.log")}
path=pathlib.Path.home()/"Library/LaunchAgents"/(label+".plist");path.parent.mkdir(parents=True,exist_ok=True)
with path.open("wb") as out:plistlib.dump(plist,out)
subprocess.run([python,str(root/"proxy.py"),"configure"],check=True)
domain="gui/"+str(os.getuid())
subprocess.run(["launchctl","bootout",domain+"/"+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
subprocess.run(["launchctl","bootstrap",domain,str(path)],check=True)
print("Installed "+label)
