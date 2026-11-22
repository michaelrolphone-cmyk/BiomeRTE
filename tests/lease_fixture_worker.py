"""Provider process for the real Docker lifecycle qualification."""
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path

from biomerte.worker import Worker


class Remote:
    """Forward provider RPCs to the signed test transport."""
    realm = "site"
    site = os.environ["FIXTURE_SITE"]
    node = os.environ["FIXTURE_PROVIDER"]

    def call(self, name, args, rid=None):
        request = Path("/transport") / uuid.uuid4().hex
        request.with_suffix(".tmp").write_text(json.dumps([name, args, rid or request.name]))
        request.with_suffix(".tmp").replace(request.with_suffix(".request"))
        reply = request.with_suffix(".reply")
        while not reply.exists():
            time.sleep(.02)
        data = json.loads(reply.read_text())
        reply.unlink()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]


if __name__ == "__main__":
    if sys.argv[1:] == ["execute"]:
        print(json.dumps({"system": platform.system(), "container": platform.node()}))
    else:
        adapter = dict(contract="docker.fixture:1", resource=os.environ["FIXTURE_RESOURCE"],
                       argv=[sys.executable, __file__, "execute"])
        worker = Worker("/lease-tmp/worker.sqlite", Remote(), [adapter])
        while True:
            worker.once()
            Path("/lease-tmp/ready").touch()
            time.sleep(.1)
