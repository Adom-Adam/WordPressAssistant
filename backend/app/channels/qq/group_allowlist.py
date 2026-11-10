"""群白名单的运行时存储。

`QQ_ALLOWED_GROUPS` 只在**第一次**启动时播种，之后白名单的唯一来源是
数据库。这样后台加群、删群立刻生效，不用登服务器改 `.env` 再重启。

“只播种一次”靠 `meta` 里的一行标记实现：没有标记才播种。否则管理员
删掉的群会在下次重启时被环境变量悄悄加回来，后台看起来像坏了。

默认拒绝：表为空就是“哪个群都不去”，和原来解析失败返回空集合的语义
一致——宁可不说，也不要误回陌生群。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.config import get_settings

SEED_META_KEY = "qq_allowed_groups_seeded_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (SEED_META_KEY,)
    ).fetchone()
    return row is not None


def ensure_seeded(
    conn: sqlite3.Connection, env_groups: set[int] | None = None
) -> None:
    """第一次调用时把环境变量里的群写进表；之后是空操作。"""
    if _seeded(conn):
        return
    groups = get_settings().qq_allowed_group_ids if env_groups is None else env_groups
    for group_id in sorted(int(g) for g in groups if int(g) > 0):
        conn.execute(
            "INSERT OR IGNORE INTO qq_allowed_groups (group_id, added_at) VALUES (?, ?)",
            (group_id, _now()),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (SEED_META_KEY, _now()),
    )
    conn.commit()


def allowed_group_ids(
    conn: sqlite3.Connection, *, env_groups: set[int] | None = None
) -> set[int]:
    """当前允许的群。`env_groups` 只影响第一次播种。"""
    ensure_seeded(conn, env_groups)
    return {
        int(row["group_id"])
        for row in conn.execute("SELECT group_id FROM qq_allowed_groups")
    }


def add_group(conn: sqlite3.Connection, group_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO qq_allowed_groups (group_id, added_at) VALUES (?, ?)",
        (int(group_id), _now()),
    )
    conn.commit()


def remove_group(conn: sqlite3.Connection, group_id: int) -> bool:
    """只删名单，不删这个群的策略行：重新加回来时配置还在。"""
    cursor = conn.execute(
        "DELETE FROM qq_allowed_groups WHERE group_id = ?", (int(group_id),)
    )
    conn.commit()
    return cursor.rowcount > 0
