"""Portable caller guides for each version-one storage contract."""
PATH = 'A path inside the shared volume. Use / for the root. Dot and parent traversal components are rejected.'
HANDLE = 'Use result.result.handle from a successful storage.file.open:1 command on this volume.'
FILE = '/demo/note.txt'
TOKEN = '<handle from open>'
SUCCESS = 'On succeeded, check result.ok; if false read result.error. '
LIFETIME = 'Release this contract lease before acquiring another file contract on the same resource. Open handles survive lease release and service restart. '

# Sample values refer to one example tree. A caller creates its own fresh
# directory and substitutes the actual handle returned by open.
GUIDES = {
    'mkdir': ({'path': '/demo'}, {'path': PATH}, 'Choose a fresh directory name under an existing parent.',
              'An empty result.result confirms creation.', 'ALREADY_EXISTS means the name is in use.'),
    'open': ({'path': FILE, 'mode': 'x+'}, {'path': PATH, 'mode': 'r reads, w creates or truncates, a appends, x creates exclusively. Add + for read/write.'},
             'Create /demo first for this example.', 'Save result.result.handle; offset and size are byte counts.',
             'NOT_FOUND means a required file or parent is absent. ALREADY_EXISTS protects an existing file in x mode.'),
    'write': ({'handle': TOKEN, 'data': 'aGVsbG8='}, {'handle': HANDLE, 'data': 'Base64-encode the bytes. aGVsbG8= writes hello. Maximum 65536 decoded bytes per call.',
              'offset': 'Optional byte offset; defaults to the handle position. Append mode always writes at EOF and rejects an explicit offset.'},
              'Open with a writable mode, such as x+ for a new file.', 'Read result.result.bytes, offset and size. The sample writes 5 bytes.',
              'NOT_WRITABLE, INVALID_HANDLE, INVALID_BASE64, CHUNK_TOO_LARGE, OFFSET_PAST_END or NO_SPACE can prevent a write.'),
    'read': ({'handle': TOKEN, 'offset': 0, 'length': 5}, {'handle': HANDLE, 'offset': 'Optional byte position. Use zero to read back after writing.',
             'length': 'Optional maximum byte count, 0 through 65536. Defaults to 65536.'},
             'Use a readable handle. To read the write example, reuse its x+ handle.', 'Base64-decode result.result.data. bytes is the count, offset the next byte, and eof marks end of file.',
             'NOT_READABLE, INVALID_HANDLE and INVALID_LENGTH identify invalid reads.'),
    'close': ({'handle': TOKEN}, {'handle': HANDLE}, 'Finish reads and writes on this handle.',
              'An empty result.result confirms the handle is closed. The file remains.', 'A previously closed handle returns INVALID_HANDLE.'),
    'exists': ({'path': '/'}, {'path': PATH}, 'No open handle is required.',
               'Read result.result.exists. When true, type is file or directory. The root exists and is a directory.',
               'An absent path is an ordinary successful lookup with exists=false.'),
    'size': ({'path': FILE}, {'path': PATH}, 'Supply an existing file path; a handle is unnecessary.',
             'Read result.result.size in bytes.', 'NOT_FOUND or IS_DIRECTORY means there is no file size at that path.'),
    'list': ({'path': '/', 'limit': 50}, {'path': PATH, 'limit': 'Optional page size, 1 through 200; defaults to 100.',
             'after': 'For the next page, pass the previous result.result.next as after with the same path.'},
             'Supply an existing directory.', 'Read result.result.entries for names, types and sizes. Stop when next is null.',
             'NOT_DIRECTORY or NOT_FOUND means the path cannot be listed.'),
    'rename': ({'path': FILE, 'to': '/demo/renamed.txt'}, {'path': PATH, 'to': 'An unused destination path in an existing parent.'},
               'Create the source first. Open handles continue to refer to the moved file.',
               'An empty result.result confirms the move. Use the destination for subsequent path operations.',
               'ALREADY_EXISTS protects a destination; DIRECTORY_CYCLE rejects moving a directory under itself.'),
    'delete': ({'path': '/demo/renamed.txt'}, {'path': PATH}, 'Close all handles to the file first. Use the actual current path.',
               'An empty result.result confirms deletion. A later exists call on that path returns false.',
               'FILE_OPEN means a handle is still live. IS_DIRECTORY means use rmdir for an empty directory.'),
    'rmdir': ({'path': '/demo'}, {'path': PATH}, 'Remove children first; the volume root cannot be removed.',
              'An empty result.result confirms directory removal.', 'NOT_EMPTY means the directory still contains entries.')
}


def file_usage(op):
    example, parameters, before, result, errors = GUIDES[op]
    return {'parameters': parameters, 'example_inputs': example, 'before': LIFETIME + before,
            'read_result': SUCCESS + result, 'errors': errors}
