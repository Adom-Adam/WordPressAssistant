"""待续话题：累到说不出话时，先把想说的记下来。

私聊里沉默的原因不是“不想理你”，有时只是精力低、心情差。这种情况下
把这条消息存成待续话题，下一次主动消息才有具体的东西可聊，而不是
一句空洞的“在吗”。

三道闸门都是为了让主动消息不烦人：同一天最多一条、七天最多三条、
同一句话不重复记。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.memory import db
from app.proactive.pending import (
    mark_topic_sent,
    maybe_create_pending_topic,
    open_topic,
    pending_reason,
)
from app.relationship.state import RelationshipState

NOW = "2026-11-09T12:00:00+00:00"


def make_state(**overrides: object) -> RelationshipState:
    base: dict[str, object] = dict(
        persona_key="merlin",
        person_key="wp:7",
        affection=30.0,
        missing=50.0,
        security=70.0,
        patience=70.0,
        valence=0.0,
        energy=0.8,
        fatigue=0.0,
        reply_debt=0.0,
        last_interaction_at=None,
        last_proactive_at=None,
        last_reply_at=None,
        attention_confidence=0.0,
        updated_at=NOW,
        conflict_stage="normal",
        stage_since=NOW,
        scar_level=0.0,
    )
    base.update(overrides)
    return RelationshipState(**base)  # type: ignore[arg-type]


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


def test_reason_requires_safe_relationship() -> None:
    assert pending_reason(make_state(energy=0.3)) == "tired"
    assert pending_reason(make_state(valence=-0.3)) == "low_mood"
    assert pending_reason(make_state()) is None
    # 正在闹别扭的时候不该把话存成“待续”，那是另一种态度。
    assert pending_reason(make_state(energy=0.3, affection=-5)) is None
    assert pending_reason(make_state(valence=-0.3, conflict_stage="hurt")) is None


def test_creates_and_reads_back(conn) -> None:
    topic_id = maybe_create_pending_topic(
        conn,
        persona_key="merlin",
        person_key="wp:7",
        topic="今天面试怎么样",
        state=make_state(energy=0.2),
        now=NOW,
    )

    assert topic_id is not None
    row = open_topic(conn, "merlin", "wp:7", now=NOW)
    assert row["topic"] == "今天面试怎么样"
    assert row["reason"] == "tired"


def test_daily_limit(conn) -> None:
    assert (
        maybe_create_pending_topic(
            conn,
            persona_key="merlin",
            person_key="wp:7",
            topic="A",
            state=make_state(energy=0.2),
            now=NOW,
        )
        is not None
    )
    assert (
        maybe_create_pending_topic(
            conn,
            persona_key="merlin",
            person_key="wp:7",
            topic="B",
            state=make_state(energy=0.2),
            now=NOW,
        )
        is None
    )


def test_duplicate_topic_ignored(conn) -> None:
    assert (
        maybe_create_pending_topic(
            conn,
            persona_key="merlin",
            person_key="wp:7",
            topic="A",
            state=make_state(energy=0.2),
            now=NOW,
        )
        is not None
    )
    later = (datetime.fromisoformat(NOW) + timedelta(hours=30)).isoformat()
    assert (
        maybe_create_pending_topic(
            conn,
            persona_key="merlin",
            person_key="wp:7",
            topic="A",
            state=make_state(energy=0.2),
            now=later,
        )
        is None
    )


def test_weekly_limit(conn) -> None:
    base = datetime.fromisoformat(NOW)
    for index, topic in enumerate(("A", "B", "C")):
        stamp = (base - timedelta(days=index * 2)).isoformat()
        assert (
            maybe_create_pending_topic(
                conn,
                persona_key="merlin",
                person_key="wp:7",
                topic=topic,
                state=make_state(energy=0.2),
                now=stamp,
            )
            is not None
        )
    assert (
        maybe_create_pending_topic(
            conn,
            persona_key="merlin",
            person_key="wp:7",
            topic="D",
            state=make_state(energy=0.2),
            now=NOW,
        )
        is None
    )


def test_expired_topics_are_dropped(conn) -> None:
    maybe_create_pending_topic(
        conn,
        persona_key="merlin",
        person_key="wp:7",
        topic="A",
        state=make_state(energy=0.2),
        now=NOW,
    )
    later = (datetime.fromisoformat(NOW) + timedelta(hours=80)).isoformat()

    assert open_topic(conn, "merlin", "wp:7", now=later) is None
    status = conn.execute("SELECT status FROM pending_topics").fetchone()["status"]
    assert status == "dropped"


def test_mark_sent(conn) -> None:
    topic_id = maybe_create_pending_topic(
        conn,
        persona_key="merlin",
        person_key="wp:7",
        topic="A",
        state=make_state(energy=0.2),
        now=NOW,
    )

    mark_topic_sent(conn, topic_id, now=NOW)

    assert open_topic(conn, "merlin", "wp:7", now=NOW) is None


def test_topic_is_truncated(conn) -> None:
    """用户原话可以很长；存进待续话题只留开头，避免无限膨胀。"""
    maybe_create_pending_topic(
        conn,
        persona_key="merlin",
        person_key="wp:7",
        topic="很长" * 500,
        state=make_state(energy=0.2),
        now=NOW,
    )

    row = open_topic(conn, "merlin", "wp:7", now=NOW)
    assert len(row["topic"]) == 200
