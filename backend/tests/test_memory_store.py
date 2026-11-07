from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory import db, store
from app.memory.store import MemoryDraft


class FakeEmbedder:
    available = True
    dim = 4

    def encode(self, texts, *, query=False):
        return [
            [1.0, 0.0, 0.0, 0.0] if "ROS2" in text or "机器人" in text else [0.0, 1.0, 0.0, 0.0]
            for text in texts
        ]


class UnavailableEmbedder:
    available = False
    dim = 4

    def encode(self, texts, *, query=False):
        return []


def _conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    return db.connect()


def _draft(**overrides) -> MemoryDraft:
    values = dict(
        person_key="wp:12",
        type="fact",
        text="阿东在学 ROS2",
        scope="personal",
        audience=["wp:12"],
        importance=0.7,
        confidence=0.8,
        source_event_ids=[1, 2],
    )
    values.update(overrides)
    return MemoryDraft(**values)


def test_add_memory_writes_row_and_vector(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        memory_id = store.add_memory(conn, _draft(), embedder=FakeEmbedder())
        assert store.get_memory(conn, memory_id)["text"] == "阿东在学 ROS2"
        from app.memory.vectors import MEMORIES_VEC

        assert MEMORIES_VEC.count(conn) == 1
    finally:
        conn.close()


def test_near_duplicate_is_merged(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        first = store.add_memory(conn, _draft(), embedder=FakeEmbedder())
        second = store.add_memory(
            conn,
            _draft(text="阿东最近在学 ROS2 机器人", importance=0.9, source_event_ids=[3]),
            embedder=FakeEmbedder(),
        )
        assert second == first
        rows = store.list_memories(conn, "wp:12")
        assert len(rows) == 1
        assert rows[0]["importance"] == pytest.approx(0.9)
        assert set(rows[0]["source_event_ids"]) == {1, 2, 3}
    finally:
        conn.close()


def test_unavailable_embedder_still_stores(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        memory_id = store.add_memory(conn, _draft(), embedder=UnavailableEmbedder())
        assert store.get_memory(conn, memory_id) is not None
        from app.memory.vectors import MEMORIES_VEC

        assert MEMORIES_VEC.count(conn) == 0
    finally:
        conn.close()


def test_invalid_type_is_rejected(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError):
            store.add_memory(conn, _draft(type="gossip"), embedder=FakeEmbedder())
    finally:
        conn.close()


def test_delete_person_removes_rows_and_vectors(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        store.add_memory(conn, _draft(), embedder=FakeEmbedder())
        assert store.delete_person(conn, "wp:12") == 1
        assert store.list_memories(conn, "wp:12") == []
        from app.memory.vectors import MEMORIES_VEC

        assert MEMORIES_VEC.count(conn) == 0
    finally:
        conn.close()
