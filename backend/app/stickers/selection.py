"""主动发表情时挑一张素材。

两件事：
1. 根据关系状态决定“该用哪种色彩”——冲突/冷淡只挑 cold，
   心情好挑 warm，平时 neutral；没有任何合适的就返回 None，让调用方
   回退去写文字，而不是拿一张可爱表情硬凑。
2. 在同一色彩里按标签匹配、最近使用惩罚和随机扰动做加权选择。
"""

from __future__ import annotations

import random
import sqlite3

from app.stickers import store

# 念头 → 想表达的情绪标签。只作为加分项，不强制命中。
KIND_TAGS: dict[str, tuple[str, ...]] = {
    "missing": ("想念", "撒娇", "抱抱", "关心"),
    "companionship": ("陪伴", "打招呼", "开心"),
    "spontaneous": ("开心", "分享", "得意"),
    "care": ("关心", "安慰", "抱抱"),
}

# 这些阶段里她不会用可爱/卖萌的表情；没有 cold 素材就不发。
COLD_STAGES = {"hurt", "cold", "withdrawing"}


def tones_for_state(conflict_stage: str, valence: float) -> tuple[str, ...]:
    if conflict_stage in COLD_STAGES:
        return ("cold",)
    if conflict_stage == "repairing":
        return ("neutral", "warm")
    if valence <= -0.25:
        return ("cold", "neutral")
    if valence >= 0.2:
        return ("warm", "neutral")
    return ("neutral", "warm")


def recent_sticker_ids(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    limit: int = 5,
) -> set[int]:
    rows = conn.execute(
        """SELECT sticker_id FROM proactive_log
            WHERE target_key = ? AND format = 'sticker' AND sticker_id IS NOT NULL
            ORDER BY id DESC LIMIT ?""",
        (target_key, max(1, int(limit))),
    ).fetchall()
    return {int(row["sticker_id"]) for row in rows}


def pick_for_thought(
    conn: sqlite3.Connection,
    *,
    tone: str,
    kind: str,
    target_key: str,
    rng: random.Random,
) -> store.Sticker | None:
    """在指定色彩里挑一张；没有候选返回 None。"""
    stickers = [
        item
        for item in store.list_stickers(conn, tone=tone, enabled=True, limit=5000)
        if item.id not in recent_sticker_ids(conn, target_key=target_key)
    ]
    if not stickers:
        return None

    wanted = {tag.casefold() for tag in KIND_TAGS.get(kind, ())}
    weights: list[float] = []
    for sticker in stickers:
        matches = len(
            wanted.intersection(tag.casefold() for tag in sticker.tags)
        )
        # 标签命中权重更高，但标签不命中也可能被选中——保留一点随机性。
        weights.append(1.0 + 2.0 * matches + rng.random())

    total = sum(weights)
    if total <= 0:
        return rng.choice(stickers)
    pick = rng.random() * total
    upto = 0.0
    for sticker, weight in zip(stickers, weights):
        upto += weight
        if pick <= upto:
            return sticker
    return stickers[-1]
