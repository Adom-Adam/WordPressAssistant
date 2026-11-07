from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.memory import db, events


def _conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    return db.connect()


def test_record_personal_event(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        event_id = events.record_event(
            conn,
            person_key="wp:12",
            platform="web",
            channel_id="web:12",
            scope="personal",
            audience=["wp:12"],
            source="user_message",
            content="今天在学 ROS2",
            occurred_at="2026-11-06T10:00:00+00:00",
        )
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        assert row["person_key"] == "wp:12"
        assert row["audience_key"] == "wp:12"
        assert json.loads(row["audience"]) == ["wp:12"]
        assert row["occurred_at"] == "2026-11-06T10:00:00+00:00"
    finally:
        conn.close()


def test_group_event_uses_group_key(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        event_id = events.record_event(
            conn,
            person_key="wp:12",
            platform="qq",
            channel_id="group:789",
            scope="group",
            audience=["group:789"],
            source="user_message",
            content="大家好",
        )
        row = conn.execute("SELECT audience_key FROM events WHERE id = ?", (event_id,)).fetchone()
        assert row["audience_key"] == "group:789"
    finally:
        conn.close()


def test_content_is_cleaned_and_truncated(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        event_id = events.record_event(
            conn,
            person_key="wp:12",
            platform="web",
            channel_id="web:12",
            scope="personal",
            audience=["wp:12"],
            source="user_message",
            content="a\x00b" + "x" * 5000,
        )
        content = conn.execute("SELECT content FROM events WHERE id = ?", (event_id,)).fetchone()[0]
        assert "\x00" not in content
        assert len(content) == 4000
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,value",
    [("scope", "unknown"), ("sensitivity", "top-secret"), ("source", "guess")],
)
def test_invalid_enums_are_rejected(tmp_path, monkeypatch, field, value) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        kwargs = dict(
            person_key="wp:12",
            platform="web",
            channel_id="web:12",
            scope="personal",
            audience=["wp:12"],
            source="user_message",
            content="hi",
        )
        kwargs[field] = value
        with pytest.raises(ValueError):
            events.record_event(conn, **kwargs)
    finally:
        conn.close()


def test_empty_content_and_audience_are_rejected(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError):
            events.record_event(
                conn, person_key="wp:12", platform="web", channel_id="web:12",
                scope="personal", audience=[], source="user_message", content="hi",
            )
        with pytest.raises(ValueError):
            events.record_event(
                conn, person_key="wp:12", platform="web", channel_id="web:12",
                scope="personal", audience=["wp:12"], source="user_message", content="  ",
            )
    finally:
        conn.close()
