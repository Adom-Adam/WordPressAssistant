"""身份合并与彻底删除。

====================================================================
为什么要物理合并，而不是“读的时候联合”
====================================================================
腾讯 QQ 号可以是临时登录方式：一个人先以 `qq:<uin>` 聊了几天，之后
绑定博客账号变成 `wp:<id>`。如果只在读取时把两个 key 合起来看，
数据会一直有两份——关系状态两份（主动消息按两个身份各发一次）、
待续话题两份、评估预算两份。越晚合并越难解释“为什么她记得两遍”。

所以绑定成功时把源身份的所有行搬到目标身份名下，并记一条别名：
以后旧 key 还能查到“已并入谁”，但不再是独立身份。

====================================================================
合并的三个硬约束
====================================================================
1. **每个 persona 只有一行关系状态**：`relationship_state` 的主键是
   `(persona_key, person_key)`，直接 UPDATE 会撞主键。所以按 persona
   逐行比较，只留较新的一行，并发里较晚的 `last_proactive_at` 保留。
2. **audience 里也要改**：一条事件的 `person_key` 搬走了，但
   `audience` JSON 里还写着旧 key 的话，新的读者看不到、旧的读者还能
   看到——正好反了。
3. **可重复执行**：绑定接口可能重试，合并函数必须幂等；第二次调用
   返回全零，而不是报错。

====================================================================
未来的适配器
====================================================================
新平台（微信、Telegram……）只要也产出 `person_key` 并在下面这张
`_PERSON_TABLES` 里登记，就自动获得合并与删除能力。不要在各自的
适配器里另写一份“搬数据”的代码。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.memory import events as event_store
from app.memory import identity
from app.memory.vectors import MEMORIES_VEC

# 所有带 `person_key` 列、需要跟着身份走的表。
_PERSON_TABLES = (
    "events",
    "memories",
    "relationship_events",
    "decision_log",
    "appraisal_log",
    "pending_topics",
)

# `proactive_log` 用的列名是 `target_key`（它的目标可能是 `group:<id>`）。
_TARGET_KEY_TABLE = "proactive_log"

_ROLE_RANK = {"normal": 0, "special": 1, "exception": 2, "owner": 3}

_ALIAS_HOPS = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_key(conn: sqlite3.Connection, person_key: str) -> str:
    """沿别名链找到最终身份；没有别名时原样返回。"""
    key = str(person_key or "").strip()
    seen: set[str] = set()
    for _ in range(_ALIAS_HOPS):
        if key in seen:
            break
        seen.add(key)
        row = conn.execute(
            "SELECT canonical_key FROM person_aliases WHERE alias_key = ?", (key,)
        ).fetchone()
        if row is None:
            break
        key = str(row["canonical_key"])
    return key


def _placeholders(items: list[str]) -> str:
    return ",".join("?" for _ in items)


def _rewrite_audience(
    raw: str, sources: list[str], target: str
) -> list[str]:
    """把 audience JSON 里的旧 key 换成新 key，保持顺序、去重。"""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    result: list[str] = []
    for item in parsed:
        key = str(item)
        key = target if key in sources else key
        if key not in result:
            result.append(key)
    if target not in result:
        result.append(target)
    return result


def _later(first: str | None, second: str | None) -> str | None:
    """两个 ISO 时间取较晚的一个；空值不算“晚”。"""
    if not first:
        return second
    if not second:
        return first
    return first if str(first) >= str(second) else second


def _state_columns(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("PRAGMA table_info(relationship_state)")]


def _merge_state_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    target: str,
    columns: list[str],
) -> None:
    persona_key = row["persona_key"]
    current = conn.execute(
        """SELECT * FROM relationship_state
            WHERE persona_key = ? AND person_key = ?""",
        (persona_key, target),
    ).fetchone()

    if current is None:
        conn.execute(
            """UPDATE relationship_state SET person_key = ?
                WHERE persona_key = ? AND person_key = ?""",
            (target, persona_key, row["person_key"]),
        )
        return

    later_proactive = _later(row["last_proactive_at"], current["last_proactive_at"])
    source_is_newer = str(row["updated_at"]) > str(current["updated_at"])

    if source_is_newer:
        values = {
            name: row[name]
            for name in columns
            if name not in ("persona_key", "person_key")
        }
        values["last_proactive_at"] = later_proactive
        assignments = ", ".join(f"{name} = ?" for name in values)
        conn.execute(
            f"""UPDATE relationship_state SET {assignments}
                 WHERE persona_key = ? AND person_key = ?""",
            [*values.values(), persona_key, target],
        )
    elif later_proactive != current["last_proactive_at"]:
        # 目标行更新：数值照旧，但“最近主动过”的时间要取较晚的那个，
        # 否则合并后可能立刻触发一条重复的主动消息。
        conn.execute(
            """UPDATE relationship_state SET last_proactive_at = ?
                WHERE persona_key = ? AND person_key = ?""",
            (later_proactive, persona_key, target),
        )

    conn.execute(
        """DELETE FROM relationship_state
            WHERE persona_key = ? AND person_key = ?""",
        (persona_key, row["person_key"]),
    )


def _merge_persons_row(
    conn: sqlite3.Connection, source: str, target: str
) -> bool:
    """把 `persons` 的档案并进目标；返回是否真的动过。"""
    src = conn.execute(
        "SELECT * FROM persons WHERE person_key = ?", (source,)
    ).fetchone()
    if src is None:
        return False

    dst = conn.execute(
        "SELECT * FROM persons WHERE person_key = ?", (target,)
    ).fetchone()
    if dst is None:
        conn.execute(
            "UPDATE persons SET person_key = ? WHERE person_key = ?", (target, source)
        )
        return True

    src_role = str(src["role"] or "normal")
    dst_role = str(dst["role"] or "normal")
    role = (
        src_role
        if _ROLE_RANK.get(src_role, 0) > _ROLE_RANK.get(dst_role, 0)
        else dst_role
    )
    conn.execute(
        """UPDATE persons
              SET display_name = COALESCE(display_name, ?),
                  wp_user_id = COALESCE(wp_user_id, ?),
                  qq_uin = COALESCE(qq_uin, ?),
                  first_seen_at = MIN(first_seen_at, ?),
                  last_seen_at = MAX(last_seen_at, ?),
                  note = COALESCE(note, ?),
                  role = ?
            WHERE person_key = ?""",
        (
            src["display_name"],
            src["wp_user_id"],
            src["qq_uin"],
            src["first_seen_at"],
            src["last_seen_at"],
            src["note"],
            role,
            target,
        ),
    )
    conn.execute("DELETE FROM persons WHERE person_key = ?", (source,))
    return True


def _merge_appraisal_usage(
    conn: sqlite3.Connection, source: str, target: str
) -> None:
    """评估预算按人记账：把源身份当天用掉的量累加到目标身份。"""
    rows = conn.execute(
        "SELECT day, used FROM appraisal_usage WHERE scope = ?",
        (f"person:{source}",),
    ).fetchall()
    for row in rows:
        conn.execute(
            """INSERT INTO appraisal_usage (scope, day, used) VALUES (?, ?, ?)
               ON CONFLICT(scope, day) DO UPDATE SET used = used + excluded.used""",
            (f"person:{target}", row["day"], int(row["used"])),
        )
        conn.execute(
            "DELETE FROM appraisal_usage WHERE scope = ? AND day = ?",
            (f"person:{source}", row["day"]),
        )


def merge_person(
    conn: sqlite3.Connection,
    *,
    source_keys: list[str],
    target_key: str,
    now: str | None = None,
) -> dict[str, int]:
    """把 `source_keys` 的全部数据搬到 `target_key` 名下。

    返回每张表被搬动的行数；已经合并过时全是 0。
    调用方负责提交事务（绑定流程里和 `link_identity` 一起提交）。
    """
    stamp = now or _now()
    target = canonical_key(conn, target_key)

    sources: list[str] = []
    for raw in source_keys:
        raw_key = str(raw or "").strip()
        if not raw_key:
            continue
        key = canonical_key(conn, raw_key)
        # 已经合并过：数据已经在 target 名下，无事可做。
        if key == target:
            continue
        # 已经属于**另一个**身份：那是别人的数据。典型场景是同一个 QQ
        # 换绑到新账号——旧账号的记忆留在旧账号名下，不能被新账号
        # 顺着别名拖走。
        if key != raw_key:
            continue
        if key not in sources:
            sources.append(key)

    moved = {name: 0 for name in _PERSON_TABLES}
    moved.update({_TARGET_KEY_TABLE: 0, "appraisal_usage": 0, "persons": 0})
    if not sources:
        return moved

    for source in sources:
        conn.execute(
            """INSERT INTO person_aliases (alias_key, canonical_key, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(alias_key) DO UPDATE SET canonical_key = excluded.canonical_key""",
            (source, target, stamp),
        )
        # 链式合并：以前指向 source 的别名，现在指向最终目标。
        conn.execute(
            "UPDATE person_aliases SET canonical_key = ? WHERE canonical_key = ?",
            (target, source),
        )

    for table in ("events", "memories"):
        rows = conn.execute(
            f"""SELECT id, scope, audience FROM {table}
                 WHERE person_key IN ({_placeholders(sources)})""",
            sources,
        ).fetchall()
        for row in rows:
            audience = _rewrite_audience(row["audience"], sources, target)
            conn.execute(
                f"""UPDATE {table}
                       SET person_key = ?, audience = ?, audience_key = ?
                     WHERE id = ?""",
                (
                    target,
                    json.dumps(audience, ensure_ascii=False),
                    event_store.audience_key(row["scope"], audience),
                    row["id"],
                ),
            )
            moved[table] += 1

    for table in (
        "relationship_events",
        "decision_log",
        "appraisal_log",
        "pending_topics",
    ):
        cursor = conn.execute(
            f"""UPDATE {table} SET person_key = ?
                 WHERE person_key IN ({_placeholders(sources)})""",
            [target, *sources],
        )
        moved[table] = max(cursor.rowcount, 0)

    cursor = conn.execute(
        f"""UPDATE {_TARGET_KEY_TABLE} SET target_key = ?
             WHERE target_key IN ({_placeholders(sources)})""",
        [target, *sources],
    )
    moved[_TARGET_KEY_TABLE] = max(cursor.rowcount, 0)

    state_columns = _state_columns(conn)
    for source in sources:
        rows = conn.execute(
            "SELECT * FROM relationship_state WHERE person_key = ?", (source,)
        ).fetchall()
        for row in rows:
            _merge_state_row(conn, row, target, state_columns)
            moved["relationship_state"] = moved.get("relationship_state", 0) + 1
        _merge_appraisal_usage(conn, source, target)

    for source in sources:
        if _merge_persons_row(conn, source, target):
            moved["persons"] += 1

    return moved


def delete_person_data(
    conn: sqlite3.Connection, person_key: str
) -> dict[str, int]:
    """彻底删除一个身份（含所有别名）的全部数据。

    注销账号和后台“彻底删除 QQ 数据”共用这一条路径：漏一张表就会留下
    孤儿行，之后按 key 查得到、界面上又删不掉。

    与聊天链路相反，这里**不吞异常**：调用方要知道删除是否成功。
    """
    key = canonical_key(conn, person_key)
    keys = list(dict.fromkeys([key, *identity.linked_keys(conn, key)]))
    placeholders = _placeholders(keys)

    deleted: dict[str, int] = {}

    ids = [
        int(row[0])
        for row in conn.execute(
            f"SELECT id FROM memories WHERE person_key IN ({placeholders})", keys
        ).fetchall()
    ]
    if ids:
        MEMORIES_VEC.delete_ids(conn, ids)
    deleted["memories"] = len(ids)

    # 关系状态不在 `_PERSON_TABLES` 里（它要按 persona 逐行合并），
    # 但删除时同样要清：多一行就是一条会继续触发的主动消息。
    cursor = conn.execute(
        f"DELETE FROM relationship_state WHERE person_key IN ({placeholders})", keys
    )
    deleted["relationship_state"] = max(cursor.rowcount, 0)

    for table in _PERSON_TABLES:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE person_key IN ({placeholders})", keys
        )
        deleted[table] = max(cursor.rowcount, 0)

    cursor = conn.execute(
        f"DELETE FROM {_TARGET_KEY_TABLE} WHERE target_key IN ({placeholders})",
        keys,
    )
    deleted[_TARGET_KEY_TABLE] = max(cursor.rowcount, 0)

    cursor = conn.execute(
        f"DELETE FROM appraisal_usage WHERE scope IN ({placeholders})",
        [f"person:{item}" for item in keys],
    )
    deleted["appraisal_usage"] = max(cursor.rowcount, 0)

    # 别名两边都清：既要删掉“别人并进了这组”的记录，也要删掉
    # “这组并进了别人”的记录（否则目标身份仍会顺着别名找回已删数据）。
    cursor = conn.execute(
        f"""DELETE FROM person_aliases
             WHERE alias_key IN ({placeholders})
                OR canonical_key IN ({placeholders})""",
        [*keys, *keys],
    )
    deleted["person_aliases"] = max(cursor.rowcount, 0)

    cursor = conn.execute(
        f"DELETE FROM persons WHERE person_key IN ({placeholders})", keys
    )
    deleted["persons"] = max(cursor.rowcount, 0)

    return deleted
