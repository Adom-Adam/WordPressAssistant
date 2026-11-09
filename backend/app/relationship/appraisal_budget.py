"""模型评估的本地日与每日预算。

评估调用不扣用户聊天额度，但会真实花钱，所以单独限流：
- 全站每天默认 1000 次，每人每天默认 60 次；0 表示不限，但仍计数。
- “每天”按 `QUOTA_TIMEZONE`（默认 Asia/Shanghai）的本地午夜切换，
  不能用 UTC 日，否则国内用户会在早上 8 点遇到额度重置。

计数放在 SQLite 里并用 UPSERT 原子占位，多进程同时评估也不会超发。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.relationship.state import DEFAULT_TIMEZONE, _zone, now_iso, parse_iso

GLOBAL_SCOPE = "global"


def _local_now(now: str | None, timezone_name: str) -> datetime:
    return parse_iso(now or now_iso()).astimezone(_zone(timezone_name))


def local_day(now: str | None, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    """返回本地日期，格式 `YYYY-MM-DD`。"""
    return _local_now(now, timezone_name).strftime("%Y-%m-%d")


def day_start_iso(now: str | None, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    """返回本地午夜对应的 UTC ISO 时间，用于查询“今天”的事件。"""
    local = _local_now(now, timezone_name)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def _person_scope(person_key: str) -> str:
    return f"person:{person_key}"


def _take(conn: sqlite3.Connection, scope: str, day: str, limit: int) -> bool:
    """给一个 scope 原子地加 1；已达上限时返回 False 且不修改。"""
    cursor = conn.execute(
        """
        INSERT INTO appraisal_usage(scope, day, used) VALUES (?, ?, 1)
        ON CONFLICT(scope, day) DO UPDATE SET used = used + 1
        WHERE ? = 0 OR used < ?
        """,
        (scope, day, max(0, int(limit)), max(0, int(limit))),
    )
    return cursor.rowcount > 0


def _release(conn: sqlite3.Connection, scope: str, day: str) -> None:
    """把已经占掉的一次名额减回去（只用于同一事务内的回滚）。"""
    conn.execute(
        """
        UPDATE appraisal_usage SET used = used - 1
         WHERE scope = ? AND day = ? AND used > 0
        """,
        (scope, day),
    )


def reserve_budget(
    conn: sqlite3.Connection,
    *,
    person_key: str,
    day: str,
    global_limit: int,
    user_limit: int,
) -> bool:
    """同时占全站和用户两个名额；任一失败就回滚已占的部分。"""
    if not _take(conn, GLOBAL_SCOPE, day, global_limit):
        conn.commit()
        return False
    if not _take(conn, _person_scope(person_key), day, user_limit):
        # 全站名额已经加了，但用户名额不够：必须减回去，
        # 否则一个人把全站预算吃光而自己没有实际调用。
        _release(conn, GLOBAL_SCOPE, day)
        conn.commit()
        return False
    conn.commit()
    return True


def used_budget(
    conn: sqlite3.Connection, *, person_key: str, day: str
) -> tuple[int, int]:
    """返回 (全站已用, 该用户已用)，没有记录时都是 0。"""
    rows = {
        row["scope"]: int(row["used"])
        for row in conn.execute(
            "SELECT scope, used FROM appraisal_usage WHERE day = ? AND scope IN (?, ?)",
            (day, GLOBAL_SCOPE, _person_scope(person_key)),
        )
    }
    return rows.get(GLOBAL_SCOPE, 0), rows.get(_person_scope(person_key), 0)
