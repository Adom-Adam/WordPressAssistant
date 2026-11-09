"""冲突弧状态机的单元测试。

所有转移都是确定性的；这里只验证“给定状态和事件，得到什么阶段”，
不碰 QQ 适配器和模型。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.appraisal import AppraisalResult
from app.relationship.state import RelationshipState

NOW = "2026-11-09T12:00:00+00:00"


def minutes_ago(minutes: float) -> str:
    return (
        datetime.fromisoformat(NOW) - timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


def hours_ago(hours: float) -> str:
    return minutes_ago(hours * 60)


def days_ago(days: float) -> str:
    return minutes_ago(days * 24 * 60)


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


def make_dynamics(**overrides):
    data = {
        "schema": "l2d.persona/v1",
        "id": "merlin",
        "version": 1,
        "name": "梅林",
        "core": "CORE",
    }
    if overrides:
        data["dynamics"] = overrides
    return PersonaVersion.model_validate(data).dynamics


@pytest.fixture
def dynamics():
    return make_dynamics()


def make_state(**overrides) -> RelationshipState:
    base = RelationshipState(
        persona_key="merlin",
        person_key="wp:1",
        affection=30.0,
        missing=50.0,
        security=70.0,
        patience=70.0,
        valence=0.1,
        energy=0.8,
        fatigue=0.1,
        reply_debt=0.0,
        last_interaction_at=None,
        last_proactive_at=None,
        last_reply_at=None,
        attention_confidence=0.0,
        updated_at=hours_ago(1),
        conflict_stage="normal",
        stage_since=hours_ago(1),
        scar_level=0.0,
    )
    return replace(base, **overrides)


def appraisal(event: str, intensity: float = 0.9, confidence: float = 0.9):
    return AppraisalResult(
        event=event,
        intensity=intensity,
        confidence=confidence,
        reply_bias=0.0,
        reason="",
    )


def insert_appraisal(
    conn,
    event: str,
    *,
    confidence: float = 0.9,
    intensity: float = 0.9,
    created_at: str,
    status: str = "ok",
    person_key: str = "wp:1",
) -> None:
    conn.execute(
        """
        INSERT INTO appraisal_log (
            persona_key, person_key, channel_id, message_id, event, intensity,
            confidence, reply_bias, reason, raw_output, d_affection, d_valence,
            d_security, d_patience, applied_affection, applied_valence,
            latency_ms, status, skipped_reason, created_at
        ) VALUES ('merlin', ?, 'qq_private', NULL, ?, ?, ?, 0, '', '', 0, 0, 0, 0, 0, 0, 0, ?, '', ?)
        """,
        (person_key, event, intensity, confidence, status, created_at),
    )
    conn.commit()


# ---------------------------------------------------------------- Task 3
def test_stage_multiplier_reads_persona_config(dynamics):
    from app.relationship.conflict import stage_multiplier

    assert stage_multiplier("withdrawing", dynamics) == 0.5
    assert stage_multiplier("unknown", dynamics) == 1.0


def test_repair_points_depend_on_affection(dynamics):
    from app.relationship.conflict import repair_points_needed

    assert repair_points_needed(80, dynamics) == 1
    assert repair_points_needed(30, dynamics) == 2
    assert repair_points_needed(-10, dynamics) == 3


def test_positive_count_excludes_low_confidence(conn, dynamics):
    from app.relationship.conflict import positive_appraisal_count

    insert_appraisal(conn, "care", confidence=0.9, created_at=minutes_ago(10))
    insert_appraisal(conn, "care", confidence=0.3, created_at=minutes_ago(10))

    assert (
        positive_appraisal_count(conn, "merlin", "wp:1", since=hours_ago(1))
        == 1
    )


# ---------------------------------------------------------------- Task 4
def test_severe_betrayal_enters_hurt(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    after = advance_by_event(
        conn,
        make_state(),
        result=appraisal("betrayal", intensity=0.9, confidence=0.9),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "hurt"
    assert after.scar_level == pytest.approx(0.3)
    assert after.stage_since == NOW


def test_low_intensity_betrayal_does_not_enter_conflict(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    after = advance_by_event(
        conn,
        make_state(),
        result=appraisal("betrayal", intensity=0.2, confidence=0.9),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "normal"


def test_boundary_threshold_enters_hurt(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    for offset in (50, 40):
        insert_appraisal(
            conn, "boundary", confidence=0.8, created_at=minutes_ago(offset)
        )
    after = advance_by_event(
        conn,
        make_state(),
        result=appraisal("boundary", intensity=0.8, confidence=0.8),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "hurt"


def test_hurt_apology_enters_repairing_after_dwell(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    state = make_state(conflict_stage="hurt", stage_since=minutes_ago(31))
    after = advance_by_event(
        conn,
        state,
        result=appraisal("apology", intensity=0.9, confidence=0.9),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "repairing"


def test_hurt_apology_before_dwell_does_not_repair(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    state = make_state(conflict_stage="hurt", stage_since=minutes_ago(5))
    after = advance_by_event(
        conn,
        state,
        result=appraisal("apology", intensity=0.9, confidence=0.9),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "hurt"


def test_betrayal_while_repairing_goes_withdrawing(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    state = make_state(conflict_stage="repairing", stage_since=hours_ago(2))
    after = advance_by_event(
        conn,
        state,
        result=appraisal("betrayal", intensity=0.9, confidence=0.9),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "withdrawing"
    assert after.scar_level == pytest.approx(0.2)


# ---------------------------------------------------------------- Task 5
def test_hurt_goes_cold_after_24h(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="hurt", stage_since=hours_ago(25), patience=70)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "cold"


def test_hurt_stays_when_patience_is_fine(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="hurt", stage_since=hours_ago(13), patience=70)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "hurt"


def test_low_patience_goes_cold_after_12h(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="hurt", stage_since=hours_ago(13), patience=30)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "cold"


def test_cold_goes_withdrawing_when_security_low(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="cold", stage_since=hours_ago(49), security=20)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "withdrawing"


def test_cold_stays_when_security_is_ok(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="cold", stage_since=hours_ago(49), security=70)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "cold"


def test_withdrawing_softens_after_14_days(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="withdrawing", stage_since=days_ago(15))
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "normal_with_scar"
    assert after.scar_level >= 0.4


def test_repairing_goes_cold_without_new_positive_events(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(conflict_stage="repairing", stage_since=hours_ago(80))
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "cold"


def test_repairing_stays_with_recent_positive_event(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    insert_appraisal(conn, "care", confidence=0.9, created_at=hours_ago(10))
    state = make_state(conflict_stage="repairing", stage_since=hours_ago(80))
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "repairing"


def test_scar_fades_to_normal_after_week(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(
        conflict_stage="normal_with_scar",
        stage_since=days_ago(8),
        scar_level=0.05,
        updated_at=days_ago(8),
    )
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "normal"
    assert after.scar_level < 0.05


def test_scar_stays_in_normal_with_scar_when_recent(conn, dynamics):
    from app.relationship.conflict import advance_by_time

    state = make_state(
        conflict_stage="normal_with_scar", stage_since=days_ago(2), scar_level=0.4
    )
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "normal_with_scar"


def test_low_confidence_betrayal_does_not_enter_conflict(conn, dynamics):
    from app.relationship.conflict import advance_by_event

    after = advance_by_event(
        conn,
        make_state(),
        result=appraisal("betrayal", intensity=0.9, confidence=0.5),
        dynamics=dynamics,
        now=NOW,
    )
    assert after.conflict_stage == "normal"
