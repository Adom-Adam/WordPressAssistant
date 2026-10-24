"""SQLite 连接与表结构。

为什么选 SQLite：
- 整个数据库就是一个文件，部署时不用额外安装数据库服务；
- Python 标准库自带 sqlite3，零依赖；
- 自带 FTS5 全文检索，几十到几万篇文章都够用；
- 备份就是复制 `data/kb.sqlite3` 一个文件。

表结构分三块：
- `posts`：文章级元数据（用于统计、判断哪些文章被同步过）；
- `chunks`：真正被检索的文本块/图片描述块，FTS5 虚拟表；
- `images`：图片描述缓存，图片没变就不重复调用视觉模型。

FTS5 的 `UNINDEXED` 列会保存但不参与分词：
post_id、chunk_index、kind、image_url、url 都属于这类“附带元数据”，
真正参与全文匹配的是 title、body、tokens 三列。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import get_settings

# db.py 在 backend/app/kb/ 下，parents[2] 就是 backend/。
# 配置里的相对路径统一以 backend/ 为基准，避免受启动目录影响。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id    INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    modified   TEXT,
    synced_at  TEXT NOT NULL
);

-- FTS5 虚拟表：SQLite 会为它自动维护倒排索引。
-- 列顺序很重要，bm25() 的权重参数按这个顺序传。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    post_id     UNINDEXED,
    chunk_index UNINDEXED,
    kind        UNINDEXED,   -- text / image
    image_url   UNINDEXED,
    url         UNINDEXED,
    title,
    body,
    tokens,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 图片描述缓存。image_key 是图片 URL 的 SHA-256，
-- 这样同一张图在多个文章里出现时只调用一次视觉模型。
CREATE TABLE IF NOT EXISTS images (
    image_key   TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    description TEXT,
    ocr_text    TEXT,
    keywords    TEXT,        -- JSON 数组字符串
    model       TEXT,
    updated_at  TEXT NOT NULL,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def resolve_db_path() -> Path:
    """返回数据库的绝对路径，并确保父目录存在。"""

    settings = get_settings()
    path = Path(settings.kb_db_path).expanduser()
    if not path.is_absolute():
        path = _BACKEND_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    """打开一个数据库连接。

    每次调用都新建连接，而不是全局共享一个：
    - SQLite 连接默认不能跨线程使用；
    - 同步脚本、FastAPI 请求、测试可能在不同线程里跑；
    - 打开连接的成本很低，换来的是不会踩线程安全的坑。

    WAL 模式允许“一边写索引，一边读索引”，
    同步文章时前端仍然可以正常提问。
    """

    conn = sqlite3.connect(str(resolve_db_path()), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """创建所有表。重复调用是安全的（IF NOT EXISTS）。"""

    owns_connection = conn is None
    conn = conn or connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def image_key(url: str) -> str:
    """把图片 URL 变成固定长度的缓存键。"""

    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def get_image_cache(key: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """读取图片描述缓存；没有缓存或上次失败则返回 None。"""

    owns_connection = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT url, description, ocr_text, keywords, model FROM images "
            "WHERE image_key = ? AND error IS NULL AND description IS NOT NULL",
            (key,),
        ).fetchone()
        if row is None:
            return None

        keywords: list[str] = []
        try:
            keywords = json.loads(row["keywords"] or "[]")
        except json.JSONDecodeError:
            pass

        return {
            "url": row["url"],
            "description": row["description"],
            "ocr_text": row["ocr_text"] or "",
            "keywords": keywords,
            "model": row["model"] or "",
        }
    finally:
        if owns_connection:
            conn.close()


def save_image_cache(
    key: str,
    url: str,
    description: str = "",
    ocr_text: str = "",
    keywords: list[str] | None = None,
    model: str = "",
    error: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """写入/更新图片描述缓存。

    用 SQLite 的 UPSERT（ON CONFLICT DO UPDATE）语法：
    如果 image_key 已存在就更新，不存在就插入。
    """

    owns_connection = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """
            INSERT INTO images
                (image_key, url, description, ocr_text, keywords, model, updated_at, error)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
            ON CONFLICT(image_key) DO UPDATE SET
                url         = excluded.url,
                description = excluded.description,
                ocr_text    = excluded.ocr_text,
                keywords    = excluded.keywords,
                model       = excluded.model,
                updated_at  = excluded.updated_at,
                error       = excluded.error
            """,
            (
                key,
                url,
                description,
                ocr_text,
                json.dumps(keywords or [], ensure_ascii=False),
                model,
                error,
            ),
        )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def clear_index(conn: sqlite3.Connection) -> None:
    """清空文章与索引，但保留图片描述缓存。

    图片缓存可能花过钱生成，重建索引时没必要重新识别；
    只有图片 URL 或内容变化时才需要重新调用视觉模型。
    """

    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM posts")
    conn.commit()


def stats(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """返回知识库的规模统计，用于同步结束后的提示和排查。"""

    owns_connection = conn is None
    conn = conn or connect()
    try:
        result: dict[str, int] = {}
        for key, sql in {
            "posts": "SELECT COUNT(*) FROM posts",
            "chunks": "SELECT COUNT(*) FROM chunks",
            "text_chunks": "SELECT COUNT(*) FROM chunks WHERE kind = 'text'",
            "image_chunks": "SELECT COUNT(*) FROM chunks WHERE kind = 'image'",
            "images": "SELECT COUNT(*) FROM images WHERE error IS NULL",
        }.items():
            result[key] = int(conn.execute(sql).fetchone()[0])
        return result
    finally:
        if owns_connection:
            conn.close()
