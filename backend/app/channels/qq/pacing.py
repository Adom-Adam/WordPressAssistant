"""把一条回复切成多条 QQ 气泡，并给出打字间隔。

设计文档 5.4 的要求：
- 先按“约 32 字/段”定气泡数，再按标点权重挑切点；
- 段落 > 换行 > 句末 > 分号 > 逗号；
- 每段至少 8–12 字，过短的并回相邻段；
- 代码块、行内代码、URL 不切断。

RNG 由调用方注入：测试可以固定种子复现同一次切分，
线上则用共享的 `random.Random` 实例，避免每次重启后行为完全一样。
"""

from __future__ import annotations

import math
import random
import re

from app.persona.models import Dynamics

# 候选切点：(标记, 权重)。标记紧跟在切点前面，切点位置是标记之后。
_BOUNDARIES: tuple[tuple[str, float], ...] = (
    ("\n\n", 1.0),
    ("\n", 0.9),
    ("。", 0.85),
    ("！", 0.85),
    ("？", 0.85),
    ("!", 0.85),
    ("?", 0.85),
    ("…", 0.85),
    ("；", 0.45),
    (";", 0.45),
    ("：", 0.3),
    (":", 0.3),
    ("，", 0.18),
    (",", 0.18),
)

_URL = re.compile(r"https?://[^\s，。；！？、]+")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")

_SENTENCE_ENDINGS = ("。", "！", "？", "!", "?", "…", ".")


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """代码块、行内代码、URL 的区间；切点落在区间内部就丢弃。"""
    ranges: list[tuple[int, int]] = []
    for pattern in (_FENCE, _INLINE_CODE, _URL):
        ranges.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return ranges


def _inside(position: int, ranges: list[tuple[int, int]]) -> bool:
    # 区间是左闭右开；切点在 start 处是“刚好在代码前面”，允许。
    return any(start < position < end for start, end in ranges)


def _candidates(text: str, minimum: int) -> list[tuple[int, float]]:
    ranges = _protected_ranges(text)
    found: dict[int, float] = {}
    for marker, weight in _BOUNDARIES:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            position = index + len(marker)
            # 两侧都至少要有 minimum 个字符，否则这个切点一定会被并回去。
            if (
                minimum <= position <= len(text) - minimum
                and not _inside(position, ranges)
            ):
                # 同一位置可能命中多个标记（例如 “。\n”），取更高权重。
                found[position] = max(found.get(position, 0.0), weight)
            start = index + len(marker)
    return sorted(found.items())


def _weighted_sample(
    candidates: list[tuple[int, float]], count: int, rng: random.Random
) -> list[int]:
    """按权重无放回抽取 `count` 个切点。"""
    pool = list(candidates)
    chosen: list[int] = []
    for _ in range(min(count, len(pool))):
        total = sum(weight for _, weight in pool)
        if total <= 0:
            break
        needle = rng.random() * total
        for index, (position, weight) in enumerate(pool):
            needle -= weight
            if needle <= 0:
                chosen.append(position)
                pool.pop(index)
                break
        else:  # 浮点误差兜底
            chosen.append(pool.pop()[0])
    return sorted(chosen)


def _merge_short(pieces: list[str], minimum: int) -> list[str]:
    merged: list[str] = []
    for piece in pieces:
        if merged and (len(piece.strip()) < minimum or not piece.strip()):
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)

    # 第一段过短时没有“上一段”可并，改成并到下一段。
    if len(merged) > 1 and len(merged[0].strip()) < minimum:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)

    return [piece.strip() for piece in merged if piece.strip()]


def segment_bubbles(
    text: str, dynamics: Dynamics, *, rng: random.Random
) -> list[str]:
    """把回复切成若干条气泡；切不动或太短时保持一条。"""
    text = re.sub(r"\x00", "", str(text)).strip()
    if not text:
        return []

    cfg = dynamics.pacing
    target = max(1, cfg.target_chars_per_bubble)
    bubbles = min(cfg.max_bubbles, max(1, math.ceil(len(text) / target)))
    if bubbles <= 1:
        return [text]

    candidates = _candidates(text, cfg.min_bubble_chars)
    if not candidates:
        return [text]

    positions = _weighted_sample(candidates, bubbles - 1, rng)
    if not positions:
        return [text]

    pieces: list[str] = []
    last = 0
    for position in positions:
        pieces.append(text[last:position])
        last = position
    pieces.append(text[last:])

    merged = _merge_short(pieces, cfg.min_bubble_chars)
    return merged or [text]


def bubble_delay(previous: str, dynamics: Dynamics, *, rng: random.Random) -> float:
    """两条气泡之间的等待秒数：打字时间 + 句末停顿 + 自然间隔。"""
    cfg = dynamics.pacing
    seconds = len(previous) / cfg.chars_per_second
    if previous.rstrip().endswith(_SENTENCE_ENDINGS):
        seconds += rng.uniform(0.2, 0.4)
    seconds += rng.uniform(cfg.min_gap_ms, cfg.max_gap_ms) / 1000.0
    return seconds
