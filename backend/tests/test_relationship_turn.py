"""每轮对话对关系状态的影响。

这里验证的是“聊完之后状态怎么变”，不包括概率决策（那在
test_reply_decision.py）。关键边界：
- 已绑定的 QQ 取 wp 身份和两者中更高的角色；
- 正常回复涨好感、降精力、刷新 last_reply_at；
- 被忽略只加回复债，不刷新 last_reply_at；
- 不同 persona 的状态互不影响。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.state import get_or_create_state, save_state
from app.relationship.turn import (
    load_signals,
    record_reply,
    record_silence,
    relationship_subject,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    connection.execute(
        """
        INSERT INTO persons (person_key, first_seen_at, last_seen_at, role)
        VALUES (?, ?, ?, ?)
        """,
        ("wp:7", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "special"),
    )
    connection.commit()
    yield connection
    connection.close()


def dynamics(**overrides):
    data = {
        "schema": "l2d.persona/v1",
        "id": "merlin",
        "version": 1,
        "name": "梅林",
        "core": "C",
    }
    if overrides:
        data["dynamics"] = overrides
    return PersonaVersion.model_validate(data).dynamics


def test_bound_qq_uses_wp_key_and_higher_role(conn):
    person_key, role = relationship_subject(conn, 10001, 7)
    assert person_key == "wp:7"
    assert role == "special"


def test_unbound_qq_uses_qq_key_and_normal_role(conn):
    person_key, role = relationship_subject(conn, 10001, None)
    assert person_key == "qq:10001"
    assert role == "normal"


def test_load_signals_derives_affinity(conn):
    d = dynamics()
    signals = load_signals(conn, "merlin", "wp:7", role="normal", dynamics=d)
    assert signals.state.affection == 0
    assert signals.affinity == pytest.approx(1.50, abs=0.01)


def test_record_reply_grows_affection_and_sets_last_reply(conn):
    d = dynamics()
    state = record_reply(conn, "merlin", "wp:7", role="normal", dynamics=d)
    # 0 + 0.5 units * 1.0 unit * 0.6 growth = 0.3
    assert state.affection == pytest.approx(0.3)
    assert state.last_reply_at is not None
    assert state.energy < 1.0


def test_record_reply_adds_fatigue_and_lowers_energy(conn):
    d = dynamics()
    state = record_reply(
        conn,
        "merlin",
        "wp:7",
        role="normal",
        dynamics=d,
        now="2026-01-01T10:00:00+00:00",
        timezone_name="UTC",
    )
    assert state.fatigue == pytest.approx(d.energy.fatigue_per_turn)
    assert state.energy == pytest.approx(
        d.energy.hourly_curve[10] - state.fatigue, abs=0.01
    )


def test_record_silence_adds_debt_without_reply(conn):
    d = dynamics()
    state = record_silence(conn, "merlin", "wp:7", role="normal", dynamics=d)
    assert state.reply_debt == pytest.approx(d.reply.debt_increment)
    assert state.last_reply_at is None


def test_turn_signals_are_persona_isolated(conn):
    d = dynamics()
    record_reply(conn, "merlin", "wp:7", role="normal", dynamics=d)
    other = get_or_create_state(conn, "other", "wp:7", role="normal", dynamics=d)
    assert other.affection == 0


def test_load_signals_advances_conflict_by_time(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:7", role="normal", dynamics=d)
    stamp = "2026-01-01T12:00:00+00:00"
    save_state(
        conn,
        replace(
            state,
            conflict_stage="hurt",
            stage_since="2025-12-31T10:00:00+00:00",  # 26 小时前
            updated_at="2025-12-31T10:00:00+00:00",
        ),
    )

    signals = load_signals(
        conn, "merlin", "wp:7", role="normal", dynamics=d, now=stamp
    )

    assert signals.state.conflict_stage == "cold"
