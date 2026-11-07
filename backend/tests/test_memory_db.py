from __future__ import annotations

from types import SimpleNamespace

from app.memory import db, identity


def _settings(tmp_path):
    return SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )


def _patch(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)


def test_init_schema_creates_tables(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"events", "memories", "persons", "memories_vec"} <= names
        assert db.stats(conn) == {"events": 0, "memories": 0, "persons": 0}
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    db.init_schema()
    assert db.stats()["events"] == 0


def test_person_key_rules() -> None:
    assert identity.person_key_web(12) == "wp:12"
    assert identity.person_key_qq(123456) == "qq:123456"
    assert identity.person_key_qq(123456, bound_user_id=12) == "wp:12"


def test_link_identity_makes_history_shared(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        identity.touch_person(conn, "qq:123456", qq_uin="123456")
        identity.touch_person(conn, "wp:12", wp_user_id=12)
        assert identity.linked_keys(conn, "wp:12") == ["wp:12"]

        identity.link_identity(conn, ["wp:12", "qq:123456"], wp_user_id=12, qq_uin="123456")
        assert set(identity.linked_keys(conn, "wp:12")) == {"wp:12", "qq:123456"}
        assert set(identity.linked_keys(conn, "qq:123456")) == {"wp:12", "qq:123456"}
        conn.commit()
    finally:
        conn.close()


def test_touch_person_updates_last_seen(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        identity.touch_person(conn, "wp:12", display_name="阿东", now="2026-01-01T00:00:00+00:00")
        identity.touch_person(conn, "wp:12", display_name="阿东", now="2026-01-02T00:00:00+00:00")
        row = conn.execute("SELECT * FROM persons WHERE person_key = 'wp:12'").fetchone()
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen_at"] == "2026-01-02T00:00:00+00:00"
    finally:
        conn.close()
