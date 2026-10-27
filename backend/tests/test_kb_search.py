"""知识库检索的兜底行为测试。

重点不是 BM25 排序，而是两个容易回归的入口：
- 问题里没有有效关键词时，应该列出最近文章，而不是整句 LIKE 查空；
- 问“最近有什么新东西”时，应该按时间返回目录，而不是当成普通关键词检索。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, search


def _seed_recent_posts(tmp_path, monkeypatch) -> None:
    """用临时 SQLite 文件造两篇文章，避免碰本地真实知识库。"""

    settings = SimpleNamespace(
        kb_db_path=str(tmp_path / "kb.sqlite3"),
        kb_top_k=4,
    )
    # db.resolve_db_path() 和 search.list_recent() 各自导入了 get_settings，
    # 所以两个模块都要替换，不能只改其中一个。
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(search, "get_settings", lambda: settings)

    db.init_schema()
    conn = db.connect()
    try:
        conn.executemany(
            "INSERT INTO posts (post_id, title, url, modified, synced_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (1, "旧文章", "https://example.com/1/", "2026-10-01", "2026-10-01"),
                (2, "新文章", "https://example.com/2/", "2026-10-02", "2026-10-02"),
            ],
        )
        conn.executemany(
            "INSERT INTO chunks "
            "(post_id, chunk_index, kind, image_url, url, title, body, tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    1,
                    0,
                    "text",
                    None,
                    "https://example.com/1/",
                    "旧文章",
                    "旧文章的开头。",
                    "旧文章 开头",
                ),
                (
                    2,
                    0,
                    "text",
                    None,
                    "https://example.com/2/",
                    "新文章",
                    "新文章的开头，讲的是 DDS。",
                    "新文章 开头 dds",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_broad_question_lists_recent_articles(tmp_path, monkeypatch) -> None:
    _seed_recent_posts(tmp_path, monkeypatch)

    results = search.search_site("这里都有什么", limit=2)

    assert [item["title"] for item in results] == ["新文章", "旧文章"]
    assert "新文章的开头" in results[0]["excerpt"]


def test_recency_question_falls_back_to_recent_articles(tmp_path, monkeypatch) -> None:
    _seed_recent_posts(tmp_path, monkeypatch)

    results = search.search_site("最近有什么新东西", limit=2)

    assert results
    assert results[0]["title"] == "新文章"
