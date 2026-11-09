from __future__ import annotations

from app.relationship.context import render_state_context
from app.relationship.state import RelationshipState


def make_state(**overrides):
    values = {
        "persona_key": "merlin",
        "person_key": "wp:1",
        "affection": 0.0,
        "missing": 50.0,
        "security": 70.0,
        "patience": 70.0,
        "valence": 0.1,
        "energy": 1.0,
        "fatigue": 0.0,
        "reply_debt": 0.0,
        "last_interaction_at": None,
        "last_proactive_at": None,
        "last_reply_at": None,
        "attention_confidence": 0.0,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(overrides)
    return RelationshipState(**values)


def test_neutral_state_uses_plain_labels():
    text = render_state_context(make_state())

    assert "陌生人" in text
    assert "平静" in text
    assert "只读背景" in text
    assert "危机" in text


def test_low_energy_and_negative_affection_are_described():
    text = render_state_context(
        make_state(
            energy=0.1,
            affection=-50,
            valence=-0.6,
            patience=20,
            security=20,
        )
    )

    assert "精力很低" in text
    assert "反感" in text
    assert "语气会冷" in text
    assert "耐心快耗尽" in text
    assert "缺乏安全感" in text


def test_high_affection_and_high_energy_are_described():
    text = render_state_context(
        make_state(energy=0.9, affection=80, valence=0.7, missing=80)
    )

    assert "精神不错" in text
    assert "很亲近" in text
    assert "心情很好" in text
    assert "很想念" in text


def test_withdrawing_context_describes_distance():
    text = render_state_context(make_state(conflict_stage="withdrawing"))
    assert "有些退缩" in text
    assert "withdrawing" not in text


def test_normal_with_scar_mentions_not_dredging_up():
    text = render_state_context(
        make_state(conflict_stage="normal_with_scar", scar_level=0.5)
    )
    assert "还记着" in text
    assert "不要主动翻旧账" in text
    assert "0.5" not in text


def test_deep_scar_adds_reminder_line():
    text = render_state_context(
        make_state(conflict_stage="normal_with_scar", scar_level=0.5)
    )
    assert "再犯时" in text


def test_normal_state_has_no_conflict_line():
    assert "受伤" not in render_state_context(make_state())
