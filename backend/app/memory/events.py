"""统一事件时间线的写入。

一条事件就是“某个时刻、某人、在某个地方、说/做了一件事”。四个可见性
字段（`scope` / `audience` / `sensitivity` / `source`）在这里校验并落库，
读取侧（`visibility.py`、`retrieval.py`）只认这里的取值，不再自己猜。

为什么强制校验枚举：时间线是所有记忆的源头。一条 `scope` 拼错的事件
不会在写入时报错，而是等到检索时“谁都能看见”或“谁都看不见”，
问题被藏得很深。宁可在这里抛 `ValueError`，让调用方立刻发现。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCOPES = ("personal", "group", "world", "merlin")
SENSITIVITIES = ("normal", "private", "secret")
SOURCES = ("user_message", "assistant_message", "proactive", "system")

MAX_CONTENT = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audience_key(scope: str, audience: list[str]) -> str:
    """把 audience 压成一个可建索引的代表键。

    检索先用它把范围缩小，再逐条做完整可见性判断；它只是索引，
    不是权限本身。
    """
    if scope == "world":
        return "public"
    if scope == "group":
        for key in audience:
            if key.startswith("group:"):
                return key
    return audience[0]


def record_event(
    conn: sqlite3.Connection,
    *,
    person_key: str,
    platform: str,
    channel_id: str,
    scope: str,
    audience: list[str],
    source: str,
    content: str,
    sensitivity: str = "normal",
    meta: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> int:
    """写一条事件，返回自增 id。参数不合法时抛 `ValueError`。"""
    if scope not in SCOPES:
        raise ValueError(f"未知的 scope：{scope}")
    if sensitivity not in SENSITIVITIES:
        raise ValueError(f"未知的 sensitivity：{sensitivity}")
    if source not in SOURCES:
        raise ValueError(f"未知的 source：{source}")
    if not audience or not all(str(key).strip() for key in audience):
        raise ValueError("audience 不能为空")

    cleaned = content.replace("\x00", "").strip()[:MAX_CONTENT]
    if not cleaned:
        raise ValueError("事件内容不能为空")

    stamp = _now()
    cursor = conn.execute(
        """
        INSERT INTO events (
            occurred_at, person_key, platform, channel_id, scope, audience,
            audience_key, sensitivity, source, content, meta, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            occurred_at or stamp,
            person_key,
            platform,
            channel_id,
            scope,
            json.dumps(list(audience), ensure_ascii=False),
            audience_key(scope, list(audience)),
            sensitivity,
            source,
            cleaned,
            json.dumps(meta, ensure_ascii=False) if meta else None,
            stamp,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)
