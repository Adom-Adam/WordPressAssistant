"""4b 三张新表的建表测试。

只验证 schema：主动决策日志、待续话题、群策略。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def test_new_tables_exist(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"proactive_log", "pending_topics", "qq_group_policies"} <= tables


def test_stats_stays_user_facing(conn):
    assert db.stats(conn) == {"events": 0, "memories": 0, "persons": 0}


def test_proactive_log_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(proactive_log)")}
    assert cols == {
        "id",
        "persona_key",
        "target_key",
        "channel_id",
        "kind",
        "thought",
        "score",
        "threshold",
        "status",
        "reason",
        "event_id",
        "created_at",
    }


def test_pending_topics_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(pending_topics)")}
    assert cols == {
        "id",
        "persona_key",
        "person_key",
        "topic",
        "reason",
        "status",
        "created_at",
        "expires_at",
        "sent_at",
    }


def test_group_policy_columns(conn):
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(qq_group_policies)")
    }
    assert cols == {"group_id", "trigger", "reply_policy", "proactive", "updated_at"}
