"""Chat image understanding: URL allowlist, cache and budget.

These tests never touch the network: the downloader and model client are
replaced with fakes, so the assertions describe our own logic.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from app.channels.qq import image_understanding as iu
from app.config import get_settings
from app.memory import db as memory_db


@pytest.fixture(autouse=True)
def memory_isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_only_qq_image_hosts_are_allowed() -> None:
    assert iu._allowed_url("https://gchat.qpic.cn/gchatpic_new/1/2")
    assert iu._allowed_url("https://multimedia.nt.qq.com.cn/download?appid=1407")
    assert not iu._allowed_url("http://gchat.qpic.cn/gchatpic_new/1/2")
    assert not iu._allowed_url("https://example.com/a.png")
    assert not iu._allowed_url("file:///tmp/a.png")


def test_cached_description_skips_model_call(monkeypatch: Any) -> None:
    conn = memory_db.connect()
    memory_db.init_schema(conn)
    conn.execute(
        """INSERT INTO qq_image_cache (key, description, model, created_at)
           VALUES (?, ?, ?, ?)""",
        ("hash-1", "一只柴犬", "deepseek-flash", "2026-11-14T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(iu, "download_image", lambda *a, **k: (b"data", "image/png"))
    monkeypatch.setattr(iu, "hashlib", types.SimpleNamespace(sha256=_fake_sha256("hash-1")))

    def fail_client() -> Any:
        raise AssertionError("cached image must not call the model")

    monkeypatch.setattr(iu, "openai_client", fail_client)

    assert iu.describe_image("https://gchat.qpic.cn/a.png", person_key="wp:7", day="2026-11-14") == "一只柴犬"


def test_budget_exhaustion_returns_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(iu, "download_image", lambda *a, **k: (b"data", "image/png"))
    monkeypatch.setattr(iu, "hashlib", types.SimpleNamespace(sha256=_fake_sha256("hash-2")))

    def fail_client() -> Any:
        raise AssertionError("budget exhausted must not call the model")

    monkeypatch.setattr(iu, "openai_client", fail_client)

    assert iu._reserve_budget("wp:7", "2026-11-14", 10, 1) is True
    assert iu._reserve_budget("wp:7", "2026-11-14", 10, 1) is False
    assert (
        iu.describe_image(
            "https://gchat.qpic.cn/a.png",
            person_key="wp:7",
            day="2026-11-14",
            global_limit=10,
            user_limit=1,
        )
        is None
    )


def test_failed_model_call_releases_budget(monkeypatch: Any) -> None:
    conn = memory_db.connect()
    memory_db.init_schema(conn)
    conn.close()

    monkeypatch.setattr(iu, "download_image", lambda *a, **k: (b"data", "image/png"))
    monkeypatch.setattr(iu, "hashlib", types.SimpleNamespace(sha256=_fake_sha256("hash-3")))

    class BrokenClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs: Any) -> Any:
                    raise RuntimeError("boom")

    monkeypatch.setattr(iu, "openai_client", lambda: BrokenClient())

    assert (
        iu.describe_image(
            "https://gchat.qpic.cn/a.png",
            person_key="wp:7",
            day="2026-11-14",
            global_limit=10,
            user_limit=1,
        )
        is None
    )

    conn = memory_db.connect()
    try:
        used = {
            row["scope"]: int(row["used"])
            for row in conn.execute(
                "SELECT scope, used FROM qq_image_usage WHERE day = ?",
                ("2026-11-14",),
            )
        }
    finally:
        conn.close()
    assert used == {"global": 0, "person:wp:7": 0}


def _fake_sha256(value: str) -> Any:
    class _Hash:
        @staticmethod
        def hexdigest() -> str:
            return value

    return lambda data: _Hash()
