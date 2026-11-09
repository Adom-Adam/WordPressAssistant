"""多气泡切分与打字节奏。

分气泡的原则是“像人打字”，不是“按字数硬切”：
- 先按目标长度定气泡数，再按标点权重挑切点；
- 代码块、行内代码和 URL 不切开；
- 太短的气泡并回上一段，避免出现“嗯。”单独一条。
"""

from __future__ import annotations

import random

from app.channels.qq.pacing import bubble_delay, segment_bubbles
from app.persona.models import PersonaVersion


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


def test_short_text_stays_one_bubble():
    d = dynamics()
    assert segment_bubbles("我在。", d, rng=random.Random(0)) == ["我在。"]


def test_long_text_splits_on_sentence_boundaries():
    d = dynamics()
    text = "今天风有点大。我出门的时候差点被吹回去。你那边下雨了吗？记得带伞。"
    bubbles = segment_bubbles(text, d, rng=random.Random(3))
    assert len(bubbles) >= 2
    assert "".join(bubbles).replace(" ", "") == text.replace(" ", "")
    assert all(bubble.strip() for bubble in bubbles)


def test_never_splits_inside_code_or_url():
    d = dynamics()
    text = "看这个 https://example.com/a/b/c 和 `print(1)` 都别拆。后面还有一句话。再一句。"
    bubbles = segment_bubbles(text, d, rng=random.Random(1))
    joined = "".join(bubbles)
    assert "https://example.com/a/b/c" in joined
    assert "`print(1)`" in joined
    for bubble in bubbles:
        assert bubble.count("`") % 2 == 0


def test_tiny_segments_are_merged():
    d = dynamics()
    text = "好。嗯。就这样吧，明天见。"
    bubbles = segment_bubbles(text, d, rng=random.Random(2))
    assert all(len(bubble) >= d.pacing.min_bubble_chars for bubble in bubbles)


def test_delay_is_bounded():
    d = dynamics()
    delay = bubble_delay("今天风有点大。", d, rng=random.Random(0))
    assert d.pacing.min_gap_ms / 1000 <= delay <= 10.0
