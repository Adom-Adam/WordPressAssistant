"""表情标签的受控词表。

模型只允许从这套情绪词里选，避免几个月后出现几十个近义词。
出处不在这里：它在 `sticker_origins` 表里由后台维护；模型可以提议
新出处，但先进 `proposed_origin` 等人工确认。
"""

from __future__ import annotations

EMOTION_TAGS: tuple[str, ...] = (
    "开心",
    "偷笑",
    "得意",
    "害羞",
    "撒娇",
    "委屈",
    "难过",
    "大哭",
    "生气",
    "埋怨",
    "无语",
    "白眼",
    "震惊",
    "疑惑",
    "尴尬",
    "紧张",
    "害怕",
    "疲惫",
    "困",
    "冷漠",
    "嘲讽",
    "鼓励",
    "安慰",
    "抱抱",
    "点赞",
    "打招呼",
    "晚安",
    "吃货",
    "加油",
    "比心",
)

_EMOTION_SET = {tag.casefold() for tag in EMOTION_TAGS}


def normalize_emotions(values: list[object]) -> list[str]:
    """只保留词表里的情绪，去重并保持原顺序。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(str(value or "").split()).strip()
        key = tag.casefold()
        if not tag or key in seen or key not in _EMOTION_SET:
            continue
        seen.add(key)
        result.append(tag)
    return result
