"""记忆库：SQLite 建表、连接与统计。

单独一个文件（默认 `backend/data/memory.sqlite3`），不和额度库混：
额度是计数，记忆是内容，备份、按人删除、清理的粒度完全不同。

时间一律存 UTC ISO 字符串（`2026-11-07T09:30:00+00:00`）。SQLite 没有
原生时间类型，ISO 字符串既能直接比较大小，也能原样给 Python 解析。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import get_settings
from app.vector_store import load_extension

from app.memory.vectors import MEMORIES_VEC

_BACKEND_ROOT = Path(__file__).resolve().parents[2]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS persons (
    person_key TEXT PRIMARY KEY,
    display_name TEXT,
    wp_user_id INTEGER,
    qq_uin TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    person_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    audience TEXT NOT NULL,
    audience_key TEXT NOT NULL,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_key, id);
CREATE INDEX IF NOT EXISTS idx_events_audience ON events(audience_key, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_key TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    scope TEXT NOT NULL,
    audience TEXT NOT NULL,
    audience_key TEXT NOT NULL,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.6,
    valid_from TEXT,
    valid_until TEXT,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    access_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_person ON memories(person_key, id);
CREATE INDEX IF NOT EXISTS idx_memories_audience ON memories(audience_key, id);
"""


def resolve_db_path() -> Path:
    """把配置里的相对路径按 `backend/` 解析成绝对路径。"""
    path = Path(get_settings().memory_db_path)
    return path if path.is_absolute() else _BACKEND_ROOT / path


def connect() -> sqlite3.Connection:
    """打开记忆库连接。目录不存在时创建（新部署也能直接起）。"""
    path = resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    load_extension(conn)
    return conn


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """建表。可以反复调用；每次调用都很轻。"""
    own = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.executescript(_SCHEMA)
        MEMORIES_VEC.ensure(conn, getattr(get_settings(), "embedding_dim", 512))
        conn.commit()
    finally:
        if own:
            conn.close()


def stats(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """三张表的行数，供健康检查和命令行使用。"""
    own = conn is None
    if conn is None:
        conn = connect()
    try:
        return {
            "events": int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
            "memories": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            "persons": int(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]),
        }
    finally:
        if own:
            conn.close()
