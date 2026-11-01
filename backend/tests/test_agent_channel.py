"""频道名从 HTTP/QQ 入口一路传到提示词组装的这一层。

网页和 QQ 用同一套 agent，但“在哪里说话”会改变人设覆盖层：
网页面对的是图书馆里的访客，QQ 面对的是隔着通讯法阵说话的人。
如果 `stream_reply` 把 channel 吞掉，覆盖层就永远选不到，
表现就是梅林在 QQ 里也一直讲图书馆——所以这里单独钉住这一根线。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agent import core
from app.config import get_settings
from app.schemas import ChatMessage


class EmptyAgent:
    """一个不产出任何内容的 agent，用来验证参数传递而不是模型行为。"""

    async def astream(self, state: Any, stream_mode: Any = None) -> Any:
        return
        yield  # pragma: no cover - 让函数成为异步生成器


def test_stream_reply_forwards_channel(monkeypatch: Any) -> None:
    # 默认测试环境没有 API Key，会走 mock 分支直接返回，绕开 _build_agent。
    # 这里显式打开“真实模式”，再替换 _build_agent，避免真的调用模型。
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("MOCK_MODE", "false")
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    def fake_build_agent(page_url: Any, page_title: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return EmptyAgent()

    monkeypatch.setattr(core, "_build_agent", fake_build_agent)

    async def run() -> list[dict[str, Any]]:
        return [
            event
            async for event in core.stream_reply(
                [ChatMessage(role="user", content="在吗")],
                channel="qq_private",
            )
        ]

    try:
        events = asyncio.run(run())
    finally:
        get_settings.cache_clear()

    assert captured["channel"] == "qq_private"
    assert events == [{"type": "done"}]
