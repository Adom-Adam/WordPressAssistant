"""私聊、命令分发与未绑定引导。

这一层最容易出的错不是"不会回"，而是"回错人/回错地方"：
- 未绑定的 QQ 不该拿到 AI 回答，只该拿到引导；
- 群里 @ 未绑定的人，不能把绑定流程展开在群里；
- 斜杠命令无论谁发，都不能进模型，也不能进历史。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import pytest

from app.channels.qq import commands
from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.qq_quota import QQUsage


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


class AllowAllGate:
    """额度永不拦人，也不计数。

    不注入的话适配器会去创建全局额度门面，读写 `backend/data/quota.sqlite3`
    这个真实文件；测试跑几轮就会把每人每日额度耗光，然后莫名其妙红掉。
    """

    def reserve(self, **kwargs: Any) -> tuple[str, Any]:
        return "ok", QQUsage("admin", None, None)

    def refund(self, usage: Any) -> None:
        return None


def make_adapter(
    tmp_path: Any, *, stream: Any = None, wp_site_url: str = ""
) -> tuple[QQAdapter, FakeSender, CreditStore]:
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_admin_users="",
        qq_mock_mode=True,
        wp_site_url=wp_site_url,
    )
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    sender = FakeSender()
    adapter = QQAdapter(
        settings,
        sender,
        stream=stream or stream_ok(),
        credits=store,
        gate=AllowAllGate(),
    )
    return adapter, sender, store


def bind(store: CreditStore, qq_uin: str = "10001", user_id: int = 7) -> None:
    store.set_bind_code(user_id, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq(qq_uin, hash_bind_code("1234-5678"))


@async_test
async def test_unbound_private_gets_guide_every_message(tmp_path: Any) -> None:
    adapter, sender, _ = make_adapter(tmp_path)

    first = await adapter.handle_event(private_event(10001, "在吗", 1))
    second = await adapter.handle_event(private_event(10001, "在吗", 2))

    assert first["reason"] == "guide_unbound"
    assert second["reason"] == "guide_unbound"
    assert [text for _, text in sender.private] == [
        commands.PRIVATE_GUIDE,
        commands.PRIVATE_GUIDE,
    ]


@async_test
async def test_private_guide_links_to_configured_site(tmp_path: Any) -> None:
    """站点地址来自配置，不写死在仓库里。

    仓库按公开处理，真实域名属于部署信息；后端 `.env` 的 WP_SITE_URL
    本来就要填（知识库同步、站内跳转都用它），引导文案直接复用它。
    """
    adapter, sender, _ = make_adapter(tmp_path, wp_site_url="https://example.com/")

    await adapter.handle_event(private_event(10001, "在吗"))

    text = sender.private[0][1]
    assert "https://example.com/" in text
    assert ".xyz" not in text


@async_test
async def test_private_guide_without_site_still_makes_sense(tmp_path: Any) -> None:
    adapter, sender, _ = make_adapter(tmp_path)

    await adapter.handle_event(private_event(10001, "在吗"))

    text = sender.private[0][1]
    # 没配站点时退化成不带链接的说法，流程本身仍然说得清。
    assert "「个人资料」" in text
    assert "/绑定 1234-5678" in text
    assert "http" not in text


@async_test
async def test_unbound_group_points_to_private(tmp_path: Any) -> None:
    adapter, sender, _ = make_adapter(tmp_path)

    result = await adapter.handle_event(group_event(10001, "你好"))

    assert result["reason"] == "guide_unbound"
    assert sender.group == [(888, commands.GROUP_GUIDE)]
    assert sender.private == []


@async_test
async def test_bound_private_reaches_agent(tmp_path: Any) -> None:
    adapter, sender, store = make_adapter(tmp_path)
    bind(store)

    result = await adapter.handle_event(private_event(10001, "讲个故事"))

    assert result["handled"] is True
    assert sender.private == [(10001, "我在")]


@async_test
async def test_bound_group_reaches_agent(tmp_path: Any) -> None:
    adapter, sender, store = make_adapter(tmp_path)
    bind(store)

    result = await adapter.handle_event(group_event(10001, "讲个故事"))

    assert result["handled"] is True
    assert sender.group == [(888, "我在")]


def recording_stream(seen: dict[str, Any], text: str = "我在") -> Any:
    """记录调用参数的一次流式回答。

    频道名决定了用哪一份人设覆盖层：私聊和群里说话的分寸不一样，
    所以适配器必须把“在哪说话”告诉 agent，而不是让它一律当成网页。
    """

    async def _stream(messages: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        yield {"type": "delta", "content": text}

    return _stream


@async_test
async def test_bound_private_tells_agent_it_is_private(tmp_path: Any) -> None:
    seen: dict[str, Any] = {}
    adapter, _, store = make_adapter(tmp_path, stream=recording_stream(seen))
    bind(store)

    await adapter.handle_event(private_event(10001, "讲个故事"))

    assert seen["channel"] == "qq_private"
    # QQ 里没有网页可看，页面上下文必须是空的。
    assert seen["page_url"] is None
    assert seen["page_title"] is None


@async_test
async def test_bound_group_tells_agent_it_is_group(tmp_path: Any) -> None:
    seen: dict[str, Any] = {}
    adapter, _, store = make_adapter(tmp_path, stream=recording_stream(seen))
    bind(store)

    await adapter.handle_event(group_event(10001, "讲个故事"))

    assert seen["channel"] == "qq_group"


@async_test
async def test_command_never_reaches_agent(tmp_path: Any) -> None:
    calls: list[Any] = []

    async def stream(messages: Any, **kwargs: Any) -> Any:
        calls.append(messages)
        yield {"type": "delta", "content": "不该出现"}

    adapter, sender, store = make_adapter(tmp_path, stream=stream)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)

    result = await adapter.handle_event(private_event(10001, "/绑定 1234-5678"))

    assert calls == []
    assert result["reason"] == "command"
    assert sender.private == [(10001, commands.BIND_OK)]
    assert store.qq_uin_of(7) == "10001"


@async_test
async def test_unknown_command_in_group_does_not_reach_agent(tmp_path: Any) -> None:
    calls: list[Any] = []

    async def stream(messages: Any, **kwargs: Any) -> Any:
        calls.append(messages)
        yield {"type": "delta", "content": "不该出现"}

    adapter, sender, _ = make_adapter(tmp_path, stream=stream)

    await adapter.handle_event(group_event(10001, "/天气"))

    assert calls == []
    assert "这条咒文我没见过呢" in sender.group[0][1]


@async_test
async def test_private_and_group_history_are_separate(tmp_path: Any) -> None:
    adapter, _, store = make_adapter(tmp_path)
    bind(store)

    await adapter.handle_event(private_event(10001, "私聊一", 1))
    await adapter.handle_event(group_event(10001, "群里一", 2))

    assert set(adapter._history) == {(0, 10001), (888, 10001)}


@async_test
async def test_duplicate_private_message_ignored(tmp_path: Any) -> None:
    adapter, sender, _ = make_adapter(tmp_path)

    await adapter.handle_event(private_event(10001, "在吗", 5))
    await adapter.handle_event(private_event(10001, "在吗", 5))

    assert len(sender.private) == 1


@async_test
async def test_command_not_written_to_history(tmp_path: Any) -> None:
    adapter, _, store = make_adapter(tmp_path)
    bind(store)

    await adapter.handle_event(private_event(10001, "/帮助", 1))

    assert (0, 10001) not in adapter._history


@async_test
async def test_qq_reply_receives_memory_context(tmp_path: Any, monkeypatch: Any) -> None:
    # 记忆库同样指到临时目录；关掉提炼，避免异步任务真的去调模型。
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    async def recording_stream(messages: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        yield {"type": "delta", "content": "我在"}

    adapter, sender, store = make_adapter(tmp_path, stream=recording_stream)
    bind(store)

    await adapter.handle_event(private_event(10001, "在吗"))

    assert "memory_context" in captured
    get_settings.cache_clear()
