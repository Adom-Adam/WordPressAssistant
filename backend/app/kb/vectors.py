"""知识库的 `sqlite-vec` 向量表（薄封装）。

====================================================================
它和 FTS5 的关系
====================================================================

FTS5 负责“字面命中”，向量负责“意思相近”。两者是并列的两路召回，
最后由 `search.py` 用 RRF 融合，不存在谁替代谁。

表结构：

    chunks_vec(rowid, embedding float[512])

- `rowid` 直接复用 `chunks` 表的 rowid，不再单独维护一列主键；
- 删除/重建某篇文章时，必须先按 `chunks.post_id` 删掉旧向量，再删文本块；
- 维度由 `Settings.embedding_dim` 决定。维度变了就整表重建，
  不能把 512 维的旧向量当 1024 维查询。

通用逻辑在 `app.vector_store.VectorTable`；这里只保留知识库自己的
表名、`post_id` 删除语义和对外函数名，避免调用方跟着改。

所有函数都遵循“向量是增强”的原则：扩展没装、表不存在、维度不对时
返回 0 / 空列表，让调用方继续走 FTS，而不是抛异常。
"""

from __future__ import annotations

import logging
import sqlite3

from app.vector_store import VectorTable, load_extension

logger = logging.getLogger(__name__)

# meta 键沿用旧值 `vec_dim`：已有部署的 chunks_vec 不会被误判成新表重建。
_VECTORS = VectorTable("chunks_vec", "vec_dim")

__all__ = [
    "load_extension",
    "ensure_schema",
    "upsert",
    "search",
    "delete_post",
    "clear",
    "stats",
]


def ensure_schema(conn: sqlite3.Connection, dim: int) -> bool:
    """创建/校验向量表。维度变化时整表重建。"""
    return _VECTORS.ensure(conn, dim)


def upsert(conn: sqlite3.Connection, rows: list[tuple[int, list[float]]]) -> int:
    """写入/覆盖一批 (rowid, vector)，返回真正写入的条数。"""
    return _VECTORS.upsert(conn, rows)


def search(
    conn: sqlite3.Connection,
    embedding: list[float],
    limit: int,
) -> list[tuple[int, float]]:
    """按余弦距离返回最近的 rowid；任何异常都返回空列表。"""
    return _VECTORS.search(conn, embedding, limit)


def delete_post(conn: sqlite3.Connection, post_id: int) -> None:
    """删除一篇文章的所有旧向量。必须在删除 chunks 之前调用。"""
    if not _VECTORS.exists(conn):
        return
    try:
        rows = conn.execute(
            "SELECT rowid FROM chunks WHERE post_id = ?", (int(post_id),)
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("查询文章旧向量失败：post_id=%s error=%s", post_id, exc)
        return
    _VECTORS.delete_ids(conn, [int(row[0]) for row in rows])


def clear(conn: sqlite3.Connection) -> None:
    """清空所有向量，但保留表结构。"""
    _VECTORS.clear(conn)


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """返回 {"vectors": N}；表不存在时 N=0。"""
    return {"vectors": _VECTORS.count(conn)}
