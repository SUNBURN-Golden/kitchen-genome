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
    """Apply checksum-pinned migrations atomically, including during concurrent startup."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = []
    for fn in sorted(f for f in os.listdir(schema_dir) if f.endswith('.sql')):
        with open(os.path.join(schema_dir, fn), encoding='utf-8') as script:
            sql = script.read()
        ck = _checksum(sql)
        done = conn.execute('SELECT checksum FROM schema_migration WHERE version=?', (fn,)).fetchone()
        if done and done[0] == ck:
            continue
        if conn.in_transaction:
            conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            # Another collector may have applied the script while waiting for the lock.
            done = conn.execute('SELECT checksum FROM schema_migration WHERE version=?', (fn,)).fetchone()
            if done:
                if done[0] != ck:
                    raise RuntimeError(f'migration {fn} was modified after being applied; add a new migration instead')
                continue
            # executescript commits an existing transaction. Complete statements keep
            # the DDL, CREATE TRIGGER bodies and checksum under one lock.
            statement = ''
            for piece in sql.split(';'):
                statement += piece + ';'
                if sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement = ''
            if statement.strip():
                conn.execute(statement)
            conn.execute('INSERT INTO schema_migration(version, checksum, applied_at) VALUES (?,?,?)', (fn, ck, now or utcnow()))
            applied.append(fn)
    return applied


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic opaque id (idempotent migrations). Never derived again once stored."""
    h = hashlib.sha1('\x1f'.join(parts).encode('utf-8')).hexdigest()[:12]
    return f'{prefix}_{h}'


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:12]}'
