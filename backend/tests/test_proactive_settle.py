"""主动消息的“被忽略”结算。

主动发出去的消息，过一段时间要有结论：对方接话了，还是没接。
这条结论会影响关系——但只按**证据强度**扣分，不能因为“没回”就默认
对方冷淡。所以置信度取“注意力衰减后的值”和“别的渠道是否活跃”的较大者：

- 24 小时后这个人一句话都没说 → 置信度 0，不扣分（可能就是没看见）；
- 他在别处活跃、只是没回这条 → 0.2；
- 他回到这个私聊里说了话 → 已经算回应，不扣分。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.proactive.settle import settle_ignores
from app.proactive.store import record_proactive
from app.relationship.state import RelationshipState, get_state, save_state

NOW = "2026-11-09T12:00:00+00:00"


def hours_ago(hours: float) -> str:
    return (datetime.fromisoformat(NOW) - timedelta(hours=hours)).isoformat()


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


def dynamics():
    return PersonaVersion.model_validate(
        {
            "schema": "l2d.persona/v1",
            "id": "merlin",
            "version": 1,
            "name": "梅林",
            "core": "C",
        }
    ).dynamics


def seed_state(conn, attention: float = 1.0) -> None:
    save_state(
        conn,
        RelationshipState(
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
            attention_confidence=attention,
            updated_at=hours_ago(1),
            conflict_stage="normal",
            stage_since=hours_ago(1),
            scar_level=0.0,
        ),
    )


def seed_sent(conn, created_at: str) -> int:
    return record_proactive(
        conn,
        persona_key="merlin",
        target_key="wp:7",
        channel_id="private:10001",
        kind="missing",
        thought="在忙吗",
        status="sent",
        now=created_at,
    )


def insert_user_message(conn, channel_id: str, occurred_at: str) -> None:
    conn.execute(
        """INSERT INTO events (occurred_at, person_key, platform, channel_id,
               scope, audience, audience_key, source, content, created_at)
           VALUES (?, 'wp:7', 'qq', ?, 'personal', '["wp:7"]',
                   'wp:7', 'user_message', '刚看到', ?)""",
        (occurred_at, channel_id, occurred_at),
    )
    conn.commit()


def test_ignored_applies_penalty_times_attention(conn) -> None:
    seed_state(conn, attention=1.0)
    row_id = seed_sent(conn, hours_ago(30))

    result = settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)

    assert result == {"answered": 0, "ignored": 1}
    state = get_state(conn, "merlin", "wp:7")
    assert state is not None
    assert state.affection == pytest.approx(29.8)
    status = conn.execute(
        "SELECT status FROM proactive_log WHERE id=?", (row_id,)
    ).fetchone()["status"]
    assert status == "ignored"


def test_zero_attention_does_not_drop_affection(conn) -> None:
    seed_state(conn, attention=0.0)
    seed_sent(conn, hours_ago(30))

    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)

    state = get_state(conn, "merlin", "wp:7")
    assert state is not None
    assert state.affection == pytest.approx(30.0)


def test_user_reply_marks_answered(conn) -> None:
    seed_state(conn)
    row_id = seed_sent(conn, hours_ago(30))
    insert_user_message(conn, "private:10001", hours_ago(29))

    result = settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)

    assert result == {"answered": 1, "ignored": 0}
    state = get_state(conn, "merlin", "wp:7")
    assert state is not None
    assert state.affection == pytest.approx(30.0)
    assert (
        conn.execute("SELECT status FROM proactive_log WHERE id=?", (row_id,))
        .fetchone()["status"]
        == "answered"
    )


def test_recent_message_is_not_settled(conn) -> None:
    seed_state(conn)
    seed_sent(conn, hours_ago(1))

    result = settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)

    assert result == {"answered": 0, "ignored": 0}


def test_settle_twice_does_not_double_penalize(conn) -> None:
    seed_state(conn)
    seed_sent(conn, hours_ago(30))
    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    after_first = get_state(conn, "merlin", "wp:7").affection

    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)

    assert get_state(conn, "merlin", "wp:7").affection == pytest.approx(after_first)


def test_activity_elsewhere_gives_partial_confidence(conn) -> None:
    seed_state(conn, attention=0.0)
    seed_sent(conn, hours_ago(30))
    insert_user_message(conn, "group:888", hours_ago(29))

    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)

    state = get_state(conn, "merlin", "wp:7")
    assert state is not None
    assert state.affection == pytest.approx(29.96)
