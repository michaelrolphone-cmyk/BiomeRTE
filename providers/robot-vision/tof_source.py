"""Latest FTM solution; no RSSI fingerprint model and no implicit world alignment."""
import json
import math
from pathlib import Path
import time

def _clean(value):
    if isinstance(value, dict):
        return {k:_clean(v) for k,v in value.items()
                if k not in ("rssi_dbm", "rssi_filtered_dbm")}
    if isinstance(value,list): return [_clean(v) for v in value]
    return value

def read_tof(path, *, now=None):
    try:
        source=Path(path)
        if source.stat().st_size>262144: raise ValueError("oversized")
        data=json.loads(source.read_text(),parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if not isinstance(data,dict) or data.get("schema")!="rover.tof.solution/1":
            raise ValueError("schema")
        updated=float(data["updated_at"])
        age=(time.time() if now is None else now)-updated
        if not math.isfinite(age) or age < -1: raise ValueError("clock")
        data=_clean(data)
        data["collector_age_s"]=max(0,age)
        data["sample_reference"]="asynchronous_ftm_not_camera_capture"
        data["navigation_usable"]=False
        if age>5:
            data["valid"]=False
            data["status"]="collector_stale"
            data["position"]=None
            data["position_hypotheses"]=[]
        return data
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        return {"schema":"rover.tof.solution/1","status":"collector_unavailable",
                "valid":False,"position":None,"navigation_usable":False}
