"""Independent publication recovery; never starts or stops terminal commands."""
import fcntl
import json
import logging
import os
import sys
import time
from mac_provider import ROOT, Remote
from terminal_adapter import recover_finished
sys.path.insert(0, str(ROOT / 'runtime'))


def main():
    os.umask(0o077)
    with (ROOT / 'terminal-receipt-recovery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                cfg = json.loads((ROOT / 'terminal-manifest.json').read_text())
                recover_finished(Remote(cfg), cfg)
            except Exception as exc:
                logging.warning('Terminal receipt publication paused: %s', type(exc).__name__)
            time.sleep(5)


if __name__ == '__main__': main()
