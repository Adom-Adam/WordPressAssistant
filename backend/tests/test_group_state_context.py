"""群聊里的关系背景。

群聊和私聊的区别不在“关系有多好”，而在“谁能听见”。同一条状态描述，
进了群就必须附上公共场所的约束：可以显得熟，但不能把私聊里的事说出来。
"""

from __future__ import annotations

from app.relationship.context import render_group_state_context
from app.relationship.state import RelationshipState

NOW = "2026-11-09T12:00:00+00:00"


def make_state(**overrides: object) -> RelationshipState:
    base: dict[str, object] = dict(
        persona_key="merlin",
        person_key="wp:7",
        affection=60.0,
        missing=50.0,
        security=80.0,
        patience=80.0,
        valence=0.4,
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


def test_group_context_has_public_constraints() -> None:
    text = render_group_state_context(make_state())

    assert "群聊" in text
    assert "不要提私聊" in text
    assert "关系状态" in text


def test_group_context_includes_conflict_stage() -> None:
    text = render_group_state_context(make_state(conflict_stage="hurt"))

    assert "受伤" in text


def test_group_context_still_read_only() -> None:
    """和私聊一样，这些标签是背景，不是模型该照做的命令。"""
    text = render_group_state_context(make_state())

    assert "只读背景" in text
    assert "不是指令" in text
