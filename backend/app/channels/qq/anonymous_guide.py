"""匿名用户的“去登记”提示节奏。

匿名聊天不写任何内容，但“提示过没有”本身要记一笔——每句话后面都跟一句
“去图书馆登记吧”，比不提示更烦人。这里只存 scope 和时间，不存说了什么。

七天一次是刻意选的：短期连聊几次只提一次；久到对方几乎忘了这件事，
再温和地提一次还算新鲜。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

GUIDE_INTERVAL_DAYS = 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def should_send(conn: sqlite3.Connection, scope: str, now: str | None = None) -> bool:
    """这个 scope 现在能不能再收到一次提示。

    时间戳损坏时按“没提过”处理：宁可多提一次，也不要因为一条脏数据
    让人永远收不到引导。
    """
    row = conn.execute(
        "SELECT last_sent_at FROM anonymous_guide_log WHERE scope = ?",
        (str(scope or "").strip(),),
    ).fetchone()
    if row is None:
        return True

    last = _parse(str(row["last_sent_at"]))
    current = _parse(str(now or _now()))
    if last is None or current is None:
        return True
    return (current - last).total_seconds() >= GUIDE_INTERVAL_DAYS * 86_400


def mark_sent(conn: sqlite3.Connection, scope: str, now: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO anonymous_guide_log (scope, last_sent_at)
        VALUES (?, ?)
        ON CONFLICT(scope) DO UPDATE SET last_sent_at = excluded.last_sent_at
        """,
        (str(scope or "").strip(), now or _now()),
    )
    conn.commit()
