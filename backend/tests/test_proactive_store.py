"""主动消息的落账。

三件事要一起记：

- `proactive_log`：这一次想了什么、发没发、为什么没发，用来算限额和去重；
- `events`：真的发出去的那句话要进时间线，人设“记得自己说过什么”；
- `last_proactive_at`：状态里的时间戳，决定下一次最早什么时候还能想。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory import db
from app.proactive.store import (
    last_sent_at,
    record_proactive,
    record_proactive_event,
    recent_channel_events,
    recent_kinds,
    recent_thoughts,
    sent_count_since,
    touch_last_proactive,
)
from app.relationship.state import get_state


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(
            memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4
        ),
    )
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def test_record_and_read_thoughts(conn) -> None:
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="missing",
        thought="想你了",
        status="sent",
        now="2026-11-09T01:00:00+00:00",
    )
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="care",
        thought="早点休息",
        status="sent",
        now="2026-11-09T02:00:00+00:00",
    )
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="missing",
        thought="跳过",
        status="skipped",
        now="2026-11-09T03:00:00+00:00",
    )

    assert recent_thoughts(conn, "merlin", "wp:7") == ["早点休息", "想你了"]
    assert recent_kinds(conn, "merlin", "wp:7") == ("care", "missing")


def test_counts_respect_target_and_since(conn) -> None:
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="missing",
        status="sent",
        now="2026-11-09T01:00:00+00:00",
    )
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:8",
        channel_id="private:10002",
        kind="missing",
        status="sent",
        now="2026-11-09T02:00:00+00:00",
    )

    assert (
        sent_count_since(
            conn, persona_key="merlin", since="2026-11-09T00:00:00+00:00"
        )
        == 2
    )
    assert (
        sent_count_since(
            conn,
            persona_key="merlin",
            since="2026-11-09T00:00:00+00:00",
            target_key="wp:7",
        )
        == 1
    )
    assert (
        sent_count_since(
            conn, persona_key="merlin", since="2026-11-09T01:30:00+00:00"
        )
        == 1
    )


def test_last_sent_at(conn) -> None:
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="missing",
        status="sent",
        now="2026-11-09T01:00:00+00:00",
    )

    assert last_sent_at(conn, target_key="wp:7") == "2026-11-09T01:00:00+00:00"


def test_skipped_rows_do_not_count_as_sent(conn) -> None:
    record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="missing",
        status="skipped",
        now="2026-11-09T01:00:00+00:00",
    )

    assert (
        sent_count_since(
            conn, persona_key="merlin", since="2026-11-09T00:00:00+00:00"
        )
        == 0
    )
    assert last_sent_at(conn, target_key="wp:7") is None


def test_proactive_event_is_visible_to_person(conn) -> None:
    event_id = record_proactive_event(
        conn,
        person_key="wp:7",
        channel_id="private:10001",
        scope="personal",
        audience=["wp:7"],
        content="在忙吗",
        now="2026-11-09T01:00:00+00:00",
    )

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["source"] == "proactive"
    assert row["audience_key"] == "wp:7"


def test_touch_last_proactive(conn) -> None:
    conn.execute(
        """INSERT INTO relationship_state (persona_key, person_key, updated_at)
           VALUES ('merlin', 'wp:7', '2026-11-09T00:00:00+00:00')"""
    )
    conn.commit()

    touch_last_proactive(conn, "merlin", "wp:7", now="2026-11-09T05:00:00+00:00")

    state = get_state(conn, "merlin", "wp:7")
    assert state is not None
    assert state.last_proactive_at == "2026-11-09T05:00:00+00:00"


def test_touch_last_proactive_without_state_is_noop(conn) -> None:
    """没有关系状态的人（比如刚绑定还没聊过）不该被这次主动凭空造出来。"""
    touch_last_proactive(conn, "merlin", "wp:404", now="2026-11-09T05:00:00+00:00")

    assert get_state(conn, "merlin", "wp:404") is None


def test_recent_channel_events(conn) -> None:
    for text in ("第一句", "第二句"):
        record_proactive_event(
            conn,
            person_key="wp:7",
            channel_id="private:10001",
            scope="personal",
            audience=["wp:7"],
            content=text,
        )

    assert recent_channel_events(conn, "wp:7", "private:10001") == ["第二句", "第一句"]
