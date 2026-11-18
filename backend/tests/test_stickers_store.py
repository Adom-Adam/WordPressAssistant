"""表情库的存储层：素材、标签、出处。"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.memory import db as memory_db
from app.stickers import store


@pytest.fixture(autouse=True)
def memory_isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def conn() -> Any:
    connection = memory_db.connect()
    memory_db.init_schema(connection)
    yield connection
    connection.close()


def test_add_sticker_saves_tags_and_origin(conn: Any) -> None:
    sticker_id = store.add_sticker(
        conn,
        content_hash="hash-1",
        file_name="hash-1.gif",
        format="image/gif",
        description="一个暗中观察的表情",
        tone="warm",
        origin="原神",
        tags=["偷偷看", "暗中观察", "偷偷看"],
    )

    sticker = store.get_sticker(conn, sticker_id)
    assert sticker is not None
    assert sticker.tone == "warm"
    assert sticker.origin_name == "原神"
    assert set(sticker.tags) == {"偷偷看", "暗中观察"}


def test_same_content_hash_is_not_imported_twice(conn: Any) -> None:
    first = store.add_sticker(
        conn,
        content_hash="same",
        file_name="same.gif",
        format="image/gif",
    )
    second = store.add_sticker(
        conn,
        content_hash="same",
        file_name="same.gif",
        format="image/gif",
        description="重复导入",
    )

    assert second == first
    assert store.count_stickers(conn) == 1


def test_update_sticker_replaces_tags_and_tone(conn: Any) -> None:
    sticker_id = store.add_sticker(
        conn,
        content_hash="hash-2",
        file_name="hash-2.png",
        format="image/png",
        tone="warm",
        origin="原神",
        tags=["开心"],
    )

    assert store.update_sticker(
        conn,
        sticker_id,
        tone="cold",
        origin="鸣潮",
        tags=["冷漠", "无语"],
        enabled=False,
    )

    sticker = store.get_sticker(conn, sticker_id)
    assert sticker is not None
    assert sticker.tone == "cold"
    assert sticker.origin_name == "鸣潮"
    assert set(sticker.tags) == {"冷漠", "无语"}
    assert sticker.enabled is False


def test_delete_sticker_removes_tags(conn: Any) -> None:
    sticker_id = store.add_sticker(
        conn,
        content_hash="hash-3",
        file_name="hash-3.png",
        format="image/png",
        tags=["开心"],
    )

    assert store.delete_sticker(conn, sticker_id) is True
    assert store.get_sticker(conn, sticker_id) is None
    rows = conn.execute(
        "SELECT COUNT(*) FROM sticker_tags WHERE sticker_id = ?", (sticker_id,)
    ).fetchone()
    assert rows[0] == 0


def test_list_filters_by_tone_enabled_and_text(conn: Any) -> None:
    warm = store.add_sticker(
        conn,
        content_hash="w1",
        file_name="w1.png",
        format="image/png",
        description="可爱的猫猫",
        tone="warm",
        origin="明日方舟",
        tags=["可爱"],
    )
    cold = store.add_sticker(
        conn,
        content_hash="c1",
        file_name="c1.png",
        format="image/png",
        description="冷漠地看",
        tone="cold",
        origin="鸣潮",
        tags=["冷漠"],
    )
    store.update_sticker(conn, cold, enabled=False)

    assert [s.id for s in store.list_stickers(conn, tone="warm")] == [warm]
    assert [s.id for s in store.list_stickers(conn, query="猫猫")] == [warm]
    assert [s.id for s in store.list_stickers(conn, query="鸣潮")] == [cold]
    assert [s.id for s in store.list_stickers(conn, enabled=False)] == [cold]
    assert store.count_stickers(conn) == 2
    assert store.count_stickers(conn, enabled=True) == 1


def test_inbox_import_is_idempotent(tmp_path: Any, monkeypatch: Any) -> None:
    from app.stickers import files, sync

    monkeypatch.setenv("STICKER_DIR", str(tmp_path / "stickers"))
    get_settings.cache_clear()
    from app.config import get_settings as settings

    settings.cache_clear()
    files.ensure_dirs()
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63f8cfc0500f000485018084a98c21"
        "0000000049454e44ae426082"
    )
    (files.inbox_dir() / "cat.png").write_bytes(png)

    first = sync.import_inbox(with_vision=False)
    second = sync.import_inbox(with_vision=False)

    assert first["imported"] == 1
    assert second["skipped"] == 1
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        assert store.count_stickers(conn) == 1
    finally:
        conn.close()


def test_origin_alias_resolves_to_same_origin(conn: Any) -> None:
    first = store.add_sticker(
        conn,
        content_hash="a1",
        file_name="a1.png",
        format="image/png",
        origin="原神",
    )
    store.ensure_origin(conn, "原神", aliases=["Genshin", "Genshin Impact"])
    second = store.add_sticker(
        conn,
        content_hash="a2",
        file_name="a2.png",
        format="image/png",
        origin="genshin",
    )

    left = store.get_sticker(conn, first)
    right = store.get_sticker(conn, second)
    assert left is not None and right is not None
    assert left.origin_id == right.origin_id
