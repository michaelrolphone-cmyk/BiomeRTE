#!/bin/bash
set -euo pipefail
BIOME_MAINTENANCE_PLIST="$HOME/Library/LaunchAgents/org.biomerte.maintenance.plist"
if [[ ! -f "$BIOME_MAINTENANCE_PLIST" ]]; then
  echo "Existing maintenance LaunchAgent is missing; this repair does not enroll a new host." >&2
  exit 1
fi
BIOME_MAINTENANCE_PYTHON=$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$BIOME_MAINTENANCE_PLIST" 2>/dev/null) || {
  echo "Cannot read the existing maintenance Python from its LaunchAgent." >&2
  exit 1
}
if [[ ! -x "$BIOME_MAINTENANCE_PYTHON" ]]; then
  echo "The existing maintenance Python is unavailable; no files changed." >&2
  exit 1
fi
exec "$BIOME_MAINTENANCE_PYTHON" -S - "$BIOME_MAINTENANCE_PLIST" <<'BIOME_MAINTENANCE_REPAIR'
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import uuid

LABEL = 'org.biomerte.maintenance'
AGENT_SHA256 = '53c4d29d2d0f8841529691325ad27901fe4b7f0f73a2f7674072d6e609c6d114'
AGENT_BASE64 = 'IiIiSW5kZXBlbmRlbnQgb3duZXIgbWFpbnRlbmFuY2UgYWdlbnQuIE5vIGltcG9ydHMgb3IgY2FsbHMgaW50byBCaW9tZVJURS4iIiIKaW1wb3J0IGhhc2hsaWIKaW1wb3J0IGpzb24KaW1wb3J0IG9zCmZyb20gcGF0aGxpYiBpbXBvcnQgUGF0aAppbXBvcnQgc3FsaXRlMwppbXBvcnQgc3VicHJvY2VzcwppbXBvcnQgc3lzCmltcG9ydCB0aHJlYWRpbmcKaW1wb3J0IHRpbWUKZnJvbSB1cmxsaWIucmVxdWVzdCBpbXBvcnQgUmVxdWVzdCwgdXJsb3BlbgoKb3MudW1hc2soMG8wNzcpCkhPTUUgPSBQYXRoKHN5cy5hcmd2WzFdKQpDT05GID0ganNvbi5sb2FkcygoSE9NRSAvICdjb25maWcuanNvbicpLnJlYWRfdGV4dCgpKQpLRVkgPSAoSE9NRSAvICdkZXZpY2Uua2V5JykucmVhZF90ZXh0KCkuc3RyaXAoKQpEQiA9IHNxbGl0ZTMuY29ubmVjdChzdHIoSE9NRSAvICdsZWRnZXIuc3FsaXRlJyksIGNoZWNrX3NhbWVfdGhyZWFkPUZhbHNlKQpEQi5leGVjdXRlKCdQUkFHTUEgam91cm5hbF9tb2RlPVdBTCcpCkRCLmV4ZWN1dGUoJ0NSRUFURSBUQUJMRSBJRiBOT1QgRVhJU1RTIGNvbW1hbmRzKGlkIFRFWFQgUFJJTUFSWSBLRVksc3RhdGUgVEVYVCxyZXN1bHQgVEVYVCknKQpEQi5leGVjdXRlKCJVUERBVEUgY29tbWFuZHMgU0VUIHN0YXRlPSd1bmNlcnRhaW4nLHJlc3VsdD0/IFdIRVJFIHN0YXRlPSdydW5uaW5nJyIsIChqc29uLmR1bXBzKHsnc3RhdGUnOid1bmNlcnRhaW4nLCdyZWFzb24nOidNYWludGVuYW5jZSBhZ2VudCByZXN0YXJ0ZWQ7IHJlY29uY2lsZSBwcm9jZXNzIGFuZCBvdXRwdXQgZmlsZXMgYmVmb3JlIGFub3RoZXIgZXhlY3V0aW9uLid9KSwpKQpEQi5jb21taXQoKQpMT0NLID0gdGhyZWFkaW5nLlJMb2NrKCkKCgojIEpvYnMgYXJlIGFkbWl0dGVkIGJ5IHRoZSBhdXRoZW50aWNhdGVkIG1haW50ZW5hbmNlIGdhdGV3YXkuIFRoZSBkZXZpY2UKIyBjcmVkZW50aWFsIGFuZCBUTFMgdHJhbnNwb3J0IHJlbWFpbiByZXF1aXJlZDsgbm8gc2VwYXJhdGUgcGhvbmUgc2lnbmF0dXJlLgoKZGVmIGV4Y2hhbmdlKGJvZHkpOgogICAgcmVxdWVzdCA9IFJlcXVlc3QoQ09ORlsnb3JpZ2luJ10gKyAnL21haW50ZW5hbmNlL2RldmljZScsIGpzb24uZHVtcHMoYm9keSkuZW5jb2RlKCksCiAgICAgICAgICAgICAgICAgICAgICB7J0F1dGhvcml6YXRpb24nOidCZWFyZXIgJyArIEtFWSwgJ0NvbnRlbnQtVHlwZSc6J2FwcGxpY2F0aW9uL2pzb24nLAogICAgICAgICAgICAgICAgICAgICAgICdVc2VyLUFnZW50JzonQmlvbWVSVEUtSW5kZXBlbmRlbnQtTWFpbnRlbmFuY2UvMSd9KQogICAgd2l0aCB1cmxvcGVuKHJlcXVlc3QpIGFzIHJlc3BvbnNlOgogICAgICAgIHJldHVybiBqc29uLmxvYWRzKHJlc3BvbnNlLnJlYWQoKSkKCgpkZWYgZXhlY3V0ZShqb2IpOgogICAgZGlyZWN0b3J5ID0gSE9NRSAvICdjb21tYW5kcycgLyBoYXNobGliLnNoYTI1Nihqb2JbJ2lkJ10uZW5jb2RlKCkpLmhleGRpZ2VzdCgpCiAgICB0cnk6CiAgICAgICAgZGlyZWN0b3J5Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSkKICAgICAgICB3aXRoIChkaXJlY3RvcnkvJ3N0ZG91dCcpLm9wZW4oJ3diJykgYXMgc3Rkb3V0LCAoZGlyZWN0b3J5LydzdGRlcnInKS5vcGVuKCd3YicpIGFzIHN0ZGVycjoKICAgICAgICAgICAgcHJvY2VzcyA9IHN1YnByb2Nlc3MuUG9wZW4oam9iWydhcmd2J10sIGN3ZD1qb2IuZ2V0KCdjd2QnKSBvciBOb25lLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBzdGRvdXQ9c3Rkb3V0LCBzdGRlcnI9c3RkZXJyLCBzdGFydF9uZXdfc2Vzc2lvbj1UcnVlKQogICAgICAgICAgICAoZGlyZWN0b3J5LydwaWQnKS53cml0ZV90ZXh0KHN0cihwcm9jZXNzLnBpZCkpCiAgICAgICAgICAgIGNvZGUgPSBwcm9jZXNzLndhaXQoKQogICAgICAgIHJlc3VsdCA9IHsnc3RhdGUnOidzdWNjZWVkZWQnIGlmIGNvZGUgPT0gMCBlbHNlICdmYWlsZWQnLCAnZXhpdF9jb2RlJzpjb2RlLAogICAgICAgICAgICAgICAgICAnc3Rkb3V0JzooZGlyZWN0b3J5LydzdGRvdXQnKS5yZWFkX3RleHQoZXJyb3JzPSdyZXBsYWNlJyksCiAgICAgICAgICAgICAgICAgICdzdGRlcnInOihkaXJlY3RvcnkvJ3N0ZGVycicpLnJlYWRfdGV4dChlcnJvcnM9J3JlcGxhY2UnKSwKICAgICAgICAgICAgICAgICAgJ291dHB1dF9kaXJlY3RvcnknOnN0cihkaXJlY3RvcnkpfQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICByZXN1bHQgPSB7J3N0YXRlJzonZmFpbGVkJywnZXJyb3InOnR5cGUoZXJyb3IpLl9fbmFtZV9fICsgJzogJyArIHN0cihlcnJvciksCiAgICAgICAgICAgICAgICAgICdvdXRwdXRfZGlyZWN0b3J5JzpzdHIoZGlyZWN0b3J5KX0KICAgIHNhdmVfcmVzdWx0KGpvYlsnaWQnXSwgcmVzdWx0KQoKCmRlZiBzYXZlX3Jlc3VsdChpZGVudGlmaWVyLCByZXN1bHQpOgogICAgd2l0aCBMT0NLOgogICAgICAgIERCLmV4ZWN1dGUoJ1VQREFURSBjb21tYW5kcyBTRVQgc3RhdGU9PyxyZXN1bHQ9PyBXSEVSRSBpZD0/JywKICAgICAgICAgICAgICAgICAgIChyZXN1bHRbJ3N0YXRlJ10sanNvbi5kdW1wcyhyZXN1bHQpLGlkZW50aWZpZXIpKQogICAgICAgIERCLmNvbW1pdCgpCgoKZGVmIHN0YXJ0X2pvYihqb2IpOgogICAgd2l0aCBMT0NLOgogICAgICAgIGtub3duID0gREIuZXhlY3V0ZSgnU0VMRUNUIHN0YXRlIEZST00gY29tbWFuZHMgV0hFUkUgaWQ9PycsKGpvYlsnaWQnXSwpKS5mZXRjaG9uZSgpCiAgICAgICAgaWYga25vd246CiAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgIERCLmV4ZWN1dGUoIklOU0VSVCBJTlRPIGNvbW1hbmRzIFZBTFVFUyg/LCdydW5uaW5nJyxOVUxMKSIsKGpvYlsnaWQnXSwpKQogICAgICAgIERCLmNvbW1pdCgpCiAgICB0cnk6CiAgICAgICAgdGhyZWFkaW5nLlRocmVhZCh0YXJnZXQ9ZXhlY3V0ZSxhcmdzPShqb2IsKSxkYWVtb249VHJ1ZSkuc3RhcnQoKQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICBzYXZlX3Jlc3VsdChqb2JbJ2lkJ10sIHsnc3RhdGUnOidmYWlsZWQnLAogICAgICAgICAgICAgICAgICAgICdlcnJvcic6J01haW50ZW5hbmNlIHdvcmtlciBjb3VsZCBub3Qgc3RhcnQ6ICcgKyB0eXBlKGVycm9yKS5fX25hbWVfX30pCiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICByZXR1cm4gVHJ1ZQoKCndoaWxlIFRydWU6CiAgICB0cnk6CiAgICAgICAgd2l0aCBMT0NLOgogICAgICAgICAgICBjb21wbGV0ZWQgPSBsaXN0KERCLmV4ZWN1dGUoIlNFTEVDVCBpZCxyZXN1bHQgRlJPTSBjb21tYW5kcyBXSEVSRSBzdGF0ZSBJTiAoJ3N1Y2NlZWRlZCcsJ2ZhaWxlZCcsJ3VuY2VydGFpbicpIikpCiAgICAgICAgZm9yIGlkZW50aWZpZXIsIHJlc3VsdCBpbiBjb21wbGV0ZWQ6CiAgICAgICAgICAgIGV4Y2hhbmdlKHsnb3BlcmF0aW9uJzonY29tcGxldGUnLCdpZCc6aWRlbnRpZmllciwncmVzdWx0Jzpqc29uLmxvYWRzKHJlc3VsdCl9KQogICAgICAgICAgICB3aXRoIExPQ0s6CiAgICAgICAgICAgICAgICBEQi5leGVjdXRlKCJVUERBVEUgY29tbWFuZHMgU0VUIHN0YXRlPSdyZXBvcnRlZCcgV0hFUkUgaWQ9PyIsKGlkZW50aWZpZXIsKSkKICAgICAgICAgICAgICAgIERCLmNvbW1pdCgpCiAgICAgICAgam9icyA9IGV4Y2hhbmdlKHsnb3BlcmF0aW9uJzoncG9sbCd9KVsnam9icyddCiAgICAgICAgZm9yIGpvYiBpbiBqb2JzOgogICAgICAgICAgICBzdGFydF9qb2Ioam9iKQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICAjIE5ldmVyIHByaW50IGNyZWRlbnRpYWxzLCByZXF1ZXN0IGJvZGllcyBvciBjb21tYW5kIG91dHB1dCB0byBsYXVuY2hkIGxvZ3MuCiAgICAgICAgcHJpbnQoJ21haW50ZW5hbmNlIHRyYW5zcG9ydCByZXRyeTogJyt0eXBlKGVycm9yKS5fX25hbWVfXyxmbHVzaD1UcnVlKQogICAgdGltZS5zbGVlcCgxKQo='


def repair(plist_path, run=subprocess.run):
    """Update one existing enrollment without touching its credentials or ledger."""
    os.umask(0o077)
    plist_path = Path(plist_path)
    if not plist_path.is_file() or plist_path.is_symlink():
        raise RuntimeError('Existing maintenance LaunchAgent plist is required.')
    service = plistlib.loads(plist_path.read_bytes())
    arguments = service.get('ProgramArguments')
    if (service.get('Label') != LABEL or not isinstance(arguments, list) or
            len(arguments) < 3 or any(not isinstance(v, str) or not v for v in arguments) or
            any(v not in ('-S', '-u', '-B') for v in arguments[1:-2])):
        raise RuntimeError('Existing maintenance LaunchAgent has an unsupported command.')
    if os.path.realpath(arguments[0]) != os.path.realpath(sys.executable):
        raise RuntimeError('Run this repair with the existing maintenance Python.')
    agent, home = Path(arguments[-2]), Path(arguments[-1])
    if (not agent.is_absolute() or not home.is_absolute() or
            agent.name != 'agent.py' or agent.parent.resolve() != home.resolve() or
            not home.is_dir() or service.get('WorkingDirectory', str(home)) != str(home)):
        raise RuntimeError('Existing maintenance LaunchAgent paths do not match its home.')
    for path in (plist_path, agent, home/'config.json', home/'device.key', home/'ledger.sqlite'):
        if not path.is_file() or path.is_symlink() or path.stat().st_uid != os.getuid():
            raise RuntimeError('Existing owned maintenance file is required: ' + path.name)
    config = json.loads((home/'config.json').read_text())
    if not isinstance(config, dict) or not config.get('origin') or not config.get('host'):
        raise RuntimeError('Existing maintenance enrollment configuration is incomplete.')
    if (home/'device.key').stat().st_size == 0:
        raise RuntimeError('Existing maintenance device credential is empty.')
    payload = base64.b64decode(AGENT_BASE64, validate=True)
    if hashlib.sha256(payload).hexdigest() != AGENT_SHA256:
        raise RuntimeError('Embedded maintenance agent checksum does not match.')
    compile(payload, str(agent), 'exec')
    target = 'gui/' + str(os.getuid()) + '/' + LABEL
    if run(['/bin/launchctl', 'print', target], check=False,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError('Existing maintenance LaunchAgent is not loaded; no files changed.')
    previous = agent.read_bytes()
    backup = home/('agent.py.before-repair-' + uuid.uuid4().hex)
    with backup.open('xb') as output:
        output.write(previous)
        output.flush()
        os.fsync(output.fileno())
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.maintenance-agent-', dir=str(home))
        temporary = Path(name)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if agent.read_bytes() != previous:
            raise RuntimeError('Maintenance agent changed during repair; replacement was not applied.')
        os.replace(str(temporary), str(agent))
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink()
    if run(['/bin/launchctl', 'kickstart', '-k', target], check=False,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError('Verified agent installed, but maintenance restart failed. Previous agent: ' + str(backup))
    return {'service': LABEL, 'sha256': AGENT_SHA256, 'previous_agent': str(backup)}


if __name__ == '__main__':
    try:
        if sys.platform != 'darwin' or os.getuid() == 0:
            raise RuntimeError('Run this repair as the enrolled Mac user, without sudo.')
        result = repair(sys.argv[1])
        print('Installed the verified maintenance agent and restarted the existing service.')
        print('Enrollment, configuration and command ledger were preserved.')
        print('Previous agent: ' + result['previous_agent'])
        print('Verify recovery with a fresh maintenance command result.')
    except Exception as error:
        print('Maintenance repair failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
BIOME_MAINTENANCE_REPAIR
