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
        # Native model custody stays on the Mac. Never interpret its PID queue
        # or fcntl lock in Docker Desktop's separate Linux kernel.
        config=os.environ.get("BIOME_MODEL_HOST_IO_CONFIG")
        if not config:
            raise SystemExit("Private workers require the configured native Mac model bridge.")
        from biomerte.host_io_client import HostIOClient
        from biomerte.model_host_io import require_model_transport
        require_model_transport(HostIOClient.from_config(config))
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
