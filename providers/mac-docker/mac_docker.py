"""Run the shared Docker CLI adapter through the installed Mac gateway client."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATE = HOME / '.local/share/biome-mac-docker'
TERMINAL = HOME / '.local/share/biome-mac-terminal'
LABEL = 'org.biomerte.mac-docker'
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parent / 'pi-docker'), str(TERMINAL)]
import mac_provider as gateway
from biomerte.worker import ActionPool, Worker
from docker_cli import CONTRACT, execute, usage


class Remote:
    realm = 'site'

    def __init__(self, cfg):
        self.node = cfg['provider']
        self.site = cfg['site']

    def call(self, name, args, rid=None):
        return gateway.invoke(name, args, rid, self.realm)


def offer(cfg, state):
    guide = usage()
    guide['before'] += (' This offer runs on %s with %d concurrent CLI leases. Detached containers '
                        'continue running after the CLI returns and the lease is released.'
                        % (cfg['host'], cfg['slots']))
    return dict(provider=cfg['provider'], contract=CONTRACT, resource=cfg['resource'],
                service='docker-cli', capacity=cfg['slots'], state=state, usage=guide,
                configuration={'execution_custody': True})


def daemon_ready(cfg):
    try:
        result = subprocess.run(['/usr/bin/curl', '--silent', '--fail', '--max-time', '4',
                                 '--unix-socket', cfg['socket'], 'http://localhost/version'],
                                capture_output=True, timeout=6)
        return result.returncode == 0 and bool(json.loads(result.stdout).get('Version'))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def serve(cfg):
    remote = Remote(cfg)
    local = dict(offer(cfg, 'ready'), argv=[sys.executable, str(HERE / 'mac_docker.py'), 'execute'])
    with open(STATE / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(str(STATE / 'worker.sqlite'), remote, [local], 5400)
        pool = ActionPool(worker, cfg['slots'])
        advertised = None
        checked = launch_after = 0
        stopping = False

        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        try:
            while not stopping:
                try:
                    now = time.monotonic()
                    if now >= checked:
                        ready = daemon_ready(cfg)
                        state = 'ready' if ready else 'offline'
                        if state != advertised:
                            remote.call('cap.offer', offer(cfg, state))
                            advertised = state
                        if not ready and cfg.get('desktop') and now >= launch_after:
                            subprocess.run(['/usr/bin/open', '-gja', 'Docker'], timeout=15, check=False)
                            launch_after = now + 120
                        checked = now + 10
                    if advertised == 'ready':
                        worker.once(pool)
                    time.sleep(2)
                except Exception as exc:
                    print('Worker retry:', type(exc).__name__, str(exc)[:240], file=sys.stderr, flush=True)
                    time.sleep(5)
        finally:
            try:
                remote.call('cap.offer', offer(cfg, 'offline'))
            finally:
                pool.executor.shutdown(wait=True)
                worker.db.close()


def install():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', default=str(HOME / '.docker/run/docker.sock'))
    parser.add_argument('--executable', default='/usr/local/bin/docker')
    parser.add_argument('--slots', type=int, help='Concurrent CLI calls (preserves the installed setting on update)')
    parser.add_argument('--external-engine', action='store_true')
    args = parser.parse_args(sys.argv[2:])
    previous = STATE / 'manifest.json'
    prior = json.loads(previous.read_text()) if previous.exists() else {}
    if args.slots is None:
        args.slots = prior.get('slots', min(64, 4 * (os.cpu_count() or 1)))
    if sys.platform != 'darwin' or not 1 <= args.slots <= 64:
        parser.error('Expected macOS and 1–64 slots')
    if not Path(args.executable).is_absolute() or not os.access(args.executable, os.X_OK):
        parser.error('Docker executable is unavailable')
    if not Path(args.socket).is_absolute():
        parser.error('Expected an absolute Unix socket path')
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    site = json.loads((TERMINAL / 'terminal-manifest.json').read_text())['site']
    node = json.loads((TERMINAL / 'identity/node.json').read_text())['node']
    provider = hashlib.sha256(('biomerte/mac/docker/v1/' + node).encode()).hexdigest()
    cfg = dict(site=site, provider=provider, resource='docker.mac.' + node[:20], host=socket.gethostname(),
               executable=args.executable, socket=args.socket, slots=args.slots, chunk_bytes=48*1024,
               desktop=not args.external_engine)
    for key in ('spool', 'client_config', 'workspace'):
        directory = STATE / key
        directory.mkdir(mode=0o700, exist_ok=True)
        cfg[key] = str(directory)
    if prior and prior['site'] != site:
        raise SystemExit('Controller site identity changed; reconcile the existing ledger before installation')
    target = 'gui/' + str(os.getuid())
    job = target + '/' + LABEL
    plist = HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, check=True)
    replacing = subprocess.run(['/bin/launchctl', 'print', job], capture_output=True).returncode == 0
    if replacing:
        subprocess.run(['/bin/launchctl', 'bootout', job], check=True)
    gateway.save_json(previous, cfg)
    spec = dict(Label=LABEL, ProgramArguments=[sys.executable, str(HERE / 'mac_docker.py'), 'serve'],
                WorkingDirectory=str(STATE), RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                EnvironmentVariables=dict(HOME=str(HOME), PATH=os.environ['PATH'], PYTHONUNBUFFERED='1'),
                StandardOutPath=str(STATE / 'worker.log'), StandardErrorPath=str(STATE / 'worker-error.log'))
    tmp = plist.with_suffix('.tmp')
    tmp.write_bytes(plistlib.dumps(spec))
    os.replace(tmp, plist)
    subprocess.run(['/bin/launchctl', 'enable', job], check=True)
    # bootout can return before launchd has finished removing the old job.
    deadline = time.monotonic() + 30
    while True:
        result = subprocess.run(['/bin/launchctl', 'bootstrap', target, str(plist)], capture_output=True)
        if result.returncode == 0:
            break
        if not replacing or result.returncode != 5 or time.monotonic() >= deadline:
            sys.stderr.buffer.write(result.stderr)
            result.check_returncode()
        time.sleep(1)
    print(cfg['resource'], 'slots', cfg['slots'])


if __name__ == '__main__':
    if sys.argv[1] == 'install':
        install()
    else:
        cfg = json.loads((STATE / 'manifest.json').read_text())
        if sys.argv[1] == 'execute':
            request = json.load(sys.stdin)
            json.dump(execute(cfg, request['inputs'], Remote(cfg), request.get('progress_path')), sys.stdout)
        elif sys.argv[1] == 'serve':
            serve(cfg)
        else:
            raise SystemExit('Expected install, serve or execute')
