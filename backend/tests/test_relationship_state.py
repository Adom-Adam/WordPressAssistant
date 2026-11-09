from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.state import (
    affinity_for,
    apply_event,
    decay_state,
    energy_at,
    get_or_create_state,
    save_state,
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
    yield connection
    connection.close()


def dynamics(**overrides):
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


def test_energy_uses_local_hour_not_utc():
    curve = [0.0] * 8 + [1.0] * 16
    assert energy_at(
        "2026-01-01T00:00:00+00:00", curve, timezone_name="UTC"
    ) == pytest.approx(0.0)
    assert energy_at(
        "2026-01-01T00:00:00+00:00", curve, timezone_name="Asia/Shanghai"
    ) == pytest.approx(1.0)


def test_invalid_timezone_falls_back_to_utc():
    curve = [0.0] * 8 + [1.0] * 16
    assert energy_at(
        "2026-01-01T00:00:00+00:00", curve, timezone_name="Not/AZone"
    ) == pytest.approx(0.0)


def test_affinity_matches_default_curve():
    d = dynamics()
    assert affinity_for(-100, d) == pytest.approx(0.14, abs=0.01)
    assert affinity_for(-60, d) == pytest.approx(0.24, abs=0.01)
    assert affinity_for(-20, d) == pytest.approx(0.51, abs=0.01)
    assert affinity_for(0, d) == pytest.approx(0.70, abs=0.01)
    assert affinity_for(20, d) == pytest.approx(0.89, abs=0.01)
    assert affinity_for(40, d) == pytest.approx(1.05, abs=0.01)
    assert affinity_for(60, d) == pytest.approx(1.16, abs=0.01)
    assert affinity_for(85, d) == pytest.approx(1.20, abs=0.01)
    assert affinity_for(100, d) == pytest.approx(1.20)


def test_affinity_uses_custom_curve():
    d = dynamics(
        reply={
            "affinity_mid": 0.5,
            "affinity_amp": 0.5,
            "affinity_scale": 10,
            "affinity_min": 0.0,
            "affinity_max": 1.0,
        }
    )
    assert affinity_for(0, d) == pytest.approx(0.5)
    assert affinity_for(100, d) == pytest.approx(1.0)


def test_state_initializes_from_role(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="owner", dynamics=d)
    assert state.affection == 60
    assert state.missing == 50
    assert state.security == 70
    assert state.patience == 70
    assert state.valence == pytest.approx(d.mood.baseline_valence)
    assert 0 <= state.energy <= 1
    assert state.fatigue == 0
    assert state.reply_debt == 0


def test_fatigue_is_persisted(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    save_state(conn, replace(state, fatigue=0.4))

    loaded = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)

    assert loaded.fatigue == pytest.approx(0.4)


def test_unknown_role_falls_back_to_normal(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:2", role="stranger", dynamics=d)
    assert state.affection == 0


def test_persona_state_is_isolated(conn):
    d = dynamics()
    merlin = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    other = get_or_create_state(conn, "other", "wp:1", role="normal", dynamics=d)
    assert merlin.affection == other.affection == 0

    save_state(conn, replace(merlin, affection=99))

    assert get_or_create_state(
        conn, "merlin", "wp:1", role="normal", dynamics=d
    ).affection == 99
    assert get_or_create_state(
        conn, "other", "wp:1", role="normal", dynamics=d
    ).affection == 0


def test_affection_uses_unit_and_growth(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=10,
    )
    assert after.affection == pytest.approx(6.0)  # 0 + 10 * 1.0 * 0.6


def test_positive_growth_slows_near_cap(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    save_state(conn, replace(state, affection=30))
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=10,
    )
    expected = 30 + 10 * 1.0 * 0.6 * (1 - 30 / 60) ** 0.8
    assert after.affection == pytest.approx(expected)


def test_growth_stops_at_role_cap(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=100,
    )
    assert after.affection == pytest.approx(60.0)


def test_cap_does_not_pull_down_existing_affection(conn):
    """上限只限制增长，不把已经高于上限的旧数据拉回来。"""
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    save_state(conn, replace(state, affection=80))
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=1,
    )
    assert after.affection == pytest.approx(80.0)


def test_negative_affection_floors_at_minus_hundred(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="conflict", d_affection_units=-100,
    )
    assert after.affection == pytest.approx(-100.0)


def test_energy_delta_is_applied_through_fatigue(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_energy=-0.2,
        now="2026-01-01T10:00:00+00:00", timezone_name="UTC",
    )
    assert after.fatigue == pytest.approx(0.2)
    assert after.energy == pytest.approx(d.energy.hourly_curve[10] - 0.2)


def test_valence_delta_is_capped(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="mood", d_valence=1.0,
    )
    assert after.valence == pytest.approx(
        d.mood.baseline_valence + d.mood.valence_delta_cap
    )


def test_valence_decays_by_half_life(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        valence=1.0,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_state(conn, state)

    decayed = decay_state(state, d, now="2026-01-01T01:30:00+00:00")

    expected = d.mood.baseline_valence + (1.0 - d.mood.baseline_valence) * 0.5
    assert decayed.valence == pytest.approx(expected)


def test_reply_debt_decays_by_half_life(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        reply_debt=0.8,
        updated_at="2026-01-01T00:00:00+00:00",
    )

    decayed = decay_state(state, d, now="2026-01-01T08:00:00+00:00")

    assert decayed.reply_debt == pytest.approx(0.4)


def test_fatigue_decays_and_energy_recovers(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        fatigue=0.5,
        energy=0.5,
        updated_at="2026-01-01T10:00:00+00:00",
    )

    decayed = decay_state(
        state,
        d,
        now="2026-01-01T11:00:00+00:00",
        timezone_name="UTC",
    )

    assert decayed.fatigue == pytest.approx(0.25)
    assert decayed.energy == pytest.approx(d.energy.hourly_curve[11] - 0.25)


def test_daytime_chat_does_not_drag_late_night(conn):
    d = dynamics()
    state = get_or_create_state(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        now="2026-01-01T10:00:00+00:00", timezone_name="UTC",
    )
    state = replace(state, fatigue=0.3, updated_at="2026-01-01T10:00:00+00:00")

    late = decay_state(
        state, d, now="2026-01-01T23:00:00+00:00", timezone_name="UTC"
    )

    assert late.fatigue < 0.01
    assert late.energy == pytest.approx(d.energy.hourly_curve[23], abs=0.01)


def test_missing_security_patience_decay_over_time(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        missing=50,
        security=70,
        patience=70,
        attention_confidence=0.8,
        updated_at="2026-01-01T00:00:00+00:00",
    )

    decayed = decay_state(state, d, now="2026-01-02T00:00:00+00:00")

    assert decayed.missing == pytest.approx(56.0)
    assert decayed.security == pytest.approx(69.0)
    assert decayed.patience == pytest.approx(72.0)
    assert decayed.attention_confidence == pytest.approx(0.4)


def test_decay_is_safe_for_zero_or_invalid_elapsed(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(state, valence=0.8, updated_at="not-a-time")

    decayed = decay_state(state, d, now="2026-01-01T00:00:00+00:00")

    assert decayed.valence == 0.8
    assert decayed.updated_at == "2026-01-01T00:00:00+00:00"


def test_raw_affection_points_apply_growth(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="appraisal", d_affection_points=2.0,
    )
    assert after.affection == pytest.approx(2.0 * 0.6)  # 0 分附近成长倍率 0.6


def test_raw_negative_affection_is_not_multiplied_by_growth(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="appraisal", d_affection_points=-2.0,
    )
    assert after.affection == pytest.approx(-2.0)


def test_apply_event_records_relationship_event(conn):
    d = dynamics()
    apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=10, d_valence=0.05, d_debt=0.1,
    )

    row = conn.execute("SELECT * FROM relationship_events").fetchone()

    assert row["persona_key"] == "merlin"
    assert row["kind"] == "chat"
    assert row["d_affection"] == pytest.approx(6.0)
    assert row["d_valence"] == pytest.approx(0.05)
    assert row["d_debt"] == pytest.approx(0.1)


def test_new_state_starts_normal(conn):
    state = get_or_create_state(
        conn, "merlin", "wp:1", role="normal", dynamics=dynamics()
    )
    assert state.conflict_stage == "normal"
    assert state.scar_level == 0.0
    assert state.stage_since is not None
