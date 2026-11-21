"""Health means successful worker polling, not merely a live Python process."""
import json, os, time
from pathlib import Path
p=Path(os.environ.get("BIOME_WORKER_HEALTH","/run/biome-health/poll.json"))
try:
    state=json.loads(p.read_text())
    healthy=state.get("state")=="online" and 0 <= time.time()-state["last_poll"] < 180
except (OSError,ValueError,KeyError,TypeError):
    healthy=False
raise SystemExit(0 if healthy else 1)
