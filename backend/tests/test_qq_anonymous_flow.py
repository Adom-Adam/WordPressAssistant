"""QQ 匿名模式：能聊，但什么都不留。

匿名要同时满足三件事，缺一件就不算“匿名”：
1. 不读也不写记忆（事件、关系、待续话题一律不产生）；
2. 用独立额度，不占登录用户的份；
3. 提示是温的——说“我今晚记不住”，不说“额度”。

引导提示七天最多一次：每句话后面都跟一句“去登记吧”很快就会被烦到。
"""

from __future__ import annotations

import asyncio
import functools
import random
from dataclasses import replace
from typing import Any

import pytest

from app.anonymous_quota import AnonymousQuotaStore
from app.channels.qq import commands
from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.qq_quota import QQUsage
from app.runtime_settings import RuntimeSettingsStore, get_runtime_store


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    get_settings.cache_clear()
    get_runtime_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_runtime_store.cache_clear()


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSender:
    def __init__(self) -> None:
        self.group: list[tuple[int, str]] = []
        self.private: list[tuple[int, str]] = []

    async def send_group_message(self, group_id: int, text: str) -> None:
        self.group.append((group_id, text))

    async def send_private_message(self, user_id: int, text: str) -> None:
        self.private.append((user_id, text))


class AllowAllGate:
    def reserve(self, **kwargs: Any) -> tuple[str, Any]:
        return "ok", QQUsage("admin", None, None)

    def refund(self, usage: Any) -> None:
        return None


def stream_ok(text: str = "我在") -> Any:
    async def _stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": text}

    return _stream


def private_event(qq_uin: int, text: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": qq_uin,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def group_event(qq_uin: int, text: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "group_id": 888,
        "user_id": qq_uin,
        "message_id": message_id,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": text}},
        ],
    }


def persona() -> PersonaVersion:
    return PersonaVersion.model_validate(
        {
            "schema": "l2d.persona/v1",
            "id": "merlin",
            "version": 1,
            "name": "梅林",
            "core": "C",
            "dynamics": {
                "namespace": {"state": "merlin"},
                "pacing": {
                    "debounce_seconds": 0.0,
                    "debounce_max_seconds": 0.0,
                    "max_gap_ms": 0,
                    "min_gap_ms": 0,
                    "chars_per_second": 50.0,
                },
            },
        }
    )


def runtime(tmp_path, **overrides):
    base = RuntimeSettingsStore(
        str(tmp_path / "runtime.json"), Settings()
    ).defaults
    return replace(base, **overrides)


def make_adapter(tmp_path, *, settings_runtime=None):
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_admin_users="",
        qq_mock_mode=True,
        qq_private_reply_policy="always",
        wp_site_url="",
    )
    sender = FakeSender()
    adapter = QQAdapter(
        settings,
        sender,
        stream=stream_ok(),
        credits=CreditStore(str(tmp_path / "quota.sqlite3")),
        gate=AllowAllGate(),
        persona=persona(),
        rng=random.Random(0),
        anonymous_quota=AnonymousQuotaStore(str(tmp_path / "anonymous.sqlite3")),
        runtime=lambda: settings_runtime
        or RuntimeSettingsStore(str(tmp_path / "runtime.json"), Settings()).defaults,
    )
    return adapter, sender


def stats() -> dict[str, int]:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("events", "memories", "relationship_state")
        }
    finally:
        conn.close()


@async_test
async def test_anonymous_private_replies_without_memory(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(
            tmp_path,
            anonymous_qq_enabled=True,
            anonymous_qq_daily_quota=2,
            anonymous_global_daily_quota=10,
        ),
    )

    result = await adapter.handle_event(private_event(12345, "你好"))

    assert result["handled"] is True
    assert sender.private
    # 提示不出现“额度”：她不该用运营口径说话。
    assert "额度" not in sender.private[0][1]
    assert stats() == {"events": 0, "memories": 0, "relationship_state": 0}


@async_test
async def test_anonymous_quota_exhausted_uses_persona_copy(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(
            tmp_path,
            anonymous_qq_enabled=True,
            anonymous_qq_daily_quota=1,
        ),
    )

    await adapter.handle_event(private_event(12345, "一", message_id=1))
    await adapter.handle_event(private_event(12345, "二", message_id=2))

    assert sender.private[-1][1] == commands.ANON_QUOTA_USER


@async_test
async def test_anonymous_global_quota_exhausted_uses_its_own_copy(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(
            tmp_path,
            anonymous_qq_enabled=True,
            anonymous_qq_daily_quota=10,
            anonymous_global_daily_quota=1,
        ),
    )

    await adapter.handle_event(private_event(11111, "一", message_id=1))
    await adapter.handle_event(private_event(22222, "二", message_id=2))

    assert sender.private[-1][1] == commands.ANON_QUOTA_GLOBAL


@async_test
async def test_anonymous_off_unbound_still_guides(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(tmp_path, anonymous_qq_enabled=False),
    )

    result = await adapter.handle_event(private_event(12345, "你好"))

    assert result["reason"] == "guide_unbound"
    assert "图书馆" in sender.private[-1][1]
    assert stats()["events"] == 0


@async_test
async def test_anonymous_group_does_not_write_timeline(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(tmp_path, anonymous_qq_enabled=True),
    )

    result = await adapter.handle_event(group_event(12345, "大家好"))

    assert result["handled"] is True
    assert sender.group
    assert stats()["events"] == 0


@async_test
async def test_first_anonymous_private_reply_gets_guide_once(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(
            tmp_path, anonymous_qq_enabled=True, anonymous_guide_enabled=True
        ),
    )

    await adapter.handle_event(private_event(12345, "你好", 1))
    assert "图书馆" in sender.private[-1][1]

    await adapter.handle_event(private_event(12345, "在吗", 2))
    assert "图书馆" not in sender.private[-1][1]


@async_test
async def test_memory_words_get_their_own_hint(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(
            tmp_path, anonymous_qq_enabled=True, anonymous_guide_enabled=True
        ),
    )

    await adapter.handle_event(private_event(12345, "你好", 1))
    await adapter.handle_event(private_event(12345, "你还记得我吗", 2))

    # 第一次是“初次见面”的提示，第二次换成“我明天就忘”的说法。
    assert sender.private[-1][1] == commands.anon_memory_hint("")


@async_test
async def test_guide_can_be_disabled(tmp_path) -> None:
    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(
            tmp_path, anonymous_qq_enabled=True, anonymous_guide_enabled=False
        ),
    )

    await adapter.handle_event(private_event(12345, "你好"))

    assert "图书馆" not in sender.private[-1][1]
