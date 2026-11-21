"""Read native mounted-volume identity and capacity without mounting or writing."""
import importlib.util
from pathlib import Path
import os

def status(settings):
    policy=settings.get("storage",{})
    if policy.get("enabled") is not True:
        raise PermissionError("native storage observation is disabled")
    path=Path(policy.get("module",""))
    if not path.is_absolute() or not path.is_file():
        raise ValueError("native storage module must be an existing absolute file")
    spec=importlib.util.spec_from_file_location("_biome_native_volume_observer",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.mounted():
        return {"mounted":False}
    # Native volume preparation is deliberately never called by this observer.
    info=os.statvfs(module.MOUNT)
    image=module.IMAGE.stat()
    vault=module.VAULT.stat()
    return {"mounted":True,"workspace_path":str(module.MOUNT),
            "workspace_uuid":(module.MOUNT/module.MARK).read_text(),
            "workspace_reserved_bytes":module.WORKSPACE,"sacred_reserved_bytes":module.SACRED,
            "workspace_allocated_bytes":image.st_blocks*512,
            "sacred_allocated_bytes":vault.st_blocks*512,
            "usable_bytes":info.f_blocks*info.f_frsize,
            "free_bytes":info.f_bavail*info.f_frsize}
