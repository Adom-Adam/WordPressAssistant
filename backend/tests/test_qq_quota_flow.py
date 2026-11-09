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

import pytest

from app.channels.qq import commands
from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.persona.models import PersonaVersion
from app.qq_quota import QQQuotaGate, QQQuotaStore
from app.quota import QuotaStore
from app.runtime_settings import RuntimeSettings


@pytest.fixture(autouse=True)
def memory_isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    """每个测试用临时记忆库，别往真实的 backend/data/ 里写事件。"""
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
        appraisal_enabled=True,
        appraisal_timeout_seconds=1.2,
        appraisal_global_daily_budget=1000,
        appraisal_per_user_daily_budget=60,
        proactive_enabled=False,
        proactive_tick_minutes=30,
        proactive_think_threshold=0.35,
        proactive_speak_threshold=0.72,
        proactive_min_interval_minutes=180,
        proactive_per_user_daily_limit=3,
        proactive_global_daily_limit=20,
        proactive_group_interval_minutes=240,
        proactive_group_daily_limit=6,
    )
    base.update(overrides)
    return RuntimeSettings(**base)


def make_persona() -> PersonaVersion:
    """额度测试只关心计数，不需要概率沉默和 1.5 秒防抖。"""
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
                    "min_gap_ms": 0,
                    "max_gap_ms": 0,
                    "chars_per_second": 50.0,
                },
            },
        }
    )


def make(tmp_path: Any, *, stream: Any, runtime_values: RuntimeSettings | None = None):
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_mock_mode=True,
        qq_private_reply_policy="always",
    )
    db = str(tmp_path / "quota.sqlite3")
    store = CreditStore(db)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    values = runtime_values or runtime()
    gate = QQQuotaGate(QuotaStore(db), QQQuotaStore(db), lambda: values)
    sender = FakeSender()
    adapter = QQAdapter(
        settings,
        sender,
        stream=stream,
        credits=store,
        gate=gate,
        persona=make_persona(),
    )
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

    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_mock_mode=True,
        qq_private_reply_policy="always",
    )
    db = str(tmp_path / "quota.sqlite3")
    store = CreditStore(db)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=True)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    gate = QQQuotaGate(
        QuotaStore(db), QQQuotaStore(db), lambda: runtime(qq_daily_quota=0)
    )
    sender = FakeSender()
    adapter = QQAdapter(
        settings,
        sender,
        stream=stream,
        credits=store,
        gate=gate,
        persona=make_persona(),
    )

    result = await adapter.handle_event(private_event(10001, "你好", 1))

    assert result["handled"] is True
    assert qq_used(tmp_path) == 0


@async_test
async def test_shared_mode_web_global_exhausted_message(tmp_path: Any) -> None:
    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": "我在"}

    adapter, sender, _ = make(
        tmp_path,
        stream=stream,
        runtime_values=runtime(
            qq_quota_shared=True, daily_quota=10, global_daily_quota=1
        ),
    )

    await adapter.handle_event(private_event(10001, "第一句", 1))
    result = await adapter.handle_event(private_event(10001, "第二句", 2))

    assert result["reason"] == "quota_exhausted"
    assert sender.private[-1] == (10001, commands.QUOTA_GLOBAL)


@async_test
async def test_empty_delta_counts_as_no_reply(tmp_path: Any) -> None:
    """只有空 delta 也算没产出：不能占额度、不能写历史。"""

    async def stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": ""}
        yield {"type": "done"}

    adapter, sender, _ = make(tmp_path, stream=stream)

    result = await adapter.handle_event(private_event(10001, "第一句", 1))

    assert result["reply"] == commands.AGENT_ERROR
    assert qq_used(tmp_path) == 0


@async_test
async def test_interrupted_generation_refunds_quota(tmp_path: Any) -> None:
    """被打断的回合没有给用户任何正文，不能吃掉额度。"""
    started = asyncio.Event()
    calls: list[int] = []

    async def blocking_stream(messages: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            started.set()
            await asyncio.sleep(30)
        yield {"type": "delta", "content": "我在"}

    adapter, sender, _ = make(tmp_path, stream=blocking_stream)

    first = asyncio.create_task(adapter.handle_event(private_event(10001, "第一句", 1)))
    await started.wait()
    second = asyncio.create_task(adapter.handle_event(private_event(10001, "第二句", 2)))
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["reason"] == "interrupted"
    assert second_result.get("reason") is None
    # 第一次占用的额度已退回；只有合并后的第二次真的计数。
    assert qq_used(tmp_path) == 1
    assert sender.private == [(10001, "我在")]


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
