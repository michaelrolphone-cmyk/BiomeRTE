"""Persistent gateway routes learned from authenticated device sessions."""
import ipaddress,json,sqlite3,time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit

class DeviceRoutes:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS routes(device_id TEXT PRIMARY KEY,host TEXT NOT NULL,revision INTEGER NOT NULL,observed_at REAL NOT NULL,identity TEXT NOT NULL)")
    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=10)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def observe(self,device_id,host,identity=None):
        if not isinstance(device_id,str) or not device_id.strip():raise ValueError("device identity required")
        host=str(ipaddress.ip_address(host))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old=db.execute("SELECT * FROM routes WHERE device_id=?",(device_id,)).fetchone()
            metadata=dict(json.loads(old["identity"]) if old else {})
            for key,value in (identity or {}).items():
                if key in metadata and metadata[key]!=value:raise ValueError("device identity changed")
                metadata[key]=value
            revision=(old["revision"]+(old["host"]!=host)) if old else 1
            db.execute("INSERT OR REPLACE INTO routes VALUES(?,?,?,?,?)",(device_id,host,revision,time.time(),json.dumps(metadata,sort_keys=True)))
        return self.get(device_id)
    def get(self,device_id):
        with self.connect() as db:
            row=db.execute("SELECT * FROM routes WHERE device_id=?",(device_id,)).fetchone()
        if row is None:return None
        result=dict(row);result["identity"]=json.loads(result["identity"])
        return result
    def list(self):
        with self.connect() as db:ids=[r[0] for r in db.execute("SELECT device_id FROM routes ORDER BY device_id")]
        return [self.get(key) for key in ids]

def resolve_url(url,config):
    path=config.get("route_registry");identity=config.get("device_id")
    if not path or not identity:return url
    row=DeviceRoutes(path).get(identity)
    if row is None:return url
    parsed=urlsplit(url);host=row["host"]
    if ":" in host:host="["+host+"]"
    if parsed.port:host+=":"+str(parsed.port)
    if "@" in parsed.netloc:host=parsed.netloc.rsplit("@",1)[0]+"@"+host
    return urlunsplit(parsed._replace(netloc=host))


def refresh_neighbors(registry,neighbors):
    def mac(value):return ":".join(part.zfill(2) for part in value.lower().split(":"))
    changed=[]
    for record in registry.list():
        expected=record["identity"].get("mac")
        if not expected:continue
        candidates=[host for host,address in neighbors if mac(address)==mac(expected)]
        if record["host"] not in candidates and len(set(candidates))==1:
            changed.append(registry.observe(record["device_id"],candidates[0]))
    return changed


def provision_routes(registry,configuration):
    """Reconfigure only changed device/gateway routes; never poll sensor readings."""
    import socket
    from urllib.request import Request,urlopen
    results=[]
    for device_id,filename in configuration.items():
        record=registry.get(device_id)
        if record is None:continue
        path=Path(filename);before=path.read_bytes();config=json.loads(before)
        if config["receiver_id"]!=device_id:raise ValueError("provisioning identity mismatch")
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as wire:
            wire.connect((record["host"],9));local=wire.getsockname()[0]
        if config.get("gateway_host")==local and config.get("device_host")==record["host"]:continue
        desired=dict(host=local,port=config["port"],token=config["token"])
        url="http://"+record["host"]+"/api/telemetry/stream"
        try:
            # A previous POST may have applied despite a lost reply. Read configuration first.
            status=json.loads(urlopen(url,timeout=3).read())
            if not status.get("configured") or status.get("host")!=local or status.get("port")!=desired["port"]:
                request=Request(url,data=json.dumps(desired).encode(),headers={"Content-Type":"application/json"},method="POST")
                status=json.loads(urlopen(request,timeout=3).read())
            if not status.get("configured") or status.get("host")!=local or status.get("port")!=desired["port"]:
                raise ValueError("device route configuration unconfirmed")
            if path.read_bytes()!=before:continue
            config.update(gateway_host=local,device_host=record["host"])
            temporary=path.with_suffix(".route-new");temporary.write_text(json.dumps(config,indent=2));temporary.chmod(0o600);temporary.replace(path)
            results.append(dict(device_id=device_id,state="configured",host=record["host"],gateway_host=local))
        except Exception as error:
            results.append(dict(device_id=device_id,state="pending",error=type(error).__name__))
    return results
