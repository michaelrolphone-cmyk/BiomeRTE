"""Bundle the current repository worker and terminal rules in a single macOS installer."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

here = Path(__file__).resolve().parent
repo = here.parents[1]


def bundle():
    files = {p.name: p.read_bytes() for p in (here / 'mac_provider.py', here / 'terminal_adapter.py',
                                               here.parent / 'pi-terminal/terminal.py',
                                               here.parent / 'pi-terminal/terminal_limits.py')}
    for name in ('__init__', 'api', 'worker', 'store', 'identity'):
        p = repo / 'biomerte' / (name + '.py')
        files['runtime/biomerte/' + p.name] = p.read_bytes()
    files['runtime-source.txt'] = ('Shared BiomeRTE worker sha256 ' +
                                    hashlib.sha256(files['runtime/biomerte/worker.py']).hexdigest() + '\n').encode()
    files['SHA256SUMS.json'] = json.dumps({k: hashlib.sha256(v).hexdigest() for k, v in files.items()}).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return archive.getvalue()


if __name__ == '__main__':
    out = Path(sys.argv[1])
    out.write_bytes((here / 'installer.sh.in').read_bytes() + b'\n__BIOME_PAYLOAD__\n' + base64.encodebytes(bundle()))
    out.chmod(0o755)
