"""QQ 白名单：管理员替未绑定的 QQ 提前“登记”。

为什么需要它：绑定 QQ 要走博客登录，很多人嫌麻烦。站长希望几个熟人
（自己的小号、老读者）不注册也能正常使用记忆、关系、主动消息，于是
在后台把号加进来，等同于“这个人已经登记过了”。

两条边界：
- 名单只管“能不能按绑定用户对待”，**不复制身份**：这些号仍然是
  `qq:<uin>`，不会被绑到任何 WP 账号上。
- 移出名单只删这一行并把角色恢复成 normal，历史数据保持休眠；
  真的要删数据是另一个接口（`DELETE /admin/qq-whitelist/{uin}/data`）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.memory import db as memory_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(qq_uin: str) -> str:
    return str(qq_uin or "").strip()


def entry_for(conn: sqlite3.Connection, qq_uin: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM qq_whitelist WHERE qq_uin = ?", (_normalize(qq_uin),)
    ).fetchone()


def is_whitelisted(conn: sqlite3.Connection, qq_uin: str) -> bool:
    return entry_for(conn, qq_uin) is not None


def is_whitelisted_qq(qq_uin: str) -> bool:
    """适配器专用：自己开库。

    任何异常都按“不在白名单”处理。白名单库坏了应该退化成匿名聊天，
    而不是把整条消息链路打挂——匿名仍然能聊天，只是不记事。
    """
    try:
        conn = memory_db.connect()
    except Exception:  # noqa: BLE001 - 库打不开也不能影响回复
        return False
    try:
        memory_db.init_schema(conn)
        return is_whitelisted(conn, qq_uin)
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def add_entry(
    conn: sqlite3.Connection,
    qq_uin: str,
    *,
    note: str | None = None,
    added_by: str | None = None,
    now: str | None = None,
) -> None:
    """重复添加时保留第一次的 `added_at`，只更新备注与操作人。"""
    conn.execute(
        """
        INSERT INTO qq_whitelist (qq_uin, note, added_by, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(qq_uin) DO UPDATE SET
            note = COALESCE(excluded.note, qq_whitelist.note),
            added_by = COALESCE(excluded.added_by, qq_whitelist.added_by)
        """,
        (
            _normalize(qq_uin),
            (note or "").strip() or None,
            (str(added_by).strip() or None) if added_by is not None else None,
            now or _now(),
        ),
    )


def remove_entry(conn: sqlite3.Connection, qq_uin: str) -> bool:
    """移出名单并恢复角色；**不删数据**。返回是否真的删掉了一行。"""
    uin = _normalize(qq_uin)
    cursor = conn.execute("DELETE FROM qq_whitelist WHERE qq_uin = ?", (uin,))
    conn.execute(
        "UPDATE persons SET role = 'normal' WHERE person_key = ?", (f"qq:{uin}",)
    )
    return cursor.rowcount > 0


def list_entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM qq_whitelist ORDER BY added_at DESC, qq_uin ASC"
    ).fetchall()
