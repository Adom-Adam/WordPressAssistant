"""梅林自己收藏表情：只收表情包，且要她心情不错。"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.memory import db as memory_db
from app.stickers import collect, store

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8cfc0500f000485018084a98c21"
    "0000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("STICKER_DIR", str(tmp_path / "stickers"))
    monkeypatch.setenv("VISION_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_download(monkeypatch: Any, data: bytes = PNG) -> None:
    monkeypatch.setattr(
        collect, "download_image", lambda *a, **k: (data, "image/png")
    )


def _analysis(**overrides: Any) -> dict[str, Any]:
    base = {
        "description": "一只柴犬",
        "kind": "sticker",
        "tone": "warm",
        "emotions": ["开心"],
        "origin": "原神",
        "known_origin": "原神",
        "text": "",
        "collect_score": 0.9,
    }
    base.update(overrides)
    return base


def test_collects_sticker_when_positive(monkeypatch: Any) -> None:
    _patch_download(monkeypatch)
    monkeypatch.setattr(collect, "analyze_bytes", lambda *a, **k: _analysis())

    sticker_id = collect.maybe_collect(
        "https://gchat.qpic.cn/a.gif",
        source_person="wp:1",
        appraisal_event="smalltalk",
        library_max=100,
        day="2026-11-16",
        global_limit=10,
        user_limit=5,
    )

    assert sticker_id is not None
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        sticker = store.get_sticker(conn, sticker_id)
    finally:
        conn.close()
    assert sticker is not None
    assert sticker.source == "collected"
    assert sticker.source_person == "wp:1"
    assert sticker.origin_name == "原神"
    assert sticker.tone == "warm"


def test_does_not_collect_photos(monkeypatch: Any) -> None:
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        collect, "analyze_bytes", lambda *a, **k: _analysis(kind="photo")
    )

    assert (
        collect.maybe_collect(
            "https://gchat.qpic.cn/a.png",
            source_person="wp:1",
            appraisal_event="smalltalk",
            library_max=100,
            day="2026-11-16",
            global_limit=10,
            user_limit=5,
        )
        is None
    )


def test_does_not_collect_when_not_positive(monkeypatch: Any) -> None:
    _patch_download(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("负面情绪下不应该下载或分析表情")

    monkeypatch.setattr(collect, "download_image", fail)
    monkeypatch.setattr(collect, "analyze_bytes", fail)

    assert (
        collect.maybe_collect(
            "https://gchat.qpic.cn/a.gif",
            source_person="wp:1",
            appraisal_event="hostility",
            library_max=100,
            day="2026-11-16",
            global_limit=10,
            user_limit=5,
        )
        is None
    )


def test_low_score_is_not_collected(monkeypatch: Any) -> None:
    _patch_download(monkeypatch)
    monkeypatch.setattr(
        collect, "analyze_bytes", lambda *a, **k: _analysis(collect_score=0.2)
    )

    assert (
        collect.maybe_collect(
            "https://gchat.qpic.cn/a.gif",
            source_person="wp:1",
            appraisal_event="smalltalk",
            library_max=100,
            day="2026-11-16",
            global_limit=10,
            user_limit=5,
        )
        is None
    )
