"""SQLite/D1-compatible service DB helpers: connect, ordered migrations with checksums, ids, clock."""
import datetime as dt
import hashlib
import os
import sqlite3
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_DIR = os.path.join(ROOT, 'schema', 'migrations')
DEFAULT_DB = os.path.join(ROOT, 'data_store', 'kg_service.db')   # gitignored


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    if path != ':memory:':
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode('utf-8')).hexdigest()[:16]


def migrate(conn: sqlite3.Connection, schema_dir: str = SCHEMA_DIR, now: str | None = None) -> list[str]:
    """Apply pending *.sql files in name order. Applied files are checksum-pinned: editing one is an error."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    done = {r['version']: r['checksum'] for r in conn.execute('SELECT version, checksum FROM schema_migration')}
    applied = []
    for fn in sorted(f for f in os.listdir(schema_dir) if f.endswith('.sql')):
        sql = open(os.path.join(schema_dir, fn), encoding='utf-8').read()
        ck = _checksum(sql)
        if fn in done:
            if done[fn] != ck:
                raise RuntimeError(f'migration {fn} was modified after being applied; add a new migration instead')
            continue
        # schema_migration is created idempotently above, so strip nothing: scripts use IF NOT EXISTS for it.
        conn.executescript(sql)
        conn.execute('INSERT INTO schema_migration(version, checksum, applied_at) VALUES (?,?,?)', (fn, ck, now or utcnow()))
        conn.commit()
        applied.append(fn)
    return applied


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic opaque id (idempotent migrations). Never derived again once stored."""
    h = hashlib.sha1('\x1f'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}_{h}'


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:12]}'
