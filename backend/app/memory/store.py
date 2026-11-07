"""记忆的写入、去重与读取。

去重放在写入侧，而不是每日整理：同一件事在几次对话里反复出现时，
如果每次都新增一条，检索结果会被同一条内容挤满。写入时先做向量近邻，
同一个人、余弦相似度 ≥ 0.92 就合并——文本取更完整的那条，重要性/置信度
取更高，来源事件合并。

没有向量也能工作：嵌入不可用时跳过去重，直接插入。记忆的“有没有”比
“重复不重复”更重要。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.embeddings import get_embedder

from app.memory.events import SCOPES, audience_key
from app.memory.vectors import MEMORIES_VEC

VALID_MEMORY_TYPES = ("fact", "preference", "episode", "promise", "relationship", "reflection")

# 单位向量下：distance² = 2 - 2·cos，所以 cos = 1 - d²/2。
DEDUPE_THRESHOLD = 0.92
MAX_TEXT = 500


@dataclass(frozen=True)
class MemoryDraft:
    """待写入的一条记忆。提炼器和测试都用它描述“想记什么”。"""

    person_key: str
    type: str
    text: str
    scope: str
    audience: list[str]
    sensitivity: str = "normal"
    importance: float = 0.5
    confidence: float = 0.6
    source_event_ids: list[int] = field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def similarity_from_distance(distance: float) -> float:
    """把 sqlite-vec 的距离换算成余弦相似度（向量已归一化）。"""
    return 1.0 - (float(distance) ** 2) / 2.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("audience", "source_event_ids"):
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except (TypeError, ValueError):
            item[key] = []
    return item


def _embed(text: str, embedder: object | None) -> list[float] | None:
    encoder = embedder if embedder is not None else get_embedder()
    if not getattr(encoder, "available", False):
        return None
    try:
        encoded = encoder.encode([text])
    except Exception:  # noqa: BLE001 - 嵌入失败只降级，不阻断
        return None
    return encoded[0] if encoded else None


def _find_duplicate(
    conn: sqlite3.Connection,
    person_key: str,
    vector: list[float],
) -> sqlite3.Row | None:
    for memory_id, distance in MEMORIES_VEC.search(conn, vector, 5):
        if similarity_from_distance(distance) < DEDUPE_THRESHOLD:
            continue
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND person_key = ?",
            (int(memory_id), person_key),
        ).fetchone()
        if row is not None:
            return row
    return None


def add_memory(
    conn: sqlite3.Connection,
    draft: MemoryDraft,
    *,
    embedder: object | None = None,
) -> int:
    """写入一条记忆；与已有记忆近似时合并，返回记忆 id。"""
    if draft.type not in VALID_MEMORY_TYPES:
        raise ValueError(f"未知的记忆类型：{draft.type}")
    if draft.scope not in SCOPES:
        raise ValueError(f"未知的 scope：{draft.scope}")

    text = (draft.text or "").replace("\x00", "").strip()[:MAX_TEXT]
    if not text:
        raise ValueError("记忆内容不能为空")

    vector = _embed(text, embedder)
    duplicate = _find_duplicate(conn, draft.person_key, vector) if vector else None
    stamp = _now()

    if duplicate is not None:
        merged_ids = sorted(
            set(json.loads(duplicate["source_event_ids"] or "[]"))
            | set(draft.source_event_ids or [])
        )
        merged_text = text if len(text) > len(duplicate["text"]) else duplicate["text"]
        conn.execute(
            """
            UPDATE memories
               SET text = ?, importance = ?, confidence = ?,
                   source_event_ids = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                merged_text,
                max(float(duplicate["importance"]), _clamp(draft.importance)),
                max(float(duplicate["confidence"]), _clamp(draft.confidence)),
                json.dumps(merged_ids, ensure_ascii=False),
                stamp,
                int(duplicate["id"]),
            ),
        )
        conn.commit()
        merged_id = int(duplicate["id"])
        if vector:
            MEMORIES_VEC.upsert(conn, [(merged_id, vector)])
        return merged_id

    cursor = conn.execute(
        """
        INSERT INTO memories (
            person_key, type, text, scope, audience, audience_key, sensitivity,
            importance, confidence, valid_from, valid_until, source_event_ids,
            created_at, updated_at, last_accessed_at, access_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
        """,
        (
            draft.person_key,
            draft.type,
            text,
            draft.scope,
            json.dumps(list(draft.audience), ensure_ascii=False),
            audience_key(draft.scope, list(draft.audience)),
            draft.sensitivity,
            _clamp(draft.importance),
            _clamp(draft.confidence),
            draft.valid_from,
            draft.valid_until,
            json.dumps(list(draft.source_event_ids or []), ensure_ascii=False),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    memory_id = int(cursor.lastrowid or 0)
    if vector:
        MEMORIES_VEC.upsert(conn, [(memory_id, vector)])
    return memory_id


def get_memory(conn: sqlite3.Connection, memory_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_memories(conn: sqlite3.Connection, person_key: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM memories WHERE person_key = ? ORDER BY id DESC LIMIT ?",
        (person_key, int(limit)),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def delete_person(conn: sqlite3.Connection, person_key: str) -> int:
    """删除某个人的全部记忆（含向量），返回删除条数。"""
    ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM memories WHERE person_key = ?", (person_key,)
        ).fetchall()
    ]
    if not ids:
        return 0
    MEMORIES_VEC.delete_ids(conn, ids)
    conn.execute("DELETE FROM memories WHERE person_key = ?", (person_key,))
    conn.commit()
    return len(ids)
