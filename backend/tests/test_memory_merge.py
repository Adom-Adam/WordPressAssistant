"""身份合并与彻底删除。

QQ 绑定 WP 账号以后，"未绑定时的 QQ 记忆"和"网页记忆"是同一个人。
只做联合读取不够：用户会看到同一个话题被记两遍、关系状态两份、
主动消息按两个身份各发一条。所以这里做**物理合并**。

合并最怕的不是漏搬，而是搬到一半主键冲突（关系状态每个 persona 只能
有一行）、或者搬完之后旧 key 还能读到东西。下面的测试盯着这两点。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory import db, identity, merge

NOW = "2026-11-10T12:00:00+00:00"


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


def seed_event(conn, person_key: str, *, audience: list[str] | None = None) -> None:
    audience = audience or [person_key]
    conn.execute(
        """INSERT INTO events (occurred_at, person_key, platform, channel_id,
               scope, audience, audience_key, source, content, created_at)
           VALUES (?, ?, 'qq', 'private:1', 'personal', ?, ?, 'user_message',
                   '你好', ?)""",
        (NOW, person_key, '["' + '","'.join(audience) + '"]', audience[0], NOW),
    )


def seed_memory(conn, person_key: str) -> None:
    conn.execute(
        """INSERT INTO memories (person_key, type, text, scope, audience,
               audience_key, created_at, updated_at)
           VALUES (?, 'fact', '他喜欢猫', 'personal', ?, ?, ?, ?)""",
        (person_key, '["' + person_key + '"]', person_key, NOW, NOW),
    )


def seed_state(conn, person_key: str, *, affection: float, updated_at: str,
               last_proactive_at: str | None = None) -> None:
    conn.execute(
        """INSERT INTO relationship_state (persona_key, person_key, affection,
               last_proactive_at, updated_at)
           VALUES ('merlin', ?, ?, ?, ?)""",
        (person_key, affection, last_proactive_at, updated_at),
    )


def seed_all_person_rows(conn, person_key: str) -> None:
    seed_event(conn, person_key)
    seed_memory(conn, person_key)
    seed_state(conn, person_key, affection=30.0, updated_at=NOW)
    conn.execute(
        """INSERT INTO relationship_events (persona_key, person_key, kind, created_at)
           VALUES ('merlin', ?, 'chat', ?)""",
        (person_key, NOW),
    )
    conn.execute(
        """INSERT INTO decision_log (persona_key, channel_id, person_key, verdict,
               created_at)
           VALUES ('merlin', 'qq_private', ?, 'reply', ?)""",
        (person_key, NOW),
    )
    conn.execute(
        """INSERT INTO appraisal_log (persona_key, person_key, channel_id, status,
               created_at)
           VALUES ('merlin', ?, 'qq_private', 'ok', ?)""",
        (person_key, NOW),
    )
    conn.execute(
        """INSERT INTO pending_topics (persona_key, person_key, topic, created_at,
               expires_at)
           VALUES ('merlin', ?, '面试怎么样', ?, ?)""",
        (person_key, NOW, NOW),
    )
    conn.execute(
        """INSERT INTO proactive_log (persona_key, target_key, channel_id, kind,
               status, created_at)
           VALUES ('merlin', ?, 'private:1', 'spontaneous', 'sent', ?)""",
        (person_key, NOW),
    )
    conn.execute(
        """INSERT INTO appraisal_usage (scope, day, used) VALUES (?, '2026-11-10', 3)""",
        (f"person:{person_key}",),
    )
    conn.commit()


def count(conn, table: str, column: str, value: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)
        ).fetchone()[0]
    )


def test_merge_moves_every_person_scoped_row(conn) -> None:
    seed_all_person_rows(conn, "qq:1")

    moved = merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    assert moved["events"] == 1
    assert moved["memories"] == 1
    for table in (
        "events",
        "memories",
        "relationship_state",
        "relationship_events",
        "decision_log",
        "appraisal_log",
        "pending_topics",
    ):
        assert count(conn, table, "person_key", "qq:1") == 0
    assert count(conn, "proactive_log", "target_key", "qq:1") == 0
    assert count(conn, "proactive_log", "target_key", "wp:2") == 1

    audience = conn.execute(
        "SELECT audience, audience_key FROM events WHERE person_key = 'wp:2'"
    ).fetchone()
    assert audience["audience"] == '["wp:2"]'
    assert audience["audience_key"] == "wp:2"

    assert merge.canonical_key(conn, "qq:1") == "wp:2"
    assert count(conn, "persons", "person_key", "qq:1") == 0


def test_merge_moves_appraisal_usage(conn) -> None:
    seed_all_person_rows(conn, "qq:1")
    conn.execute(
        """INSERT INTO appraisal_usage (scope, day, used)
           VALUES ('person:wp:2', '2026-11-10', 2)"""
    )
    conn.commit()

    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    row = conn.execute(
        "SELECT used FROM appraisal_usage WHERE scope = 'person:wp:2'"
    ).fetchone()
    assert row["used"] == 5
    assert count(conn, "appraisal_usage", "scope", "person:qq:1") == 0


def test_merge_keeps_latest_relationship_state_per_persona(conn) -> None:
    seed_state(
        conn,
        "wp:2",
        affection=70.0,
        updated_at="2026-11-10T00:00:00+00:00",
        last_proactive_at="2026-11-09T00:00:00+00:00",
    )
    seed_state(
        conn,
        "qq:1",
        affection=10.0,
        updated_at="2026-11-08T00:00:00+00:00",
        last_proactive_at="2026-11-01T00:00:00+00:00",
    )
    conn.commit()

    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    rows = conn.execute(
        "SELECT * FROM relationship_state WHERE person_key = 'wp:2'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["affection"] == 70.0
    # 主动消息的间隔算的是“最近一次”，两个身份取较晚的那个。
    assert rows[0]["last_proactive_at"] == "2026-11-09T00:00:00+00:00"


def test_newer_source_state_wins(conn) -> None:
    seed_state(conn, "wp:2", affection=10.0, updated_at="2026-11-01T00:00:00+00:00")
    seed_state(
        conn,
        "qq:1",
        affection=55.0,
        updated_at="2026-11-10T00:00:00+00:00",
        last_proactive_at="2026-11-10T00:00:00+00:00",
    )
    conn.commit()

    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    row = conn.execute(
        "SELECT * FROM relationship_state WHERE person_key = 'wp:2'"
    ).fetchone()
    assert row["affection"] == 55.0
    assert row["last_proactive_at"] == "2026-11-10T00:00:00+00:00"


def test_merge_is_idempotent(conn) -> None:
    seed_all_person_rows(conn, "qq:1")

    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")
    second = merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    assert second["events"] == 0
    assert second["memories"] == 0
    assert count(conn, "events", "person_key", "wp:2") == 1


def test_alias_chain_resolves_to_target(conn) -> None:
    seed_all_person_rows(conn, "qq:1")
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    merge.merge_person(conn, source_keys=["wp:2"], target_key="wp:3")

    assert merge.canonical_key(conn, "qq:1") == "wp:3"
    assert count(conn, "events", "person_key", "wp:3") == 1


def test_linked_keys_includes_aliases(conn) -> None:
    seed_all_person_rows(conn, "qq:1")
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    keys = identity.linked_keys(conn, "qq:1")

    assert keys[0] == "qq:1"
    assert "wp:2" in keys


def test_delete_person_data_clears_aliases_and_all_tables(conn) -> None:
    seed_all_person_rows(conn, "qq:1")
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    merge.delete_person_data(conn, "wp:2")

    for table in (
        "events",
        "memories",
        "relationship_state",
        "relationship_events",
        "decision_log",
        "appraisal_log",
        "pending_topics",
        "proactive_log",
        "persons",
        "person_aliases",
        "appraisal_usage",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_already_merged_source_is_not_dragged_to_a_third_identity(conn) -> None:
    """同一个 QQ 换绑到新账号：旧账号的记忆不能被新账号拖走。"""
    seed_all_person_rows(conn, "qq:1")
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")

    moved = merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:3")

    assert moved["events"] == 0
    assert count(conn, "events", "person_key", "wp:2") == 1
    assert count(conn, "events", "person_key", "wp:3") == 0
