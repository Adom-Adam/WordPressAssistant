"""主动消息的读写：日志表、时间线事件、状态时间戳。

`proactive_log` 记的是**每一次想过**，包括没发出去的。这样限额和去重
才有依据：不发也是一种决定，不该因为“反正没发”就每次都重新想一遍。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from app.memory.events import record_event
from app.relationship.state import get_state, now_iso, save_state


def record_proactive(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    target_key: str,
    channel_id: str,
    kind: str,
    thought: str = "",
    score: float = 0.0,
    threshold: float = 0.0,
    status: str,
    reason: str = "",
    event_id: int | None = None,
    now: str | None = None,
    commit: bool = True,
) -> int:
    """写一行主动日志，返回行号。`status` 为 `sent` / `skipped` / `failed`。"""
    cursor = conn.execute(
        """INSERT INTO proactive_log (
               persona_key, target_key, channel_id, kind, thought, score,
               threshold, status, reason, event_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            persona_key,
            target_key,
            channel_id,
            kind,
            (thought or "")[:500],
            float(score),
            float(threshold),
            status,
            (reason or "")[:200],
            event_id,
            now or now_iso(),
        ),
    )
    if commit:
        conn.commit()
    return int(cursor.lastrowid or 0)


def recent_thoughts(
    conn: sqlite3.Connection, persona_key: str, target_key: str, limit: int = 3
) -> list[str]:
    """最近真正发出去的几句，新→旧。用来避开重复的话题。"""
    rows = conn.execute(
        """SELECT thought FROM proactive_log
            WHERE persona_key = ? AND target_key = ? AND status = 'sent'
            ORDER BY id DESC LIMIT ?""",
        (persona_key, target_key, limit),
    ).fetchall()
    return [row["thought"] for row in rows if row["thought"]]


def recent_kinds(
    conn: sqlite3.Connection, persona_key: str, target_key: str, limit: int = 3
) -> tuple[str, ...]:
    """最近发出去的念头类型，新→旧。同一种连着出现会被降权。"""
    rows = conn.execute(
        """SELECT kind FROM proactive_log
            WHERE persona_key = ? AND target_key = ? AND status = 'sent'
            ORDER BY id DESC LIMIT ?""",
        (persona_key, target_key, limit),
    ).fetchall()
    return tuple(row["kind"] for row in rows)


def sent_count_since(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    since: str,
    target_key: str | None = None,
) -> int:
    """`since` 之后发出去几条。`target_key` 为空时统计全站。"""
    if target_key is None:
        row = conn.execute(
            """SELECT COUNT(*) FROM proactive_log
                WHERE persona_key = ? AND status = 'sent' AND created_at >= ?""",
            (persona_key, since),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COUNT(*) FROM proactive_log
                WHERE persona_key = ? AND target_key = ?
                  AND status = 'sent' AND created_at >= ?""",
            (persona_key, target_key, since),
        ).fetchone()
    return int(row[0])


def last_sent_at(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    persona_key: str | None = None,
) -> str | None:
    """这个人上一次收到主动消息的时间；没有则为 `None`。"""
    if persona_key is None:
        row = conn.execute(
            """SELECT MAX(created_at) FROM proactive_log
                WHERE target_key = ? AND status = 'sent'""",
            (target_key,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT MAX(created_at) FROM proactive_log
                WHERE target_key = ? AND persona_key = ? AND status = 'sent'""",
            (target_key, persona_key),
        ).fetchone()
    return row[0] if row and row[0] else None


def record_proactive_event(
    conn: sqlite3.Connection,
    *,
    person_key: str,
    channel_id: str,
    scope: str,
    audience: list[str],
    content: str,
    now: str | None = None,
) -> int:
    """把主动说出的话写进时间线，返回事件行号。

    主动消息必须进时间线：下一轮她得知道“上一条是自己先说的”，
    否则会把同一件事反复当成新话题。
    """
    return record_event(
        conn,
        person_key=person_key,
        platform="qq",
        channel_id=channel_id,
        scope=scope,
        audience=audience,
        source="proactive",
        content=content,
        occurred_at=now,
    )


def touch_last_proactive(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    now: str | None = None,
    commit: bool = True,
) -> None:
    """刷新 `last_proactive_at`。没有状态行时什么也不做。

    不调用 `get_or_create_state`：主动消息不该凭空给一个从没聊过的人
    建关系状态。
    """
    state = get_state(conn, persona_key, person_key)
    if state is None:
        return
    save_state(
        conn,
        replace(state, last_proactive_at=now or now_iso()),
        commit=commit,
    )


# 生成主动消息时要参考的上下文：用户说的、她回的、她主动说的。
_CONTEXT_SOURCES = ("user_message", "assistant_message", "proactive")


def recent_channel_events(
    conn: sqlite3.Connection, person_key: str, channel_id: str, limit: int = 6
) -> list[str]:
    """这个频道里最近的几句话，新→旧。

    只取这三种来源：系统事件（如“忽略了一条消息”）不是对话内容，
    拼进提示词只会让模型困惑。
    """
    placeholders = ", ".join("?" for _ in _CONTEXT_SOURCES)
    rows = conn.execute(
        f"""SELECT content FROM events
             WHERE person_key = ? AND channel_id = ?
               AND source IN ({placeholders})
             ORDER BY id DESC LIMIT ?""",
        (person_key, channel_id, *_CONTEXT_SOURCES, limit),
    ).fetchall()
    return [row["content"] for row in rows]
