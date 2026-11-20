"""Exercise the real daemon/socket/receipt path with isolated state and no devices."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

RADIO = Path(__file__).resolve().parents[1]
ROOT = RADIO.parents[2]
sys.path.insert(0, str(RADIO))
from entrypoint import wait_for_ready

kind = os.environ["BIOMERTE_RADIO_KIND"]
server = ROOT / "providers" / ("pi-" + kind) / "server.py"
with tempfile.TemporaryDirectory() as temporary:
    home, state = Path(temporary) / "provider", Path(temporary) / "state"
    home.mkdir()
    state.mkdir()
    if kind == "sdr":
        (state / "monitor.json").write_text('{"enabled":false,"revision":0}')
    read_fd, write_fd = os.pipe()
    child = subprocess.Popen([sys.executable, str(server), "serve", str(home), str(state)],
                             env=dict(os.environ, BIOMERTE_RADIO_READY_FD=str(write_fd)),
                             pass_fds=(write_fd,))
    os.close(write_fd)
    try:
        wait_for_ready(child, read_fd, [False])
        def call(operation, inputs):
            p = subprocess.run([sys.executable, str(server), "call", str(home)],
                               input=json.dumps({"operation":operation,"inputs":inputs}),
                               text=True, capture_output=True, check=True)
            return json.loads(p.stdout)
        first = call("isolated-status", {"action":"status"})
        assert first["ok"], first
        assert call("isolated-status", {"action":"status"}) == first
        conflict = call("isolated-status", {"action":"invalid"})
        assert not conflict["ok"], conflict
        print(json.dumps({"kind":kind,"real_daemon_ready":True,"status_ok":True,
                          "receipt_replay":True,"conflicting_replay_rejected":True,
                          "hardware_exercised":False,"isolated_state":True}))
    finally:
        os.close(read_fd)
        child.terminate()
        child.wait()
