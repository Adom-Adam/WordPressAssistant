"""主动消息的候选评分与念头。

分两层，故意拆开：

- `score_target` 回答“现在要不要想这个人”，只看关系本身的慢变量；
- `generate_thoughts` 回答“如果要想，可以想些什么”，是一条条念头，
  每条有自己的分数。念头不够高的就不发——有时候想起来了，但没什么
  好说的，那就先不说。
"""

from __future__ import annotations

import pytest

from app.proactive.candidates import generate_thoughts, pick_thought, score_target


def test_score_weights_are_exact() -> None:
    full = score_target(
        closeness=1.0,
        missing=100,
        gap_hours=72,
        quality=1.0,
        unfinished=1.0,
        energy=1.0,
        attention=1.0,
        repetition=0.0,
    )
    assert full == pytest.approx(0.95)

    cold = score_target(
        closeness=0.0,
        missing=0,
        gap_hours=0,
        quality=0.0,
        unfinished=0.0,
        energy=0.0,
        attention=0.0,
        repetition=1.0,
    )
    assert cold == pytest.approx(0.0)


def test_score_is_clamped() -> None:
    full = score_target(
        closeness=9,
        missing=999,
        gap_hours=9999,
        quality=9,
        unfinished=9,
        energy=9,
        attention=9,
        repetition=0,
    )
    assert full == pytest.approx(0.95)


def test_generate_thoughts_applies_bonuses() -> None:
    thoughts = generate_thoughts(
        0.70,
        has_pending=True,
        reply_debt=0.2,
        missing=80,
        valence=-0.3,
        recent_kinds=(),
    )
    by_kind = {thought.kind: thought.score for thought in thoughts}

    assert by_kind["event_followup"] == pytest.approx(0.80)
    assert by_kind["missing"] == pytest.approx(0.75)
    assert by_kind["care"] == pytest.approx(0.75)
    assert by_kind["companionship"] == pytest.approx(0.70)
    # 心情不好时不会想到“随便聊聊”，那是另一种情绪。
    assert "spontaneous" not in by_kind


def test_generate_thoughts_caps_at_four() -> None:
    thoughts = generate_thoughts(
        0.70,
        has_pending=True,
        reply_debt=0.2,
        missing=80,
        valence=0.3,
        recent_kinds=(),
    )

    assert len(thoughts) <= 4
    assert thoughts[0].kind == "event_followup"


def test_pick_thought_respects_threshold() -> None:
    thoughts = generate_thoughts(
        0.70,
        has_pending=False,
        reply_debt=0.0,
        missing=0,
        valence=0.0,
        recent_kinds=(),
    )

    assert pick_thought(thoughts, 0.72) is None
    assert pick_thought(thoughts, 0.60).kind == "companionship"


def test_recent_kind_penalty() -> None:
    fresh = generate_thoughts(
        0.70,
        has_pending=False,
        reply_debt=0.0,
        missing=0,
        valence=0.0,
        recent_kinds=(),
    )
    repeated = generate_thoughts(
        0.70,
        has_pending=False,
        reply_debt=0.0,
        missing=0,
        valence=0.0,
        recent_kinds=("companionship",) * 3,
    )

    assert repeated[0].score < fresh[0].score
