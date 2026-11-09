"""待续话题：想说但这一轮说不出来的话。

私聊里沉默有两种：一种是真的不想接，另一种是累了、心情差。第二种
如果什么都不留，下一次主动就只能空泛地问“在吗”。这里把用户那条
消息原样记下来，作为以后主动消息的抓手。

它同时也是主动消息的“素材库”和“刹车”：

- 每天最多记一条、七天最多三条 —— 记满了说明这段时间她状态一直不好，
  再多主动消息只会更烦人；
- 72 小时没发出去就作废 —— 三天前的“面试怎么样”再问已经没意义；
- 同一条话不重复记 —— 用户在刷屏时不该攒出十几条一样的待续。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.relationship.appraisal_budget import day_start_iso
from app.relationship.state import DEFAULT_TIMEZONE, RelationshipState, now_iso, parse_iso

# 低到什么程度算“想说但说不出来”。
PENDING_MAX_ENERGY = 0.35
PENDING_MAX_VALENCE = -0.2
# 存下来多久还值得再提；发出去以后就再也不提。
PENDING_TTL_HOURS = 72
PENDING_DAILY_LIMIT = 1
PENDING_WEEKLY_LIMIT = 3
TOPIC_MAX_CHARS = 200

# 只有“正常”和“缓过来了但还有疤”这两种阶段可以存待续话题。
# 受伤、冷战、退缩时不存：那时候不回是态度，不是没力气。
_SAFE_STAGES = ("normal", "normal_with_scar")


def pending_reason(state: RelationshipState) -> str | None:
    """返回待续话题的成因；不该存时返回 `None`。

    先看关系是否安全：对这个人反感到负数、或在冲突阶段里，沉默是
    关系本身的表现，不该被包装成“她累了”。
    """
    if state.conflict_stage not in _SAFE_STAGES:
        return None
    if state.affection < 0:
        return None
    if state.energy <= PENDING_MAX_ENERGY:
        return "tired"
    if state.valence <= PENDING_MAX_VALENCE:
        return "low_mood"
    return None


def _drop_expired(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    stamp: str,
) -> None:
    conn.execute(
        """UPDATE pending_topics SET status = 'dropped'
            WHERE persona_key = ? AND person_key = ?
              AND status = 'open' AND expires_at < ?""",
        (persona_key, person_key, stamp),
    )


def maybe_create_pending_topic(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    person_key: str,
    topic: str,
    state: RelationshipState,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> int | None:
    """记一条待续话题，返回行号；任何一道闸门拦住时返回 `None`。"""
    reason = pending_reason(state)
    if reason is None:
        return None

    text = (topic or "").strip()[:TOPIC_MAX_CHARS]
    if not text:
        return None

    stamp = now or now_iso()
    _drop_expired(conn, persona_key, person_key, stamp)

    latest = conn.execute(
        """SELECT topic FROM pending_topics
            WHERE persona_key = ? AND person_key = ? AND status = 'open'
            ORDER BY id DESC LIMIT 1""",
        (persona_key, person_key),
    ).fetchone()
    if latest is not None and latest["topic"] == text:
        conn.commit()
        return None

    # “今天”是一个窗口，不是单边条件：将来时间被写进来（时钟回拨、
    # 测试里倒序造数据）时，不该算进今天的额度。
    today = day_start_iso(stamp, timezone_name)
    tomorrow = (
        parse_iso(today) + timedelta(days=1)
    ).isoformat(timespec="seconds")
    today_count = conn.execute(
        """SELECT COUNT(*) FROM pending_topics
            WHERE persona_key = ? AND person_key = ?
              AND created_at >= ? AND created_at < ?""",
        (persona_key, person_key, today, tomorrow),
    ).fetchone()[0]
    if today_count >= PENDING_DAILY_LIMIT:
        conn.commit()
        return None

    week_ago = (
        parse_iso(stamp) - timedelta(days=7)
    ).isoformat(timespec="seconds")
    week_count = conn.execute(
        """SELECT COUNT(*) FROM pending_topics
            WHERE persona_key = ? AND person_key = ? AND created_at >= ?""",
        (persona_key, person_key, week_ago),
    ).fetchone()[0]
    if week_count >= PENDING_WEEKLY_LIMIT:
        conn.commit()
        return None

    expires_at = (
        parse_iso(stamp) + timedelta(hours=PENDING_TTL_HOURS)
    ).isoformat(timespec="seconds")
    cursor = conn.execute(
        """INSERT INTO pending_topics (
               persona_key, person_key, topic, reason, status,
               created_at, expires_at
           ) VALUES (?, ?, ?, ?, 'open', ?, ?)""",
        (persona_key, person_key, text, reason, stamp, expires_at),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def open_topic(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> sqlite3.Row | None:
    """取最早一条还没过期的待续话题。顺手把过期的标成 `dropped`。"""
    stamp = now or now_iso()
    _drop_expired(conn, persona_key, person_key, stamp)
    conn.commit()
    return conn.execute(
        """SELECT * FROM pending_topics
            WHERE persona_key = ? AND person_key = ?
              AND status = 'open' AND expires_at >= ?
            ORDER BY id LIMIT 1""",
        (persona_key, person_key, stamp),
    ).fetchone()


def mark_topic_sent(
    conn: sqlite3.Connection, topic_id: int, *, now: str | None = None
) -> None:
    """标记已发出：之后不会再被取出来，也不会重复提起。"""
    conn.execute(
        "UPDATE pending_topics SET status = 'sent', sent_at = ? WHERE id = ?",
        (now or now_iso(), int(topic_id)),
    )
    conn.commit()
