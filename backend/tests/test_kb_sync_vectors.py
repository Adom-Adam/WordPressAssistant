from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, sync, vectors


class FakeEmbedder:
    available = True
    dim = 4

    def encode(self, texts, *, query=False):
        return [[1.0, 0.0, 0.0, 0.0] if "DDS" in text else [0.0, 1.0, 0.0, 0.0] for text in texts]


class UnavailableEmbedder:
    available = False
    dim = 4

    def encode(self, texts, *, query=False):
        return []


def _settings(tmp_path):
    return SimpleNamespace(
        wp_site_url="https://example.com",
        kb_db_path=str(tmp_path / "kb.sqlite3"),
        kb_chunk_size=600,
        kb_chunk_overlap=80,
        embedding_dim=4,
        kb_vector_enabled=True,
    )


def _post():
    return {
        "id": 1,
        "link": "https://example.com/1/",
        "title": {"rendered": "DDS 入门"},
        "content": {"rendered": "<p>DDS 是一种分布式通信中间件。</p>"},
        "modified_gmt": "2026-11-01T00:00:00",
    }


def _patch(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(sync, "fetch_posts", lambda *args, **kwargs: [_post()])


def test_sync_writes_one_vector_per_chunk(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    stats = sync.sync(with_vision=False, embedder=FakeEmbedder())
    assert stats["chunks"] > 0
    assert stats["vectors"] == stats["chunks"]


def test_sync_without_vectors_keeps_fts_only(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    stats = sync.sync(with_vision=False, embedder=FakeEmbedder(), with_vectors=False)
    assert stats["chunks"] > 0
    assert stats["vectors"] == 0


def test_unavailable_embedder_does_not_break_sync(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    stats = sync.sync(with_vision=False, embedder=UnavailableEmbedder())
    assert stats["chunks"] > 0
    assert stats["vectors"] == 0


def test_resync_does_not_leave_stale_vectors(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    sync.sync(with_vision=False, embedder=FakeEmbedder())
    sync.sync(with_vision=False, embedder=FakeEmbedder())
    conn = db.connect()
    try:
        assert db.stats(conn)["chunks"] == vectors.stats(conn)["vectors"]
    finally:
        conn.close()


def test_build_vectors_backfills_all_chunks(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    sync.sync(with_vision=False, embedder=FakeEmbedder(), with_vectors=False)
    assert sync.build_vectors(embedder=FakeEmbedder()) > 0
    conn = db.connect()
    try:
        assert db.stats(conn)["vectors"] == db.stats(conn)["chunks"]
    finally:
        conn.close()
