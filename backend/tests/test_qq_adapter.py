"""QQ / OneBot 适配器的单元测试。

这些测试不启动 NapCat、不调用真实模型：
- 用一个假 sender 记录“要发到群里的消息”；
- 用一个假 stream 模拟 agent 的流式输出；
- 重点验证白名单、触发方式、去重、历史和身份。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.qq import get_qq_adapter
from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.schemas import ChatMessage


def async_test(fn: Any) -> Any:
    """不用 pytest-asyncio 的极简异步测试装饰器。

    项目现有的异步测试也是 `asyncio.run(...)` 风格；这样不额外引入插件，
    测试在任何只装了 pytest 的环境里都能跑。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSender:
    """记录发送内容，替代真实 NapCat。"""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_group_message(self, group_id: int, message: str) -> dict[str, Any]:
        self.sent.append((group_id, message))
        return {"message_id": len(self.sent)}


def make_stream(calls: list[dict[str, Any]], reply: str = "你好呀"):
    """构造一个假 stream，记录每次 agent 收到的参数。"""

    async def stream(
        messages: list[ChatMessage],
        page_url: str | None = None,
        page_title: str | None = None,
        *,
        is_admin: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        calls.append(
            {
                "messages": list(messages),
                "page_url": page_url,
                "page_title": page_title,
                "is_admin": is_admin,
            }
        )
        yield {"type": "delta", "content": reply}
        yield {"type": "done"}

    return stream


def make_event(
    text: str = "你好",
    *,
    group_id: int = 123,
    user_id: int = 456,
    self_id: int = 999,
    message_id: int = 1001,
    at_bot: bool = True,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if at_bot:
        segments.append({"type": "at", "data": {"qq": str(self_id)}})
    segments.append({"type": "text", "data": {"text": f" {text}"}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": self_id,
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": segments,
    }


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "qq_enabled": True,
        "qq_allowed_groups": "123",
        "qq_admin_users": "",
        "qq_trigger": "at",
        "qq_prefix": "梅林",
        "qq_history_turns": 2,
        "qq_mock_mode": True,
    }
    values.update(overrides)
    return Settings(**values)


@async_test
async def test_at_trigger_replies_in_allowed_group() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(make_settings(), sender, stream=make_stream(calls, "你好呀"))

    result = await adapter.handle_event(make_event("在吗"))

    assert result["handled"] is True
    assert result["reply"] == "你好呀"
    assert sender.sent == [(123, "你好呀")]
    assert calls[0]["messages"][-1].content == "在吗"


@async_test
async def test_group_not_in_allowlist_is_ignored() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(make_settings(), sender, stream=make_stream(calls))

    result = await adapter.handle_event(make_event(group_id=999))

    assert result == {"handled": False, "reason": "group_not_allowed"}
    assert sender.sent == []
    assert calls == []


@async_test
async def test_message_without_at_is_ignored_in_at_mode() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(make_settings(), sender, stream=make_stream(calls))

    result = await adapter.handle_event(make_event("在吗", at_bot=False))

    assert result["handled"] is False
    assert sender.sent == []


@async_test
async def test_duplicate_message_id_is_ignored() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(make_settings(), sender, stream=make_stream(calls))
    event = make_event("在吗")

    first = await adapter.handle_event(event)
    second = await adapter.handle_event(event)

    assert first["handled"] is True
    assert second == {"handled": False, "reason": "duplicate_message"}
    assert len(sender.sent) == 1


@async_test
async def test_prefix_trigger_strips_prefix() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(
        make_settings(qq_trigger="prefix", qq_prefix="梅林"),
        sender,
        stream=make_stream(calls),
    )

    result = await adapter.handle_event(make_event("梅林 在吗", at_bot=False))

    assert result["handled"] is True
    assert calls[0]["messages"][-1].content == "在吗"


@async_test
async def test_history_is_kept_per_group_and_user() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(make_settings(qq_history_turns=2), sender, stream=make_stream(calls))

    await adapter.handle_event(make_event("第一句", message_id=1))
    await adapter.handle_event(make_event("第二句", message_id=2))

    # 第二次调用应该带上一轮的用户消息 + 助手回复。
    assert [m.role for m in calls[1]["messages"]] == ["user", "assistant", "user"]
    assert calls[1]["messages"][0].content == "第一句"
    assert calls[1]["messages"][1].content == "你好呀"


@async_test
async def test_admin_user_is_marked_for_prompt() -> None:
    sender = FakeSender()
    calls: list[dict[str, Any]] = []
    adapter = QQAdapter(
        make_settings(qq_admin_users="456"),
        sender,
        stream=make_stream(calls),
    )

    await adapter.handle_event(make_event(user_id=456))

    assert calls[0]["is_admin"] is True


@async_test
async def test_empty_reply_uses_fallback() -> None:
    sender = FakeSender()

    async def empty_stream(
        messages: list[ChatMessage],
        page_url: str | None = None,
        page_title: str | None = None,
        *,
        is_admin: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "done"}

    adapter = QQAdapter(make_settings(), sender, stream=empty_stream)
    result = await adapter.handle_event(make_event())

    assert result["handled"] is True
    assert sender.sent[0][1] == "我在，但这次没想好怎么回。"


def test_long_reply_is_split_into_multiple_messages() -> None:
    chunks = QQAdapter._split_text("第一行\n" + "a" * 2_500)

    assert len(chunks) >= 3
    assert chunks[0] == "第一行"
    assert all(len(chunk) <= 1_200 for chunk in chunks)


# ----------------------------------------------------------------------
# HTTP 入口：验证 token/开关，并走一遍 mock 模式的完整链路
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clear_config_caches() -> None:
    """每个测试前后清掉 Settings/适配器缓存，避免环境变量串味。"""
    get_settings.cache_clear()
    get_qq_adapter.cache_clear()
    yield
    get_settings.cache_clear()
    get_qq_adapter.cache_clear()


def test_event_endpoint_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_EVENT_TOKEN", "secret")
    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/channels/qq/events",
            json=make_event(),
            headers={"X-OneBot-Token": "wrong"},
        )

    assert response.status_code == 401


def _napcat_signature(secret: str, body: bytes) -> str:
    """按 NapCat 的算法算签名：HMAC-SHA1(key=token, msg=原始body)。"""
    return "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def test_event_endpoint_accepts_napcat_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """NapCat 的 HTTP 客户端不发 Bearer，只发 X-Signature，必须能通过。"""
    outbox = tmp_path / "qq_outbox.jsonl"
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_EVENT_TOKEN", "secret")
    monkeypatch.setenv("QQ_ALLOWED_GROUPS", "123")
    monkeypatch.setenv("QQ_TRIGGER", "at")
    monkeypatch.setenv("QQ_MOCK_MODE", "true")
    monkeypatch.setenv("QQ_MOCK_OUTBOX", str(outbox))
    monkeypatch.setenv("MOCK_MODE", "true")
    get_settings.cache_clear()
    get_qq_adapter.cache_clear()

    # 签名针对的是“实际发出去的字节”，所以这里必须自己序列化一次。
    body = json.dumps(make_event("在吗"), ensure_ascii=False).encode("utf-8")

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/channels/qq/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _napcat_signature("secret", body),
            },
        )

    assert response.status_code == 204
    records = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert records[0]["action"] == "send_group_msg"


def test_event_endpoint_rejects_wrong_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_EVENT_TOKEN", "secret")
    get_settings.cache_clear()

    body = json.dumps(make_event("在吗"), ensure_ascii=False).encode("utf-8")

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/channels/qq/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _napcat_signature("wrong", body),
            },
        )

    assert response.status_code == 401


def test_event_endpoint_returns_404_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_ENABLED", "false")
    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/channels/qq/events", json=make_event())

    assert response.status_code == 404


def test_event_endpoint_runs_mock_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    outbox = tmp_path / "qq_outbox.jsonl"
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_EVENT_TOKEN", "secret")
    monkeypatch.setenv("QQ_ALLOWED_GROUPS", "123")
    monkeypatch.setenv("QQ_TRIGGER", "at")
    monkeypatch.setenv("QQ_MOCK_MODE", "true")
    monkeypatch.setenv("QQ_MOCK_OUTBOX", str(outbox))
    monkeypatch.setenv("MOCK_MODE", "true")
    get_settings.cache_clear()
    get_qq_adapter.cache_clear()

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/channels/qq/events",
            json=make_event("在吗"),
            headers={"X-OneBot-Token": "secret"},
        )

    assert response.status_code == 204
    records = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert records[0]["action"] == "send_group_msg"
    assert records[0]["params"]["group_id"] == 123
    assert "在吗" in records[0]["params"]["message"]
