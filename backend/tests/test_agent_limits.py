"""验证 agent 的“保险丝”真的会断。

这里不用真实模型：FakeMessagesListChatModel 会按顺序吐出预设的 AIMessage。
我们让它每一条都要求调用同一个工具，模拟“模型陷入循环”的 bug。
如果限制生效，循环会在第 N 次之前停下，并把中文提示交给用户。
"""

from __future__ import annotations

import asyncio

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import tool

from app.agent.limits import (
    MODEL_LIMIT_MESSAGE,
    TOOL_LIMIT_MESSAGE,
    FriendlyModelCallLimitMiddleware,
    FriendlyToolCallLimitMiddleware,
)


class LoopingModel(FakeMessagesListChatModel):
    """一个“只会要求调工具”的假模型。

    FakeMessagesListChatModel 本身不支持 bind_tools（基类会抛
    NotImplementedError），但 create_agent 一定会调用它。
    这里直接返回 self：假模型不需要真的理解工具描述。
    """

    def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
        return self


def make_looping_agent(*, model_limit: int, tool_limit: int, counter: dict[str, int]):
    @tool
    def echo(text: str) -> str:
        """把收到的文字原样返回。"""
        counter["tool"] += 1
        return text

    # 准备 30 条“要求调用 echo”的消息，足够触发任何合理的上限。
    # 如果限制失效，测试会一直消耗这些消息，最后因数据耗尽而失败，
    # 而不是无限运行。
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "echo",
                    "args": {"text": "x"},
                    "id": f"call_{index}",
                    "type": "tool_call",
                }
            ],
        )
        for index in range(30)
    ]

    return create_agent(
        model=LoopingModel(responses=responses),
        tools=[echo],
        middleware=[
            FriendlyModelCallLimitMiddleware(
                run_limit=model_limit, exit_behavior="end"
            ),
            FriendlyToolCallLimitMiddleware(
                run_limit=tool_limit, exit_behavior="end"
            ),
        ],
    )


async def collect_agent_text(agent) -> str:  # type: ignore[no-untyped-def]
    """按 core.py 的方式消费流，只收集真正展示给用户的文字。"""
    parts: list[str] = []
    async for mode, payload in agent.astream(
        {"messages": [HumanMessage("hi")]},
        stream_mode=["messages", "updates"],
    ):
        if mode != "messages":
            continue
        chunk, _metadata = payload
        # 和 core.py 保持一致：模型流式输出是 AIMessageChunk，
        # 中间件注入的收尾提示是完整 AIMessage。
        if isinstance(chunk, (AIMessageChunk, AIMessage)) and chunk.content:
            parts.append(str(chunk.content))
    return "".join(parts)


def test_model_call_limit_stops_loop_and_shows_chinese_message() -> None:
    counter = {"tool": 0}
    agent = make_looping_agent(model_limit=3, tool_limit=10, counter=counter)

    text = asyncio.run(collect_agent_text(agent))

    # 第 3 次模型调用之后，中间件不再允许第 4 次。
    assert counter["tool"] == 3
    assert text.endswith(MODEL_LIMIT_MESSAGE)
    # 内置的英文技术提示不能泄漏到用户界面。
    assert "Model call limits exceeded" not in text


def test_tool_call_limit_stops_loop_and_shows_chinese_message() -> None:
    counter = {"tool": 0}
    agent = make_looping_agent(model_limit=10, tool_limit=2, counter=counter)

    text = asyncio.run(collect_agent_text(agent))

    # 第 3 个工具调用被拦截，所以只执行了 2 次。
    assert counter["tool"] == 2
    assert text.endswith(TOOL_LIMIT_MESSAGE)
    assert "Tool call limits exceeded" not in text
