"""`sqlite-vec` 向量表。

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

所有函数都遵循“向量是增强”的原则：扩展没装、表不存在、维度不对时
返回 0 / 空列表，让调用方继续走 FTS，而不是抛异常。
"""

from __future__ import annotations

import logging
import math
import sqlite3

try:  # 可选依赖：没装时整个模块仍然可以 import
    import sqlite_vec
except ImportError:  # pragma: no cover - 安装依赖后不会走到
    sqlite_vec = None

logger = logging.getLogger(__name__)

_TABLE = "chunks_vec"
_warned_unavailable = False


def _warn_once(message: str, *args: object) -> None:
    """模块级只警告一次，避免每个数据库连接都刷同一条日志。"""
    global _warned_unavailable
    if _warned_unavailable:
        return
    _warned_unavailable = True
    logger.warning(message, *args)


def _extension_loaded(conn: sqlite3.Connection) -> bool:
    """当前连接是否已经能调用 sqlite-vec 的函数。"""
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except sqlite3.Error:
        return False


def _serialize(vector: list[float]) -> bytes:
    """把 Python 列表变成 sqlite-vec 需要的 float32 二进制。"""
    if sqlite_vec is None:
        raise RuntimeError("sqlite-vec 未安装")
    return sqlite_vec.serialize_float32(vector)


def load_extension(conn: sqlite3.Connection) -> bool:
    """在当前连接上加载 sqlite-vec；失败返回 False，不抛异常。"""
    if _extension_loaded(conn):
        return True
    if sqlite_vec is None:
        _warn_once("sqlite-vec 未安装，向量检索不可用")
        return False

    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except (AttributeError, OSError, sqlite3.Error) as exc:
        _warn_once("sqlite-vec 加载失败，检索将只使用 FTS：%s", exc)
        return False


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_TABLE,),
    ).fetchone()
    return row is not None


def _current_dim(conn: sqlite3.Connection) -> int | None:
    """读取建表时记录的维度；没有记录返回 None。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'vec_dim'").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def ensure_schema(conn: sqlite3.Connection, dim: int) -> bool:
    """创建/校验向量表。维度变化时整表重建。"""
    if dim <= 0:
        return False
    if not _extension_loaded(conn) and not load_extension(conn):
        return False

    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )

    current = _current_dim(conn)
    if current is not None and current != dim:
        # 旧向量与新维度不兼容；直接丢弃，等下次同步/补向量重建。
        logger.warning("向量维度从 %s 变为 %s，重建向量表", current, dim)
        conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")

    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} USING vec0(embedding float[{dim}])"
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('vec_dim', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(dim),),
    )
    conn.commit()
    return True


def _valid_vector(vector: list[float], dim: int | None) -> bool:
    if dim is None or len(vector) != dim:
        return False
    return all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)


def upsert(conn: sqlite3.Connection, rows: list[tuple[int, list[float]]]) -> int:
    """写入/覆盖一批 (rowid, vector)，返回真正写入的条数。"""
    if not rows or not _table_exists(conn):
        return 0

    dim = _current_dim(conn)
    written = 0
    for rowid, vector in rows:
        if not _valid_vector(vector, dim):
            logger.warning("跳过维度不对或含非有限值的向量：rowid=%s", rowid)
            continue
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {_TABLE} (rowid, embedding) VALUES (?, ?)",
                (int(rowid), _serialize(vector)),
            )
            written += 1
        except sqlite3.Error as exc:
            logger.warning("写入向量失败：rowid=%s error=%s", rowid, exc)
    conn.commit()
    return written


def search(
    conn: sqlite3.Connection,
    embedding: list[float],
    limit: int,
) -> list[tuple[int, float]]:
    """按余弦距离返回最近的 rowid；任何异常都返回空列表。"""
    if limit <= 0 or not _table_exists(conn):
        return []

    dim = _current_dim(conn)
    if not _valid_vector(embedding, dim):
        logger.warning("查询向量维度不对或含非有限值，跳过向量检索")
        return []

    try:
        rows = conn.execute(
            f"SELECT rowid, distance FROM {_TABLE} "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_serialize(embedding), int(limit)),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("向量检索失败，降级为 FTS：%s", exc)
        return []

    return [(int(row[0]), float(row[1])) for row in rows]


def delete_post(conn: sqlite3.Connection, post_id: int) -> None:
    """删除一篇文章的所有旧向量。必须在删除 chunks 之前调用。"""
    if not _table_exists(conn):
        return
    try:
        conn.execute(
            f"DELETE FROM {_TABLE} WHERE rowid IN "
            "(SELECT rowid FROM chunks WHERE post_id = ?)",
            (int(post_id),),
        )
    except sqlite3.Error as exc:
        logger.warning("删除文章旧向量失败：post_id=%s error=%s", post_id, exc)


def clear(conn: sqlite3.Connection) -> None:
    """清空所有向量，但保留表结构。"""
    if not _table_exists(conn):
        return
    try:
        conn.execute(f"DELETE FROM {_TABLE}")
    except sqlite3.Error as exc:
        logger.warning("清空向量表失败：%s", exc)


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """返回 {"vectors": N}；表不存在时 N=0。"""
    if not _table_exists(conn):
        return {"vectors": 0}
    try:
        count = int(conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0])
    except sqlite3.Error:
        count = 0
    return {"vectors": count}
