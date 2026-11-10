"""Patch only terminal stream delivery; preserve concurrent runtime overlays."""
import os
import stat
import sys
import tempfile

OLD = """            receipt = remote.call('artifact.put', {'data': base64.b64encode(block).decode('ascii'), 'transient': True},
                                  'terminal-transient-chunk-' + str(uuid.uuid4()))"""
NEW = """            arguments = {'data': base64.b64encode(block).decode('ascii'), 'transient': True}
            request_id = 'terminal-transient-chunk-' + str(uuid.uuid4())
            while True:
                try:
                    receipt = remote.call('artifact.put', arguments, request_id)
                    break
                except Exception:
                    # The command has already settled. Keep its custodian alive
                    # and retry this exact immutable upload, never the argv.
                    # The supervising runner continues custody heartbeats while
                    # transport/remote storage is unavailable.
                    time.sleep(5)"""


def main(paths):
    prepared = []
    for path in paths:
        path = os.path.realpath(path)
        with open(path) as source:
            original = source.read()
        if NEW in original:
            print('CURRENT', path)
            continue
        if original.count(OLD) != 1:
            raise RuntimeError('Concurrent uploader source differs: ' + path)
        updated = original.replace(OLD, NEW)
        if '\nimport time\n' not in updated:
            updated = updated.replace('import socket\n', 'import socket\nimport time\n', 1)
        compile(updated, path, 'exec')
        prepared.append((path, original, updated, stat.S_IMODE(os.stat(path).st_mode)))
    for path, original, updated, mode in prepared:
        with open(path) as source:
            if source.read() != original:
                raise RuntimeError('Source changed while preparing: ' + path)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(updated)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print('PATCHED', path)


if __name__ == '__main__':
    main(sys.argv[1:])
