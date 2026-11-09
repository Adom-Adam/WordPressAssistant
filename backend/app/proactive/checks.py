"""主动消息发出前的检查。

这些函数都是纯判断，不做任何副作用——调度器按顺序调用，遇到第一个
`True` 就放弃这一轮，并把原因写进日志。分开写是为了能在日志里看出
“这次为什么没发”，而不是所有跳过都变成一个模糊的 `skipped`。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from app.relationship.state import DEFAULT_TIMEZONE, _zone, parse_iso

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# 比较相似度时忽略的字符：空白、常见中英文标点。只比较“说了什么”，
# 不比较“怎么排版”。
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:~…—\-—\"'“”‘’()（）\[\]【】]+")


def _minutes_of_day(stamp: str, timezone_name: str) -> int:
    local = parse_iso(stamp).astimezone(_zone(timezone_name))
    return local.hour * 60 + local.minute


def _parse_range(item: str) -> tuple[int, int] | None:
    """把 `HH:MM-HH:MM` 解析成分钟数；格式不对返回 `None`。"""
    try:
        start_text, end_text = item.split("-", 1)
    except ValueError:
        return None
    start_match = _HHMM.match(start_text.strip())
    end_match = _HHMM.match(end_text.strip())
    if not start_match or not end_match:
        return None
    start = int(start_match.group(1)) * 60 + int(start_match.group(2))
    end = int(end_match.group(1)) * 60 + int(end_match.group(2))
    return start, end


def in_quiet_hours(
    now: str,
    ranges: list[str],
    timezone_name: str = DEFAULT_TIMEZONE,
) -> bool:
    """此刻是否在安静时段内。支持跨午夜（如 `23:00-07:00`）。"""
    minute = _minutes_of_day(now, timezone_name)
    for item in ranges or []:
        parsed = _parse_range(item)
        if parsed is None:
            continue
        start, end = parsed
        if start == end:
            # 起止相同视为整天静默，这种配置几乎必然是笔误，
            # 但既然写了就照做，总比“悄悄发消息”安全。
            return True
        if start < end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:
            return True
    return False


def too_soon(last_at: str | None, now: str, minutes: int) -> bool:
    """距离上一次主动是否还不够久。`minutes <= 0` 表示不限制。"""
    if minutes <= 0 or not last_at:
        return False
    try:
        elapsed = (parse_iso(now) - parse_iso(last_at)).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed < minutes * 60


def over_limit(count: int, limit: int) -> bool:
    """是否已达上限。`limit <= 0` 表示不限制。"""
    return limit > 0 and count >= limit


def recently_active(
    conn: sqlite3.Connection,
    *,
    person_key: str,
    channel_id: str,
    now: str,
    minutes: int,
) -> bool:
    """这个人是不是刚刚才在这个频道说过话。

    只看用户消息，且比较 `occurred_at`（发生时间）而不是写入时间：
    补记的历史事件不该把“现在正在聊天”的判断弄反。
    """
    if minutes <= 0:
        return False
    try:
        since = (parse_iso(now) - timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        return False
    row = conn.execute(
        """SELECT 1 FROM events
            WHERE person_key = ? AND channel_id = ?
              AND source = 'user_message' AND occurred_at >= ?
            LIMIT 1""",
        (person_key, channel_id, since),
    ).fetchone()
    return row is not None


def _bigrams(text: str) -> set[str]:
    cleaned = _PUNCT.sub("", text or "")
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def is_repetitive(
    text: str, recent: list[str], *, threshold: float = 0.6
) -> bool:
    """这句话和最近说过的某一句是不是太像了。

    用字符 bigram 的 Jaccard 相似度：中文没有空格分词，bigram 足够
    抓住“同一句话换个标点”这种重复，又不会把两个不同的话题判成一样。
    """
    current = _bigrams(text)
    if not current:
        return False
    for item in recent or []:
        other = _bigrams(item)
        if not other:
            continue
        union = current | other
        if union and len(current & other) / len(union) >= threshold:
            return True
    return False
