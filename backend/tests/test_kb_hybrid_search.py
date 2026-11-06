from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, search, vectors


class FakeEmbedder:
    available = True
    dim = 4

    def encode(self, texts, *, query=False):
        # 含“DDS/分布式/中间件”的文本靠拢第一个基向量，其余靠拢第二个。
        return [
            [1.0, 0.0, 0.0, 0.0]
            if any(word in text for word in ("DDS", "分布式", "中间件"))
            else [0.0, 1.0, 0.0, 0.0]
            for text in texts
        ]


class UnavailableEmbedder:
    available = False
    dim = 4

    def encode(self, texts, *, query=False):
        return []


def _settings(tmp_path):
    return SimpleNamespace(
        kb_db_path=str(tmp_path / "kb.sqlite3"),
        kb_top_k=4,
        embedding_dim=4,
        kb_vector_enabled=True,
        kb_hybrid_recall=10,
    )


def _seed(tmp_path, monkeypatch, rows):
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(search, "get_settings", lambda: settings)
    db.init_schema()
    conn = db.connect()
    ids = []
    for post_id, title, body in rows:
        cursor = conn.execute(
            "INSERT INTO chunks (post_id, chunk_index, kind, image_url, url, title, body, tokens) "
            "VALUES (?, 0, 'text', NULL, ?, ?, ?, ?)",
            (post_id, f"https://example.com/{post_id}/", title, body, body),
        )
        ids.append(int(cursor.lastrowid))
    conn.commit()
    conn.close()
    return ids


def test_vector_recall_finds_chunk_without_keyword_overlap(tmp_path, monkeypatch) -> None:
    ids = _seed(
        tmp_path,
        monkeypatch,
        [
            (1, "通信笔记", "DDS 是通信总线。"),
            (2, "天气", "今天天气很好。"),
        ],
    )
    conn = db.connect()
    try:
        vectors.upsert(
            conn,
            [
                (ids[0], [1.0, 0.0, 0.0, 0.0]),
                (ids[1], [0.0, 1.0, 0.0, 0.0]),
            ],
        )
    finally:
        conn.close()
    monkeypatch.setattr(search, "get_embedder", lambda: FakeEmbedder())

    results = search.search_site("分布式中间件", limit=1)

    assert results[0]["title"] == "通信笔记"


def test_chunk_hit_by_both_lists_ranks_first(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(search, "get_settings", lambda: settings)
    db.init_schema()
    conn = db.connect()
    try:
        rows = []
        for post_id, title, body in [
            (1, "纯向量命中", "一段完全不含查询词的内容。"),
            (2, "双路命中", "DDS 分布式通信。"),
        ]:
            cursor = conn.execute(
                "INSERT INTO chunks (post_id, chunk_index, kind, image_url, url, title, body, tokens) "
                "VALUES (?, 0, 'text', NULL, ?, ?, ?, ?)",
                (post_id, f"https://example.com/{post_id}/", title, body, body),
            )
            rows.append(int(cursor.lastrowid))
        conn.commit()
        vectors.upsert(
            conn,
            [
                (rows[0], [1.0, 0.0, 0.0, 0.0]),
                (rows[1], [0.9, 0.1, 0.0, 0.0]),
            ],
        )
    finally:
        conn.close()

    monkeypatch.setattr(search, "get_embedder", lambda: FakeEmbedder())
    results = search.search_site("DDS", limit=2)

    assert results[0]["title"] == "双路命中"


def test_unavailable_embedder_keeps_fts_only_behavior(tmp_path, monkeypatch) -> None:
    _seed(tmp_path, monkeypatch, [(1, "DDS 入门", "DDS 是通信中间件。")])
    monkeypatch.setattr(search, "get_embedder", lambda: UnavailableEmbedder())

    results = search.search_site("DDS", limit=1)

    assert results[0]["title"] == "DDS 入门"
