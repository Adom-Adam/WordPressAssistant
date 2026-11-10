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
    note TEXT,
    role TEXT NOT NULL DEFAULT 'normal'
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

CREATE TABLE IF NOT EXISTS relationship_state (
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    affection REAL NOT NULL DEFAULT 0,
    missing REAL NOT NULL DEFAULT 50,
    security REAL NOT NULL DEFAULT 70,
    patience REAL NOT NULL DEFAULT 70,
    valence REAL NOT NULL DEFAULT 0,
    energy REAL NOT NULL DEFAULT 1,
    fatigue REAL NOT NULL DEFAULT 0,
    reply_debt REAL NOT NULL DEFAULT 0,
    last_interaction_at TEXT,
    last_proactive_at TEXT,
    last_reply_at TEXT,
    attention_confidence REAL NOT NULL DEFAULT 0,
    conflict_stage TEXT NOT NULL DEFAULT 'normal',
    stage_since TEXT,
    scar_level REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (persona_key, person_key)
);

CREATE INDEX IF NOT EXISTS idx_relationship_state_person
    ON relationship_state(person_key);

CREATE TABLE IF NOT EXISTS relationship_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    d_affection REAL NOT NULL DEFAULT 0,
    d_missing REAL NOT NULL DEFAULT 0,
    d_security REAL NOT NULL DEFAULT 0,
    d_patience REAL NOT NULL DEFAULT 0,
    d_valence REAL NOT NULL DEFAULT 0,
    d_energy REAL NOT NULL DEFAULT 0,
    d_debt REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1,
    source_event_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relationship_events_person
    ON relationship_events(persona_key, person_key, id);

-- 每次“这条私聊回不回”的决策记录。只追加、不改写，用来解释
-- 为什么沉默、为什么回，以及调参时对照概率和随机数。
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    person_key TEXT NOT NULL,
    message_id TEXT,
    rule_score REAL NOT NULL DEFAULT 0,
    willingness REAL NOT NULL DEFAULT 0,
    valence REAL NOT NULL DEFAULT 0,
    energy REAL NOT NULL DEFAULT 1,
    affinity REAL NOT NULL DEFAULT 1,
    reply_debt REAL NOT NULL DEFAULT 0,
    p0 REAL NOT NULL DEFAULT 1,
    p REAL NOT NULL DEFAULT 1,
    roll REAL,
    verdict TEXT NOT NULL,
    reason TEXT,
    reply_bias REAL NOT NULL DEFAULT 0,
    appraisal_event TEXT,
    appraisal_confidence REAL NOT NULL DEFAULT 0,
    conflict_stage TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_log_persona
    ON decision_log(persona_key, id);

-- 每轮私聊模型评估的审计日志。原始输出、映射后的增量、实际落账值
-- 都留一份，调参和排查“为什么这轮好感变了”时不用猜。
CREATE TABLE IF NOT EXISTS appraisal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    event TEXT,
    intensity REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    reply_bias REAL NOT NULL DEFAULT 0,
    reason TEXT,
    raw_output TEXT,
    d_affection REAL NOT NULL DEFAULT 0,
    d_valence REAL NOT NULL DEFAULT 0,
    d_security REAL NOT NULL DEFAULT 0,
    d_patience REAL NOT NULL DEFAULT 0,
    applied_affection REAL NOT NULL DEFAULT 0,
    applied_valence REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    skipped_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appraisal_log_person
    ON appraisal_log(persona_key, person_key, id);

-- 模型评估的每日用量。scope 是 'global' 或 'person:<person_key>'。
-- 用 UPSERT + WHERE 做原子占位，多进程并发时不会超发。
CREATE TABLE IF NOT EXISTS appraisal_usage (
    scope TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, day)
);

-- 每次主动决策的审计：念头、分数、阈值、结果。
CREATE TABLE IF NOT EXISTS proactive_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    thought TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    threshold REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    event_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proactive_log_target
    ON proactive_log(persona_key, target_key, id);

-- 因累/忙/心情低而没接的话。属于主动消息，不另建调度器。
CREATE TABLE IF NOT EXISTS pending_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_topics_person
    ON pending_topics(persona_key, person_key, status, id);

-- 身份别名：`alias_key` 曾经是独立身份，现已并入 `canonical_key`。
-- 合并是物理搬迁，这张表只用来解释“旧 key 去哪了”，以及让
-- `canonical_key()` 能把链式合并（qq→wp:2→wp:3）一路追到底。
CREATE TABLE IF NOT EXISTS person_aliases (
    alias_key     TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_person_aliases_canonical
    ON person_aliases(canonical_key);

-- 每个群的三个正交开关。缺行时回退到 QQ_TRIGGER。
CREATE TABLE IF NOT EXISTS qq_group_policies (
    group_id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    reply_policy TEXT NOT NULL,
    proactive INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- 白名单里的 QQ 号。未绑定的号也能拿到完整功能；绑定过的号不该出现在这里
-- （绑定本身就带着身份），但旧数据可能残留，读取时以绑定为准。
CREATE TABLE IF NOT EXISTS qq_whitelist (
    qq_uin   TEXT PRIMARY KEY,
    note     TEXT,
    added_by TEXT,
    added_at TEXT NOT NULL
);

-- 允许梅林出现的群。首次启动从 `QQ_ALLOWED_GROUPS` 播种一次，
-- 之后只在后台增删，重启不会被环境变量覆盖。
CREATE TABLE IF NOT EXISTS qq_allowed_groups (
    group_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

-- 匿名用户最近一次收到“去图书馆登记”提示的时间。按 scope（QQ 号 / IP 哈希）
-- 记，不记内容：匿名说过什么，一句都不留。
CREATE TABLE IF NOT EXISTS anonymous_guide_log (
    scope        TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
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


def _ensure_person_role_column(conn: sqlite3.Connection) -> None:
    """给 3a 之前的旧库补 `persons.role`；新库建表时已经带这一列。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(persons)")}
    if "role" not in columns:
        conn.execute(
            "ALTER TABLE persons ADD COLUMN role TEXT NOT NULL DEFAULT 'normal'"
        )


def _ensure_relationship_state_fatigue_column(conn: sqlite3.Connection) -> None:
    """给 3a.1 之前的旧库补 `relationship_state.fatigue`。"""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(relationship_state)")
    }
    if "fatigue" not in columns:
        conn.execute(
            "ALTER TABLE relationship_state "
            "ADD COLUMN fatigue REAL NOT NULL DEFAULT 0"
        )


def _ensure_relationship_state_conflict_columns(
    conn: sqlite3.Connection,
) -> None:
    """给 3c.1 之前的旧库补冲突弧字段。"""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(relationship_state)")
    }
    if "conflict_stage" not in columns:
        conn.execute(
            "ALTER TABLE relationship_state "
            "ADD COLUMN conflict_stage TEXT NOT NULL DEFAULT 'normal'"
        )
    if "stage_since" not in columns:
        conn.execute("ALTER TABLE relationship_state ADD COLUMN stage_since TEXT")
    if "scar_level" not in columns:
        conn.execute(
            "ALTER TABLE relationship_state "
            "ADD COLUMN scar_level REAL NOT NULL DEFAULT 0"
        )


def _ensure_decision_log_stage_column(conn: sqlite3.Connection) -> None:
    """给 3c.1 之前的旧库补 `decision_log.conflict_stage`。"""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")
    }
    if "conflict_stage" not in columns:
        conn.execute(
            "ALTER TABLE decision_log "
            "ADD COLUMN conflict_stage TEXT NOT NULL DEFAULT 'normal'"
        )


def _ensure_decision_log_appraisal_columns(conn: sqlite3.Connection) -> None:
    """给 3c 之前的旧库补评估相关列。"""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")
    }
    if "reply_bias" not in columns:
        conn.execute(
            "ALTER TABLE decision_log ADD COLUMN reply_bias REAL NOT NULL DEFAULT 0"
        )
    if "appraisal_event" not in columns:
        conn.execute("ALTER TABLE decision_log ADD COLUMN appraisal_event TEXT")
    if "appraisal_confidence" not in columns:
        conn.execute(
            "ALTER TABLE decision_log "
            "ADD COLUMN appraisal_confidence REAL NOT NULL DEFAULT 0"
        )


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """建表。可以反复调用；每次调用都很轻。"""
    own = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.executescript(_SCHEMA)
        _ensure_person_role_column(conn)
        _ensure_relationship_state_fatigue_column(conn)
        _ensure_relationship_state_conflict_columns(conn)
        _ensure_decision_log_appraisal_columns(conn)
        _ensure_decision_log_stage_column(conn)
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
