"""Patch native terminal delivery while preserving concurrent source overlays."""
import os
import stat
import sys
import tempfile

OLD = "            receipt = remote.call('artifact.put', {'data': base64.b64encode(block).decode('ascii'), 'transient': True},\n                                  'terminal-transient-chunk-' + str(uuid.uuid4()))"
NEW = "            arguments = {'data': base64.b64encode(block).decode('ascii'), 'transient': True}\n            request_id = 'terminal-transient-chunk-' + str(uuid.uuid4())\n            while True:\n                try:\n                    receipt = remote.call('artifact.put', arguments, request_id)\n                    break\n                except Exception:\n                    if not retry:\n                        raise\n                    # The command has already settled. Keep its custodian alive\n                    # and retry this exact immutable upload, never the argv.\n                    # The supervising runner continues custody heartbeats while\n                    # transport/remote storage is unavailable.\n                    time.sleep(5)"


def main(paths):
    prepared = []
    for path in paths:
        path = os.path.realpath(path)
        with open(path) as source:
            original = source.read()
        updated = original
        if os.path.basename(path) == 'terminal_adapter.py':
            old = 'store_stream(path, remote, 48 * 1024)'
            new = 'store_stream(path, remote, 48 * 1024, retry=True)'
            if new not in updated:
                if updated.count(old) != 1:
                    raise RuntimeError('Concurrent Mac adapter differs: ' + path)
                updated = updated.replace(old, new, 1)
        else:
            if NEW not in updated:
                if updated.count(OLD) != 1:
                    raise RuntimeError('Concurrent uploader differs: ' + path)
                updated = updated.replace(OLD, NEW)
            updated = updated.replace('def store_stream(path, remote, chunk_bytes=256 * 1024):',
                                      'def store_stream(path, remote, chunk_bytes=256 * 1024, retry=False):')
            updated = updated.replace('store_stream(result[name], remote)',
                                      'store_stream(result[name], remote, retry=True)')
            if '\nimport time\n' not in updated:
                updated = updated.replace('import socket\n', 'import socket\nimport time\n', 1)
        compile(updated, path, 'exec')
        if updated == original:
            print('CURRENT', path)
        else:
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
