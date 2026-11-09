from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.appraisal import (
    AppraisalResult,
    apply_appraisal,
    map_appraisal,
)
from app.relationship.state import get_or_create_state, save_state


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


def dynamics():
    return PersonaVersion.model_validate(
        {
            "schema": "l2d.persona/v1",
            "id": "merlin",
            "version": 1,
            "name": "梅林",
            "core": "CORE",
        }
    ).dynamics


def tease():
    return AppraisalResult("tease", 1.0, 1.0, 0.0, "")


def test_care_adds_affection(conn):
    delta = apply_appraisal(
        conn, "merlin", "wp:1", role="normal", dynamics=dynamics(),
        result=AppraisalResult("care", 1.0, 1.0, 0.2, "关心"),
    )
    assert delta.applied_affection == pytest.approx(1.0 * 0.6)


def test_confidence_below_threshold_skips_state(conn):
    delta = apply_appraisal(
        conn, "merlin", "wp:1", role="normal", dynamics=dynamics(),
        result=AppraisalResult("care", 1.0, 0.4, 0.9, "不确定"),
    )
    assert delta.skipped_reason == "low_confidence"
    assert delta.applied_affection == 0
    assert conn.execute("SELECT COUNT(*) FROM relationship_events").fetchone()[0] == 0


def test_tease_is_negative_at_low_affection(conn):
    delta = map_appraisal(
        result=tease(), affection=0, dynamics=dynamics(),
        positive_used=0, negative_used=0,
    )
    assert delta.d_affection < 0
    assert delta.d_valence < 0


def test_tease_turns_positive_at_high_affection(conn):
    delta = map_appraisal(
        result=tease(), affection=80, dynamics=dynamics(),
        positive_used=0, negative_used=0,
    )
    assert delta.d_affection > 0
    assert delta.d_valence > 0


def test_tease_at_max_affection_does_not_hurt(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    save_state(conn, replace(state, affection=80))
    applied = apply_appraisal(
        conn, "merlin", "wp:1", role="normal", dynamics=d, result=tease()
    )
    assert applied.applied_affection >= 0


def test_betrayal_hurts_more_at_high_affection(conn):
    d = dynamics()
    result = AppraisalResult("betrayal", 1.0, 1.0, 0.0, "")
    low = apply_appraisal(
        conn, "merlin", "wp:low", role="normal", dynamics=d, result=result
    )
    state = get_or_create_state(conn, "merlin", "wp:high", role="normal", dynamics=d)
    save_state(conn, replace(state, affection=80))
    high = apply_appraisal(
        conn, "merlin", "wp:high", role="normal", dynamics=d, result=result
    )
    assert abs(high.applied_affection) > abs(low.applied_affection)


def test_scar_amplifies_negative_event(conn):
    d = dynamics()
    result = AppraisalResult("betrayal", 1.0, 1.0, 0.0, "")
    clean = map_appraisal(
        result=result, affection=0, dynamics=d, positive_used=0, negative_used=0
    )
    scarred = map_appraisal(
        result=result, affection=0, dynamics=d, positive_used=0, negative_used=0,
        scar_level=0.5,
    )
    assert scarred.d_affection == pytest.approx(clean.d_affection * 1.5)


def test_scar_does_not_amplify_positive_event(conn):
    d = dynamics()
    result = AppraisalResult("care", 1.0, 1.0, 0.0, "")
    clean = map_appraisal(
        result=result, affection=50, dynamics=d, positive_used=0, negative_used=0
    )
    scarred = map_appraisal(
        result=result, affection=50, dynamics=d, positive_used=0, negative_used=0,
        scar_level=0.5,
    )
    assert scarred.d_affection == pytest.approx(clean.d_affection)


def test_daily_positive_cap_limits_total(conn):
    delta = map_appraisal(
        result=AppraisalResult("care", 1.0, 1.0, 0.0, ""),
        affection=0, dynamics=dynamics(), positive_used=2.8, negative_used=0,
    )
    assert delta.d_affection == pytest.approx(0.2)


def test_daily_negative_cap_limits_total(conn):
    delta = map_appraisal(
        result=AppraisalResult("betrayal", 1.0, 1.0, 0.0, ""),
        affection=0, dynamics=dynamics(), positive_used=0, negative_used=2.9,
    )
    assert delta.d_affection == pytest.approx(-0.1)


def test_boundary_reduces_security_and_patience(conn):
    delta = map_appraisal(
        result=AppraisalResult("boundary", 1.0, 1.0, -0.5, ""),
        affection=0, dynamics=dynamics(), positive_used=0, negative_used=0,
    )
    assert delta.d_affection < 0
    assert delta.d_security < 0
    assert delta.d_patience < 0


def test_neutral_writes_no_relationship_event(conn):
    delta = apply_appraisal(
        conn, "merlin", "wp:1", role="normal", dynamics=dynamics(),
        result=AppraisalResult("neutral", 1.0, 1.0, 0.0, ""),
    )
    assert delta.applied_affection == 0
    assert conn.execute("SELECT COUNT(*) FROM relationship_events").fetchone()[0] == 0
