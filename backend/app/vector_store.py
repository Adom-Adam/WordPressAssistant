"""通用的 `sqlite-vec` 向量表封装。

`sqlite-vec` 本身只提供“虚拟表 + 距离查询”这一层原语，每个使用点都要自己
处理扩展加载、维度校验、维度变更重建、非法向量过滤。这些逻辑在知识库和
记忆库里一模一样，所以集中在这里；`app/kb/vectors.py` 和
`app/memory/vectors.py` 都只是薄封装。

约定：

- 表名、列名只来自代码常量，所以用 f-string 拼 SQL，不把用户输入拼进去；
- 维度记在 `meta` 表的 `dim_key` 下；不同向量表用不同的 key，
  避免知识库换模型时把记忆库的表一起删掉；
- 扩展没装、表不存在、维度不对时返回 0 / 空列表，绝不抛异常——
  向量是增强，调用方永远可以退回关键词/重要性排序。
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

_warned = False


def _warn_once(message: str, *args: object) -> None:
    """进程内只警告一次，避免每个数据库连接都刷同一条日志。"""
    global _warned
    if _warned:
        return
    _warned = True
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


def _valid_vector(vector: list[float], dim: int | None) -> bool:
    if dim is None or len(vector) != dim:
        return False
    return all(
        isinstance(value, (int, float)) and math.isfinite(value) for value in vector
    )


class VectorTable:
    """一张 `vec0` 虚拟表的最小操作集。

    `dim_key` 是记录维度的 `meta.key`，不同表必须传不同的值。
    """

    def __init__(self, table: str, dim_key: str) -> None:
        self.table = table
        self.dim_key = dim_key

    def exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (self.table,),
        ).fetchone()
        return row is not None

    def current_dim(self, conn: sqlite3.Connection) -> int | None:
        """读取建表时记录的维度；没有记录返回 None。"""
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (self.dim_key,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def ensure(self, conn: sqlite3.Connection, dim: int) -> bool:
        """创建/校验向量表。维度变化时整表重建。"""
        if dim <= 0:
            return False
        if not _extension_loaded(conn) and not load_extension(conn):
            return False

        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )

        current = self.current_dim(conn)
        if current is not None and current != dim:
            # 旧向量与新维度不兼容；直接丢弃，等下次同步/补向量重建。
            logger.warning("向量维度从 %s 变为 %s，重建 %s", current, dim, self.table)
            conn.execute(f"DROP TABLE IF EXISTS {self.table}")

        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.table} "
            f"USING vec0(embedding float[{dim}])"
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (self.dim_key, str(dim)),
        )
        conn.commit()
        return True

    def upsert(self, conn: sqlite3.Connection, rows: list[tuple[int, list[float]]]) -> int:
        """写入/覆盖一批 (rowid, vector)，返回真正写入的条数。"""
        if not rows or not self.exists(conn):
            return 0

        dim = self.current_dim(conn)
        written = 0
        for rowid, vector in rows:
            if not _valid_vector(vector, dim):
                logger.warning(
                    "跳过维度不对或含非有限值的向量：%s rowid=%s", self.table, rowid
                )
                continue
            try:
                # vec0 虚拟表不支持 REPLACE 语义：已存在的 rowid 会报
                # UNIQUE 而不是覆盖。先删再插，才能支持“合并后更新向量”。
                try:
                    conn.execute(
                        f"INSERT OR REPLACE INTO {self.table} (rowid, embedding) "
                        "VALUES (?, ?)",
                        (int(rowid), _serialize(vector)),
                    )
                except sqlite3.Error:
                    conn.execute(
                        f"DELETE FROM {self.table} WHERE rowid = ?", (int(rowid),)
                    )
                    conn.execute(
                        f"INSERT INTO {self.table} (rowid, embedding) VALUES (?, ?)",
                        (int(rowid), _serialize(vector)),
                    )
                written += 1
            except sqlite3.Error as exc:
                logger.warning(
                    "写入向量失败：%s rowid=%s error=%s", self.table, rowid, exc
                )
        conn.commit()
        return written

    def search(
        self,
        conn: sqlite3.Connection,
        embedding: list[float],
        limit: int,
    ) -> list[tuple[int, float]]:
        """按余弦距离返回最近的 rowid；任何异常都返回空列表。"""
        if limit <= 0 or not self.exists(conn):
            return []

        dim = self.current_dim(conn)
        if not _valid_vector(embedding, dim):
            logger.warning("查询向量维度不对或含非有限值，跳过向量检索")
            return []

        try:
            rows = conn.execute(
                f"SELECT rowid, distance FROM {self.table} "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (_serialize(embedding), int(limit)),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("向量检索失败：%s error=%s", self.table, exc)
            return []

        return [(int(row[0]), float(row[1])) for row in rows]

    def delete_ids(self, conn: sqlite3.Connection, ids: list[int]) -> int:
        """按 rowid 删除一批向量，返回删除的行数。"""
        if not ids or not self.exists(conn):
            return 0
        placeholders = ",".join("?" for _ in ids)
        try:
            cursor = conn.execute(
                f"DELETE FROM {self.table} WHERE rowid IN ({placeholders})",
                [int(item) for item in ids],
            )
        except sqlite3.Error as exc:
            logger.warning("删除向量失败：%s error=%s", self.table, exc)
            return 0
        return max(cursor.rowcount, 0)

    def clear(self, conn: sqlite3.Connection) -> None:
        """清空所有向量，但保留表结构。"""
        if not self.exists(conn):
            return
        try:
            conn.execute(f"DELETE FROM {self.table}")
        except sqlite3.Error as exc:
            logger.warning("清空向量表失败：%s error=%s", self.table, exc)

    def count(self, conn: sqlite3.Connection) -> int:
        """返回向量条数；表不存在时返回 0。"""
        if not self.exists(conn):
            return 0
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0])
        except sqlite3.Error:
            return 0
