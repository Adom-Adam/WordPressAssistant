"""白名单 QQ 走“已登记”的完整链路。

站长的诉求：几个熟人不想注册博客，但希望梅林真的记住他们。白名单不是
“绑到某个账号”，而是“这个号按有身份的人对待”——记忆、关系、主动消息
都照常，只是身份是 `qq:<uin>`。

顺序必须是“白名单优先于匿名”：同时开了匿名和白名单时，白名单的人不能
被降级成匿名，否则站长加了名单却还是记不住。
"""

from __future__ import annotations

import asyncio
import functools
import random
from dataclasses import replace
from typing import Any

import pytest

from app.anonymous_quota import AnonymousQuotaStore
from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db
from app.memory import identity
from app.persona.models import PersonaVersion
from app.qq_quota import QQUsage
from app.qq_whitelist import add_entry, remove_entry
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


def allow(qq_uin: str = "12345", *, role: str = "special") -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        add_entry(conn, qq_uin, added_by="99")
        identity.touch_person(conn, f"qq:{qq_uin}", qq_uin=qq_uin)
        conn.execute(
            "UPDATE persons SET role = ? WHERE person_key = ?",
            (role, f"qq:{qq_uin}"),
        )
        conn.commit()
    finally:
        conn.close()


def count(sql: str, params: tuple = ()) -> int:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


@async_test
async def test_whitelisted_unbound_private_chat_writes_memory(tmp_path) -> None:
    allow("12345")
    adapter, _sender = make_adapter(tmp_path)

    result = await adapter.handle_event(private_event(12345, "你好"))

    assert result["handled"] is True
    # 一问一答两条事件：说明真的走了完整回合，不是回一句引导。
    assert count("SELECT COUNT(*) FROM events WHERE person_key = 'qq:12345'") >= 2
    assert (
        count(
            "SELECT COUNT(*) FROM relationship_state WHERE person_key = 'qq:12345'"
        )
        == 1
    )


@async_test
async def test_whitelist_wins_over_anonymous(tmp_path) -> None:
    allow("12345")
    adapter, _sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(tmp_path, anonymous_qq_enabled=True),
    )

    await adapter.handle_event(private_event(12345, "你好"))

    assert count("SELECT COUNT(*) FROM events WHERE person_key = 'qq:12345'") > 0


@async_test
async def test_removed_whitelist_falls_back_to_anonymous(tmp_path) -> None:
    allow("12345")
    conn = memory_db.connect()
    try:
        remove_entry(conn, "12345")
        conn.commit()
    finally:
        conn.close()

    adapter, sender = make_adapter(
        tmp_path,
        settings_runtime=runtime(tmp_path, anonymous_qq_enabled=True),
    )
    result = await adapter.handle_event(private_event(12345, "你好"))

    assert result["handled"] is True
    assert count("SELECT COUNT(*) FROM events WHERE person_key = 'qq:12345'") == 0
    # 匿名仍然会回话，只是不记事。
    assert sender.private


@async_test
async def test_whitelisted_qq_keeps_data_dormant_after_removal(tmp_path) -> None:
    """移出名单不清数据：以后重新加回来还能接着聊。"""
    allow("12345")
    adapter, _sender = make_adapter(tmp_path)
    await adapter.handle_event(private_event(12345, "你好", 1))

    conn = memory_db.connect()
    try:
        remove_entry(conn, "12345")
        conn.commit()
    finally:
        conn.close()

    assert count("SELECT COUNT(*) FROM events WHERE person_key = 'qq:12345'") >= 2
    assert count("SELECT COUNT(*) FROM persons WHERE person_key = 'qq:12345'") == 1
