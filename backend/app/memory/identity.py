"""把不同平台上的同一个人归一到一个 `person_key`。

- 网页：`wp:<user_id>`
- 未绑定 QQ：`qq:<uin>`
- 已绑定 QQ：`wp:<user_id>`（与网页共用一份记忆）

绑定关系是“两个 key 属于同一个人”，不是“把 A 的记忆改到 B 名下”。
`persons` 表用 `wp_user_id` / `qq_uin` 两列做关联，`linked_keys()` 据此
把一个 key 扩成一组可读 key；解绑后两行不再共享这两列，旧记忆自然
不会再被新账号读到。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def person_key_web(user_id: int) -> str:
    return f"wp:{int(user_id)}"


def person_key_qq(qq_uin: int | str, bound_user_id: int | None = None) -> str:
    """已绑定返回 `wp:<id>`，未绑定返回 `qq:<uin>`。"""
    if bound_user_id:
        return person_key_web(bound_user_id)
    return f"qq:{str(qq_uin).strip()}"


def touch_person(
    conn: sqlite3.Connection,
    person_key: str,
    *,
    display_name: str | None = None,
    wp_user_id: int | None = None,
    qq_uin: int | str | None = None,
    now: str | None = None,
) -> None:
    """记录一次出现。

    `first_seen_at` 只在首次插入时写；`last_seen_at` 每次更新。
    昵称/绑定 id 只在本次传了非空值时覆盖，避免一次不带信息的调用把
    已有档案抹掉。
    """
    stamp = now or _now()
    qq = str(qq_uin).strip() if qq_uin is not None else None
    conn.execute(
        """
        INSERT INTO persons (person_key, display_name, wp_user_id, qq_uin,
                             first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_key) DO UPDATE SET
            display_name = COALESCE(excluded.display_name, persons.display_name),
            wp_user_id = COALESCE(excluded.wp_user_id, persons.wp_user_id),
            qq_uin = COALESCE(excluded.qq_uin, persons.qq_uin),
            last_seen_at = excluded.last_seen_at
        """,
        (
            person_key,
            display_name,
            int(wp_user_id) if wp_user_id is not None else None,
            qq,
            stamp,
            stamp,
        ),
    )
    conn.commit()


def link_identity(
    conn: sqlite3.Connection,
    keys: list[str],
    *,
    wp_user_id: int | None = None,
    qq_uin: int | str | None = None,
) -> None:
    """把一组 person_key 标成同一个人。

    不合并历史行、不改写谁名下的记忆，只给每一行补上相同的
    `wp_user_id` / `qq_uin`，之后 `linked_keys()` 就能把它们连起来。

    换绑时旧关联必须失效：`unbind_user` 只清额度库，碰不到这张表，
    如果这里只覆盖本组两行，前一个账号那一行还留着同一个 QQ 号，
    `linked_keys()` 会把新账号和旧账号连在一起。所以先把“声称同一个
    wp/qq、但不属于这组 key”的行清空，再建立新关联。
    """
    qq = str(qq_uin).strip() if qq_uin is not None else None
    if qq is not None or wp_user_id is not None:
        conditions: list[str] = []
        params: list[object] = []
        if qq is not None:
            conditions.append("qq_uin = ?")
            params.append(qq)
        if wp_user_id is not None:
            conditions.append("wp_user_id = ?")
            params.append(int(wp_user_id))
        placeholders = ",".join("?" for _ in keys)
        conn.execute(
            f"""
            UPDATE persons
               SET wp_user_id = NULL, qq_uin = NULL
             WHERE ({" OR ".join(conditions)})
               AND person_key NOT IN ({placeholders})
            """,
            [*params, *keys],
        )
        # 换绑同理：这个 QQ / 账号以前并入过别的身份，那条别名现在指向
        # 别人的数据，必须作废——否则旧账号的 `linked_keys` 里还挂着一个
        # 已经属于新账号的 key。
        stale_aliases = [
            person_key_qq(qq),
            person_key_web(wp_user_id) if wp_user_id is not None else "",
        ]
        try:
            conn.execute(
                "DELETE FROM person_aliases WHERE alias_key IN (?, ?)",
                stale_aliases,
            )
        except sqlite3.OperationalError:  # 旧库还没建表
            pass
        conn.commit()

    for key in keys:
        touch_person(conn, key, wp_user_id=wp_user_id, qq_uin=qq)


def _alias_group(conn: sqlite3.Connection, person_key: str) -> list[str]:
    """合并过的身份：自身、最终身份、以及同组的所有别名。"""
    try:
        row = conn.execute(
            "SELECT canonical_key FROM person_aliases WHERE alias_key = ?",
            (person_key,),
        ).fetchone()
        canonical = str(row["canonical_key"]) if row is not None else person_key
        aliased = conn.execute(
            "SELECT alias_key FROM person_aliases WHERE canonical_key = ?",
            (canonical,),
        ).fetchall()
    except sqlite3.OperationalError:  # 旧库还没建表：按未合并处理
        return [person_key]

    group = [person_key]
    if canonical != person_key:
        group.append(canonical)
    for item in aliased:
        key = str(item["alias_key"])
        if key not in group:
            group.append(key)
    return group


def linked_keys(conn: sqlite3.Connection, person_key: str) -> list[str]:
    """返回与 `person_key` 同属一人的所有 key；自身永远排第一。

    两种关联都算：`persons` 上的 wp/qq 关联（绑定但还没合并），
    以及 `person_aliases` 上的合并记录（历史身份已物理搬走）。
    """
    keys = _alias_group(conn, person_key)

    rows = conn.execute(
        "SELECT person_key, wp_user_id, qq_uin FROM persons"
    ).fetchall()

    seed = next((row for row in rows if row["person_key"] == person_key), None)
    if seed is None:
        return keys

    wp_user_id = seed["wp_user_id"]
    qq_uin = seed["qq_uin"]
    if wp_user_id is None and qq_uin is None:
        return keys

    for row in rows:
        if row["person_key"] in keys:
            continue
        same_wp = wp_user_id is not None and row["wp_user_id"] == wp_user_id
        same_qq = (
            qq_uin is not None
            and row["qq_uin"] is not None
            and str(row["qq_uin"]) == str(qq_uin)
        )
        if same_wp or same_qq:
            keys.append(row["person_key"])
    return keys
