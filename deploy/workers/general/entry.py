"""Launch a specific implementation; never re-enroll its existing identity."""
import json, os, runpy, sys, time
from pathlib import Path

ROOT=Path("/opt/biome")
MODES={
 "gitlab":("providers/gitlab/proxy.py","worker"),
 "workspaces":("integrations/workspaces/provider.py","worker"),
 "workspace-monitor":("integrations/workspaces/provider.py","monitor"),
 "docker-cli":("providers/mac-docker/mac_docker.py","serve"),
 "storage":("providers/mac-storage/provider.py","serve"),
 "private-chat":("providers/private-chat/private_chat.py","serve"),
 "private-image":("providers/private-image/private_image.py","serve"),
}
def main():
    os.umask(0o077)
    kind=sys.argv[1]
    if kind in ("private-chat","private-image"):
        # Mac process IDs and native fcntl admission cannot be interpreted from
        # Docker Desktop's Linux VM. Never bypass native model-memory custody.
        if sys.platform!="darwin" and os.environ.get("BIOME_MODEL_RUNTIME_DOMAIN")!="isolated-linux":
            raise SystemExit("Private inference requires a qualified isolated Linux model runtime; native Mac admission is not a shared PID domain.")
    if kind=="replica":
        path=ROOT/"providers/mac-storage/backup.py"
        sys.path.insert(0,str(path.parent))
        import backup
        while True:
            backup.sync()
            Path(os.environ["BIOME_WORKER_HEALTH"]).write_text(json.dumps({"last_poll":time.time(),"state":"online"}))
            time.sleep(300)
    path,mode=MODES[kind]
    path=ROOT/path
    sys.path.insert(0,str(path.parent))
    sys.argv=[str(path),mode]
    runpy.run_path(str(path),run_name="__main__")
if __name__=="__main__": main()
