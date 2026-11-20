"""Health is successful polling or this worker's live native admitted call."""
import json
import os
from pathlib import Path
import time

def healthy():
    poll = Path(os.environ.get("BIOME_WORKER_HEALTH", "/run/biome-health/poll.json"))
    try:
        state = json.loads(poll.read_text())
        if state.get("state") == "online" and 0 <= time.time()-state["last_poll"] < 180:
            return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        request = json.loads(Path(os.environ["BIOME_PRIVATE_MODEL_HEALTH"]).read_text())
        # This is a Linux PID from the container receipt, never a native PID.
        os.kill(request["worker_pid"], 0)
        from biomerte.host_io_client import HostIOClient
        from biomerte.model_host_io import require_model_transport
        client = HostIOClient.from_config(os.environ["BIOME_MODEL_HOST_IO_CONFIG"])
        require_model_transport(client)
        status = client.call("model.status")
        return (status.get("runtime_domain") == "native-mac" and
                any(row["request_id"] == request["request_id"] for row in status.get("active", [])))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False

if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
