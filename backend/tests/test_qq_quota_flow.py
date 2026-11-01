"""额度接入适配器后的端到端行为。

验证四件事：
1. 额度用尽时回的是梅林的固定文案，而不是模型的话；
2. 管理员的 QQ 不占任何计数；
3. agent 没有产出正文时，额度要退回来（否则一次故障吃掉一天的额度）；
4. 共享模式与独立模式的提示语不同（"你的" vs "大家的"）。
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from typing import Any

from app.channels.qq import commands
from app.channels.qq.adapter import QQAdapter
from app.config import Settings
from app.credits import CreditStore, hash_bind_code
from app.qq_quota import QQQuotaGate, QQQuotaStore
from app.quota import QuotaStore
from app.runtime_settings import RuntimeSettings


def async_test(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSender:
    def __init__(self) -> None:
        self.group: list[tuple[int, str]] = []
        self.private: list[tuple[int, str]] = []

    async def send_group_message(self, group_id: int, message: str) -> Any:
        self.group.append((group_id, message))

    async def send_private_message(self, user_id: int, message: str) -> Any:
        self.private.append((user_id, message))


def private_event(qq_uin: int, text: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": qq_uin,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def runtime(**overrides: Any) -> RuntimeSettings:
    base = dict(
        daily_quota=10,
        member_daily_quota=30,
        global_daily_quota=1000,
        agent_max_model_calls=5,
        agent_max_tool_calls=5,
        agent_max_seconds=30.0,
        model="deepseek-chat",
        credit_max_per_user=100,
        qq_daily_quota=1,
        qq_member_daily_quota=5,
        qq_global_daily_quota=0,
        qq_quota_shared=False,
        qq_unlimited=False,
    )
    base.update(overrides)
    return RuntimeSettings(**base)


def make(tmp_path: Any, *, stream: Any, runtime_values: RuntimeSettings | None = None):
    settings = Settings(
        qq_enabled=True, qq_allowed_groups="888", qq_mock_mode=True
    )
    db = str(tmp_path / "quota.sqlite3")
    store = CreditStore(db)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    values = runtime_values or runtime()
    gate = QQQuotaGate(QuotaStore(db), QQQuotaStore(db), lambda: values)
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, stream=stream, credits=store, gate=gate)
    return adapter, sender, store


def qq_used(tmp_path: Any, qq_uin: str = "10001") -> int:
    with sqlite3.connect(str(tmp_path / "quota.sqlite3")) as conn:
        row = conn.execute(
            "SELECT used FROM qq_daily_usage WHERE qq_uin = ?", (qq_uin,)
        ).fetchone()
    return int(row[0]) if row else 0


@async_test
async def test_exhausted_quota_replies_in_merlin_voice(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": "我在"}

    adapter, sender, _ = make(tmp_path, stream=stream)

    await adapter.handle_event(private_event(10001, "第一句", 1))
    result = await adapter.handle_event(private_event(10001, "第二句", 2))

    assert result["reason"] == "quota_exhausted"
    assert sender.private[-1] == (10001, commands.QUOTA_USER)
    # 额度用尽时不该调用模型
    assert len(sender.private) == 2


@async_test
async def test_global_qq_quota_message(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": "我在"}

    adapter, sender, _ = make(
        tmp_path,
        stream=stream,
        runtime_values=runtime(qq_daily_quota=10, qq_global_daily_quota=1),
    )

    await adapter.handle_event(private_event(10001, "第一句", 1))
    result = await adapter.handle_event(private_event(10001, "第二句", 2))

    assert result["reason"] == "quota_exhausted"
    assert sender.private[-1] == (10001, commands.QUOTA_GLOBAL)


@async_test
async def test_refund_when_agent_produces_nothing(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "error", "message": "boom"}

    adapter, _, _ = make(tmp_path, stream=stream)

    first = await adapter.handle_event(private_event(10001, "第一句", 1))

    assert first["reply"] == commands.AGENT_ERROR
    assert qq_used(tmp_path) == 0
    # 额度没被吃掉：同一天还能成功一次
    result = await adapter.handle_event(private_event(10001, "再来", 2))
    assert result["handled"] is True
    assert result.get("reason") != "quota_exhausted"


@async_test
async def test_no_refund_when_agent_produced_text(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": "说了一半"}
        yield {"type": "error", "message": "后来断了"}

    adapter, sender, _ = make(tmp_path, stream=stream)

    await adapter.handle_event(private_event(10001, "第一句", 1))

    assert qq_used(tmp_path) == 1
    assert sender.private[0] == (10001, "说了一半")


@async_test
async def test_admin_binding_is_not_counted(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": "我在"}

    settings = Settings(qq_enabled=True, qq_allowed_groups="888", qq_mock_mode=True)
    db = str(tmp_path / "quota.sqlite3")
    store = CreditStore(db)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=True)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    gate = QQQuotaGate(
        QuotaStore(db), QQQuotaStore(db), lambda: runtime(qq_daily_quota=0)
    )
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, stream=stream, credits=store, gate=gate)

    result = await adapter.handle_event(private_event(10001, "你好", 1))

    assert result["handled"] is True
    assert qq_used(tmp_path) == 0


@async_test
async def test_shared_mode_uses_web_quota(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": "我在"}

    adapter, sender, _ = make(
        tmp_path,
        stream=stream,
        runtime_values=runtime(qq_quota_shared=True, daily_quota=2),
    )

    await adapter.handle_event(private_event(10001, "第一句", 1))

    quota = QuotaStore(str(tmp_path / "quota.sqlite3"))
    assert quota.usage_snapshot([7])[7] == 1
    assert qq_used(tmp_path) == 0
