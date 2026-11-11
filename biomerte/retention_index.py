"""Disposable, transactionally dirty, bounded-work retention reference projection.

JSON strings are scanned in chunks rather than decoding entire historical values.
Until both the keyset backfill and dirty queue finish, pruning fails closed.
"""
import json
import hashlib

MAX_REF = 512
CHUNK = 32768


def reference_hash(value):
    # A collision can add roots but cannot remove a real reference. Surrogatepass
    # also supports arbitrary escaped JSON strings without storing their text.
    return hashlib.sha256(value.encode('utf-8', 'surrogatepass')).digest()[:16]


def initialize(db):
    # Establish the fail-closed cursor first, including recovery from interrupted
    # initial schema creation on legacy autocommit SQLite drivers.
    db.execute('CREATE TABLE IF NOT EXISTS retention_ref_cursor(singleton INTEGER PRIMARY KEY,kind TEXT,id TEXT,complete INTEGER)')
    db.execute("INSERT OR IGNORE INTO retention_ref_cursor VALUES(1,'','',0)")
    old = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_ref_version'").fetchone()
    current = old and db.execute('SELECT 1 FROM retention_ref_version WHERE version=3').fetchone()
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_refs'").fetchone()
    if existing and not current:
        # Disposable format migration. Invalidate before startup DDL; canonical
        # records are untouched, and the rebuild remains incremental.
        db.execute("UPDATE retention_ref_cursor SET kind='',id='',complete=0")
        for table in ('retention_refs', 'retention_ref_sources', 'retention_ref_tokens'):
            db.execute('DROP TABLE IF EXISTS ' + table)

    exact_schema_missing = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_exact_order'").fetchone()
    order_schema_missing = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_original_order'").fetchone()
    marker_schema_missing = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_markers'").fetchone()
    statements = [
        'CREATE TABLE IF NOT EXISTS retention_candidate_cursor(id INTEGER PRIMARY KEY,marker TEXT)',
        "INSERT OR IGNORE INTO retention_candidate_cursor VALUES(1,'')",

        'CREATE TABLE IF NOT EXISTS retention_ref_version(version INTEGER PRIMARY KEY)',
        'CREATE TABLE IF NOT EXISTS retention_markers(id TEXT PRIMARY KEY,kind TEXT,key TEXT,terminal REAL)',
        'CREATE INDEX IF NOT EXISTS retention_markers_order ON retention_markers(kind,terminal,key)',
        'CREATE TABLE IF NOT EXISTS retention_original_order(id TEXT PRIMARY KEY,kind TEXT,key TEXT,position INTEGER)',
        'CREATE INDEX IF NOT EXISTS retention_original_order_rank ON retention_original_order(kind,position,key)',
        'CREATE TABLE IF NOT EXISTS retention_exact_order(id TEXT PRIMARY KEY,kind TEXT,key TEXT,position INTEGER)',
        'CREATE INDEX IF NOT EXISTS retention_exact_order_rank ON retention_exact_order(kind,position,key)',

        'CREATE TABLE IF NOT EXISTS retention_ref_tokens(token INTEGER PRIMARY KEY,hash BLOB UNIQUE)',
        'CREATE TABLE IF NOT EXISTS retention_refs(source INTEGER,token INTEGER,PRIMARY KEY(source,token)) WITHOUT ROWID',
        'CREATE INDEX IF NOT EXISTS retention_refs_target ON retention_refs(token,source)',
        'CREATE TABLE IF NOT EXISTS retention_ref_dirty(kind TEXT,id TEXT,PRIMARY KEY(kind,id))',
        'CREATE TABLE IF NOT EXISTS retention_ref_sources(source INTEGER PRIMARY KEY,kind TEXT,id TEXT,ignored INTEGER,submission TEXT,ready INTEGER,UNIQUE(kind,id))',
        'CREATE TABLE IF NOT EXISTS retention_ref_progress(kind TEXT,id TEXT,offset INTEGER,inside INTEGER,escaped INTEGER,token TEXT,overflow INTEGER,PRIMARY KEY(kind,id))',
        ]
    for statement in statements:
        db.execute(statement)
    db.execute('CREATE TRIGGER IF NOT EXISTS retention_ref_token_cleanup AFTER DELETE ON retention_refs BEGIN '
               'DELETE FROM retention_ref_tokens WHERE token=OLD.token AND NOT EXISTS '
               '(SELECT 1 FROM retention_refs WHERE token=OLD.token); END')
    if marker_schema_missing or order_schema_missing or exact_schema_missing:
        db.execute("UPDATE retention_ref_cursor SET kind='',id='',complete=0")
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        row = 'OLD' if event == 'DELETE' else 'NEW'
        db.execute('CREATE TRIGGER IF NOT EXISTS retention_ref_' + event.lower() +
                   ' AFTER ' + event + ' ON records BEGIN '
                   'INSERT OR IGNORE INTO retention_ref_dirty VALUES(' + row + '.kind,' + row + '.id); '
                   'DELETE FROM retention_ref_progress WHERE kind=' + row + '.kind AND id=' + row + '.id; END')

    db.execute('CREATE TRIGGER IF NOT EXISTS retention_ref_update_old AFTER UPDATE ON records BEGIN '
               'INSERT OR IGNORE INTO retention_ref_dirty VALUES(OLD.kind,OLD.id); '
               'DELETE FROM retention_ref_progress WHERE kind=OLD.kind AND id=OLD.id; END')
    if not db.execute('SELECT 1 FROM retention_ref_version WHERE version=3').fetchone():
        reset(db)
        db.execute('DELETE FROM retention_ref_version')
        db.execute('INSERT INTO retention_ref_version VALUES(3)')


def reset(db):
    """Explicit rebuild hook for restore implementations replacing whole tables."""
    # Invalidate readiness first, including when called during autocommit startup.
    db.execute("UPDATE retention_ref_cursor SET kind='',id='',complete=0")
    for table in ('retention_refs', 'retention_ref_tokens', 'retention_ref_sources', 'retention_ref_dirty', 'retention_ref_progress', 'retention_markers', 'retention_original_order', 'retention_exact_order'):
        db.execute('DELETE FROM ' + table)


def _scan(db, kind, ident, chunk_chars):
    progress = db.execute('SELECT offset,inside,escaped,token,overflow FROM retention_ref_progress WHERE kind=? AND id=?', (kind, ident)).fetchone()
    if progress is None:
        db.execute('DELETE FROM retention_refs WHERE source IN (SELECT source FROM retention_ref_sources WHERE kind=? AND id=?)', (kind, ident))
        db.execute('DELETE FROM retention_ref_sources WHERE kind=? AND id=?', (kind, ident))
        db.execute('INSERT INTO retention_ref_sources(kind,id,ignored,ready) VALUES(?,?,0,0)', (kind, ident))
        if kind == '_retention':
            db.execute('DELETE FROM retention_markers WHERE id=?', (ident,))
            db.execute('DELETE FROM retention_original_order WHERE id=?', (ident,))
            db.execute('DELETE FROM retention_exact_order WHERE id=?', (ident,))
        progress = (0, 0, 0, '', 0)
    source = db.execute('SELECT source FROM retention_ref_sources WHERE kind=? AND id=?', (kind, ident)).fetchone()[0]
    offset, inside, escaped, token, overflow = progress
    row = db.execute('SELECT substr(value,?,?) FROM records WHERE kind=? AND id=?',
                     (offset + 1, chunk_chars, kind, ident)).fetchone()
    chunk = row[0] if row else ''
    refs = set()
    for char in chunk:
        if not inside:
            if char == '"':
                inside, escaped, token, overflow = 1, 0, '', 0
            continue
        if char == '"' and not escaped:
            if not overflow:
                value = json.loads('"' + token + '"')
                if len(value) <= MAX_REF:
                    refs.add(value)
                if value.startswith('action:') and len(value[7:]) <= MAX_REF:
                    refs.add(value[7:])
            inside, token = 0, ''
            continue
        # A non-BMP codepoint may occupy two six-character JSON escapes.
        if len(token) < 12 * (MAX_REF + 7):
            token += char
        else:
            overflow = 1
        escaped = int(char == '\\' and not escaped)
    hashes = set(reference_hash(ref) for ref in refs)
    db.executemany('INSERT OR IGNORE INTO retention_ref_tokens(hash) VALUES(?)', ((h,) for h in hashes))
    for hashed in hashes:
        target = db.execute('SELECT token FROM retention_ref_tokens WHERE hash=?', (hashed,)).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO retention_refs VALUES(?,?)', (source, target))
    offset += len(chunk)
    if row is not None and len(chunk) == chunk_chars:
        db.execute('INSERT OR REPLACE INTO retention_ref_progress VALUES(?,?,?,?,?,?,?)',
                   (kind, ident, offset, inside, escaped, token, overflow))
        return False, len(chunk)
    ignored, submission = int(kind in ('_retention', '_meta')), None
    # Oversized values remain conservative roots; never decode them wholesale.
    if row is not None and offset <= CHUNK:
        encoded = db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, ident)).fetchone()[0]
        value = json.loads(encoded)
        if kind == '_retention' and isinstance(value, dict):
            db.execute('INSERT OR REPLACE INTO retention_markers VALUES(?,?,?,?)',
                       (ident, value.get('kind'), value.get('key'), value.get('terminal_at')))
            if isinstance(value.get('completed_order'), int):
                db.execute('INSERT OR REPLACE INTO retention_original_order VALUES(?,?,?,?)',
                           (ident, value.get('kind'), value.get('key'), value['completed_order']))
                if not value.get('order_upper_bound'):
                    db.execute('INSERT OR REPLACE INTO retention_exact_order VALUES(?,?,?,?)',
                               (ident, value.get('kind'), value.get('key'), value['completed_order']))
        if isinstance(value, dict):
            ignored |= int(kind in ('actions', 'runs', '_receipt', '_submissions') and bool(value.get('result_expired')))
            ignored |= int(kind == 'leases' and value.get('state') == 'released')
        if kind == '_submission_leases' and isinstance(value, str):
            submission = value
    if row is not None:
        db.execute('UPDATE retention_ref_sources SET ignored=?,submission=?,ready=1 WHERE source=?', (ignored, submission, source))
    else:
        db.execute('DELETE FROM retention_ref_sources WHERE source=?', (source,))
    db.execute('DELETE FROM retention_ref_progress WHERE kind=? AND id=?', (kind, ident))
    db.execute('DELETE FROM retention_ref_dirty WHERE kind=? AND id=?', (kind, ident))
    return True, len(chunk)


def advance(db, max_rows=64, max_chars=131072):
    """Advance at most max_rows records and max_chars JSON characters per tick.

    Limits bound payload decoding, not wall time or SQLite page access. UTF-8
    characters occupy at most four bytes. A small source metadata decode adds
    at most CHUNK characters per completed row. All updates share caller's tx.
    """
    if max_rows < 1 or max_chars < 1:
        raise ValueError('Positive reference indexing budgets required')
    used = 0
    for unused in range(max_rows):
        if used >= max_chars:
            break
        cursor = db.execute('SELECT kind,id,complete FROM retention_ref_cursor WHERE singleton=1').fetchone()
        dirty = db.execute('SELECT kind,id FROM retention_ref_dirty ORDER BY kind,id LIMIT 1').fetchone()
        legacy = False
        if dirty:
            key = dirty
        elif not cursor[2]:
            key = db.execute('SELECT kind,id FROM records WHERE kind=? AND id>? ORDER BY id LIMIT 1',
                             (cursor[0], cursor[1])).fetchone()
            if key is None:
                key = db.execute('SELECT kind,id FROM records WHERE kind>? ORDER BY kind,id LIMIT 1',
                                 (cursor[0],)).fetchone()
            if key is None:
                db.execute('UPDATE retention_ref_cursor SET complete=1')
                break
            legacy = True
            # Ready reference bodies stay skipped, but canonical markers must
            # repopulate disposable order tables after a schema-only rebuild.
            if (db.execute('SELECT 1 FROM retention_ref_sources WHERE kind=? AND id=? AND ready=1', key).fetchone() and
                    key[0] != '_retention'):
                db.execute('UPDATE retention_ref_cursor SET kind=?,id=? WHERE singleton=1', key)
                continue
        else:
            break
        done, size = _scan(db, key[0], key[1], min(CHUNK, max_chars - used))
        used += size
        if done and legacy:
            db.execute('UPDATE retention_ref_cursor SET kind=?,id=? WHERE singleton=1', key)
    return bool(db.execute('SELECT complete FROM retention_ref_cursor').fetchone()[0]) and not db.execute('SELECT 1 FROM retention_ref_dirty LIMIT 1').fetchone()


def protected_by_roots(tx, pool, records):
    """Resolve indexed inbound references and candidate transitive closure."""
    from .retention import strings
    by_id, protected, pending = {}, set(), []
    for key in pool:
        aliases = [key[1]]
        value = records[key]
        if isinstance(value, dict) and isinstance(value.get('id'), str):
            aliases.append(value['id'])
        for alias in aliases:
            if len(alias) > MAX_REF:
                protected.add(key)
                pending.append(records[key])
            else:
                by_id.setdefault(alias, set()).add(key)
    for ref, targets in by_id.items():
        rows = tx.db.execute('SELECT s.kind,s.id,s.ignored,s.submission FROM retention_ref_tokens t '
                             'JOIN retention_refs r ON r.token=t.token '
                             'JOIN retention_ref_sources s ON s.source=r.source WHERE t.hash=?', (reference_hash(ref),))
        for kind, ident, ignored, submission in rows:
            if (kind, ident) in pool or ignored:
                continue
            if submission is not None and (('_submissions', submission) in pool or tx.get('_submissions', submission, {}).get('result_expired')):
                continue
            for key in targets - protected:
                protected.add(key)
                pending.append(records[key])
            break
    while pending:
        for ref in strings(pending.pop()):
            for key in by_id.get(ref, ()):
                if key not in protected:
                    protected.add(key)
                    pending.append(records[key])
    return protected


def status(db):
    """On-demand operational verification; does not load canonical payloads."""
    kind, ident, complete = db.execute('SELECT kind,id,complete FROM retention_ref_cursor WHERE singleton=1').fetchone()
    dirty = db.execute('SELECT count(*) FROM retention_ref_dirty').fetchone()[0]
    remaining = 0 if complete else db.execute('SELECT count(*) FROM records WHERE kind>=? AND (kind>? OR id>?)', (kind, kind, ident)).fetchone()[0]
    return {'ready': bool(complete and not dirty), 'legacy_complete': bool(complete),
            'dirty_records': dirty, 'legacy_keys_remaining': remaining,
            'partial_records': db.execute('SELECT count(*) FROM retention_ref_progress').fetchone()[0],
            'partial_characters': db.execute('SELECT coalesce(sum(offset),0) FROM retention_ref_progress').fetchone()[0]}
