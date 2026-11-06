from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, vectors


def _settings(tmp_path, dim=4):
    return SimpleNamespace(kb_db_path=str(tmp_path / "kb.sqlite3"), embedding_dim=dim)


def _insert_chunks(conn, post_id: int, bodies: list[str]) -> list[int]:
    rowids = []
    for index, body in enumerate(bodies):
        cursor = conn.execute(
            "INSERT INTO chunks (post_id, chunk_index, kind, image_url, url, title, body, tokens) "
            "VALUES (?, ?, 'text', NULL, ?, ?, ?, ?)",
            (post_id, index, f"https://example.com/{post_id}/", f"文章{post_id}", body, body),
        )
        rowids.append(int(cursor.lastrowid))
    conn.commit()
    return rowids


def test_vectors_are_stored_and_ranked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path))
    db.init_schema()
    conn = db.connect()
    try:
        rowids = _insert_chunks(conn, 1, ["第一块", "第二块", "第三块"])
        rows = [
            (rowids[0], [1.0, 0.0, 0.0, 0.0]),
            (rowids[1], [0.0, 1.0, 0.0, 0.0]),
            (rowids[2], [0.0, 0.0, 1.0, 0.0]),
        ]
        assert vectors.upsert(conn, rows) == 3
        found = vectors.search(conn, [0.1, 0.9, 0.0, 0.0], limit=2)
        assert [rowid for rowid, _distance in found] == [rowids[1], rowids[0]]
    finally:
        conn.close()


def test_delete_post_removes_its_vectors(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path))
    db.init_schema()
    conn = db.connect()
    try:
        rowids = _insert_chunks(conn, 1, ["第一块", "第二块"])
        vectors.upsert(conn, [(rowid, [1.0, 0.0, 0.0, 0.0]) for rowid in rowids])
        vectors.delete_post(conn, 1)
        assert vectors.stats(conn)["vectors"] == 0
    finally:
        conn.close()


def test_clear_index_removes_chunks_and_vectors(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path))
    db.init_schema()
    conn = db.connect()
    try:
        rowids = _insert_chunks(conn, 1, ["第一块"])
        vectors.upsert(conn, [(rowids[0], [1.0, 0.0, 0.0, 0.0])])
        db.clear_index(conn)
        assert db.stats(conn)["chunks"] == 0
        assert vectors.stats(conn)["vectors"] == 0
    finally:
        conn.close()


def test_dimension_change_rebuilds_vector_table(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path, dim=4))
    db.init_schema()
    conn = db.connect()
    rowids = _insert_chunks(conn, 1, ["第一块"])
    vectors.upsert(conn, [(rowids[0], [1.0, 0.0, 0.0, 0.0])])
    conn.close()

    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path, dim=8))
    db.init_schema()
    conn = db.connect()
    try:
        assert vectors.stats(conn)["vectors"] == 0
        vectors.upsert(conn, [(rowids[0], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])])
        assert vectors.search(conn, [1.0] + [0.0] * 7, limit=1)[0][0] == rowids[0]
    finally:
        conn.close()
