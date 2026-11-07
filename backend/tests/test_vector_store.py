from __future__ import annotations

import sqlite3

from app import vector_store


def _conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "vec.sqlite3"))
    conn.row_factory = sqlite3.Row
    assert vector_store.load_extension(conn)
    return conn


def test_ensure_is_idempotent_and_counts(tmp_path) -> None:
    table = vector_store.VectorTable("t_vec", "vec_dim:t")
    conn = _conn(tmp_path)
    try:
        assert table.ensure(conn, 3)
        assert table.upsert(conn, [(1, [1.0, 0.0, 0.0])]) == 1
        assert table.ensure(conn, 3)
        assert table.count(conn) == 1
    finally:
        conn.close()


def test_dimension_change_drops_old_rows(tmp_path) -> None:
    table = vector_store.VectorTable("t_vec", "vec_dim:t")
    conn = _conn(tmp_path)
    try:
        assert table.ensure(conn, 3)
        table.upsert(conn, [(1, [1.0, 0.0, 0.0])])
        assert table.ensure(conn, 4)
        assert table.count(conn) == 0
        assert table.upsert(conn, [(1, [1.0, 0.0, 0.0, 0.0])]) == 1
    finally:
        conn.close()


def test_invalid_vectors_are_skipped(tmp_path) -> None:
    table = vector_store.VectorTable("t_vec", "vec_dim:t")
    conn = _conn(tmp_path)
    try:
        table.ensure(conn, 3)
        assert table.upsert(conn, [(1, [1.0, 0.0])]) == 0  # 维度不对
        assert table.upsert(conn, [(2, [1.0, float("nan"), 0.0])]) == 0
        assert table.count(conn) == 0
    finally:
        conn.close()


def test_search_orders_by_distance(tmp_path) -> None:
    table = vector_store.VectorTable("t_vec", "vec_dim:t")
    conn = _conn(tmp_path)
    try:
        table.ensure(conn, 3)
        table.upsert(conn, [(1, [1.0, 0.0, 0.0]), (2, [0.0, 1.0, 0.0])])
        found = table.search(conn, [0.9, 0.1, 0.0], 2)
        assert [rowid for rowid, _ in found] == [1, 2]
    finally:
        conn.close()


def test_upsert_replaces_existing_row(tmp_path) -> None:
    table = vector_store.VectorTable("t_vec", "vec_dim:t")
    conn = _conn(tmp_path)
    try:
        table.ensure(conn, 3)
        assert table.upsert(conn, [(1, [1.0, 0.0, 0.0])]) == 1
        assert table.upsert(conn, [(1, [0.0, 1.0, 0.0])]) == 1
        assert table.count(conn) == 1
        assert table.search(conn, [0.0, 1.0, 0.0], 1)[0][0] == 1
    finally:
        conn.close()


def test_missing_extension_degrades_to_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vector_store, "_extension_loaded", lambda conn: False)
    monkeypatch.setattr(vector_store, "sqlite_vec", None)
    table = vector_store.VectorTable("t_vec", "vec_dim:t")
    conn = sqlite3.connect(str(tmp_path / "plain.sqlite3"))
    try:
        assert table.ensure(conn, 3) is False
        assert table.upsert(conn, [(1, [1.0, 0.0, 0.0])]) == 0
        assert table.search(conn, [1.0, 0.0, 0.0], 5) == []
        assert table.count(conn) == 0
    finally:
        conn.close()
