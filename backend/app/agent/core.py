"""Agent 核心：把「一次用户请求」变成「一串流式事件」。

====================================================================
先理解 LangChain 的 Agent 在做什么
====================================================================

普通聊天：用户消息 → 模型 → 回复。

Agent：用户消息 → 模型 → 如果模型说“我要调用工具” → 执行工具 → 把结果交回模型
       → 模型继续 → …… 直到模型不再调用工具，输出最终回答。

`create_agent`（LangChain 1.x 的入口）会把这个循环做成一张 **LangGraph 状态图**：
     ┌─────────┐   有 tool_calls    ┌─────────┐
     │  model  │ ─────────────────▶ │  tools  │
     └─────────┘ ◀───────────────── └─────────┘
          │ 没有 tool_calls
          ▼
        结束

我们不需要手写这个循环，只需要提供：
1. 一个聊天模型（model.py）
2. 工具列表（tools.py）
3. 系统提示词（prompts.py）

然后用 `agent.astream(..., stream_mode="messages")` 消费它产生的 token 流。

====================================================================
关于 stream_mode="messages"
====================================================================

LangGraph 的 `astream` 有多种流模式：
- "values"   ：每一步完整的状态快照（适合调试）
- "updates"  ：每一步“改了什么”（适合观察工具调用）
- "messages" ：模型生成的 **AIMessageChunk 增量**（适合做打字机效果，本项目用这个）

当 stream_mode="messages" 时，异步迭代的每一项是：
    (message_chunk, metadata)
其中 message_chunk 通常是 AIMessageChunk，metadata 描述它来自哪个节点/模型。
工具调用的参数也会以 AIMessageChunk 出现，但 content 是空字符串——
所以下面只把“非空 content”当作要给用户看的文字。

未来接入知识库时：
工具执行结果（ToolMessage）不会通过 "messages" 模式直接给出。
如果需要把“引用来源”作为单独事件发给前端，可以再叠加 "updates" 模式，
从 ToolMessage 的返回值里提取 post_id/title/url，整理成
{"type": "citations", ...} 事件（前端已经支持渲染）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage

from app.agent.model import build_chat_model
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOLS
from app.config import get_settings
from app.schemas import ChatMessage

logger = logging.getLogger(__name__)

# 把前端的 role 字符串映射到 LangChain 的消息类。
# 前端 schema 只允许 user / assistant；system 由 create_agent 的 system_prompt 负责，
# 这样客户端无法注入自己的“系统指令”，避免提示词注入。
_ROLE_TO_MESSAGE: dict[str, type[BaseMessage]] = {
    "user": HumanMessage,
    "assistant": AIMessage,
}


def _to_langchain_messages(messages: list[ChatMessage]) -> list[BaseMessage]:
    """把 Pydantic 的 ChatMessage 转成 LangChain 消息对象。

    LangChain 的消息类（HumanMessage / AIMessage / SystemMessage / ToolMessage）
    是模型无关的统一格式。无论底层是 DeepSeek 还是别的模型，
    业务层都只和这些类打交道。
    """
    return [_ROLE_TO_MESSAGE[item.role](content=item.content) for item in messages]


def _build_agent(page_url: str | None, page_title: str | None) -> Any:
    """创建一个带工具和页面上下文的 agent。

    返回值是 LangGraph 编译后的图（CompiledStateGraph）。
    这里不写具体类型，是为了避免绑定 langgraph 的内部路径；
    对业务而言它就是“可以 astream 的对象”。

    参数说明：
    - `model`：ChatDeepSeek 实例。create_agent 内部会调用 `bind_tools`，
      让模型知道有哪些工具可用。
    - `tools`：BaseTool 列表。空列表时 agent 退化成普通聊天模型。
    - `system_prompt`：字符串会自动被包装成 SystemMessage，
      放在每次模型调用的最前面。

    为什么每次请求都新建 agent：
    页面标题/地址是动态的，而 system_prompt 在建图时就固定了。
    建图本身很轻（只是组装状态图，不会新建 HTTP 连接，模型实例仍是复用的）。
    如果以后追求极致性能，可以用 LangGraph 的 middleware 或
    `context_schema` 把页面信息改成运行时参数。
    """
    return create_agent(
        model=build_chat_model(),
        tools=TOOLS,
        system_prompt=build_system_prompt(page_url, page_title),
    )


def _extract_text(content: Any) -> str:
    """从 message.content 中取出纯文本。

    不同模型的 content 可能是两种形态：
    1. 字符串：DeepSeek / OpenAI 普通对话就是这样；
    2. 内容块列表：例如 [{"type": "text", "text": "..."}]，
       多模态模型（图文混合）会返回这种结构。

    统一处理成一个字符串，前端就不用关心模型差异了。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


async def stream_reply(
    messages: list[ChatMessage],
    page_url: str | None = None,
    page_title: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """核心入口：产出一系列前端可直接消费的事件字典。

    事件格式（与前端 frontend/src/api/chat.ts 约定一致）：
    - {"type": "delta", "content": "..."}  一段增量文字
    - {"type": "done"}                     本轮结束
    - {"type": "error", "message": "..."}  出错时的友好提示

    FastAPI 的 SSE 路由（app/api/chat.py）只负责把这些字典序列化成
    `data: {...}`，不掺和任何 AI 逻辑。
    """
    settings = get_settings()

    # ------------------------------------------------------------------
    # 1. mock 模式：没有 API Key 时，用 LangChain 的假模型流式输出，
    #    这样前端、SSE、打字机效果都能离线联调。
    # ------------------------------------------------------------------
    if settings.mock_enabled:
        async for event in _mock_reply(messages):
            yield event
        return

    # ------------------------------------------------------------------
    # 2. 真实模式：只保留最近 N 条历史，避免上下文无限增长。
    # ------------------------------------------------------------------
    history = messages[-settings.agent_max_history :]
    agent = _build_agent(page_url=page_url, page_title=page_title)

    try:
        # astream 的第一个参数是图的输入 state。
        # create_agent 的约定是 {"messages": [...]}。
        async for chunk, _metadata in agent.astream(
            {"messages": _to_langchain_messages(history)},
            stream_mode="messages",
        ):
            # 只把模型生成的 AI 消息片段交给前端。
            # 工具调用片段的内容为空，会在 _extract_text 后自然被过滤掉。
            if not isinstance(chunk, AIMessageChunk):
                continue
            text = _extract_text(chunk.content)
            if text:
                yield {"type": "delta", "content": text}

        yield {"type": "done"}

    except asyncio.CancelledError:
        # 客户端断开连接（关闭页面/取消请求）时，FastAPI 会取消这个协程。
        # 必须原样抛出，让框架完成清理，不要吞掉。
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成前端可读的错误
        # 这里故意不把异常详情返回给浏览器（可能包含内部路径/请求信息），
        # 只把类型名告诉用户，完整堆栈留在服务端日志里。
        logger.exception("LangChain agent 执行失败")
        yield {
            "type": "error",
            "message": f"AI 服务暂时不可用（{type(exc).__name__}），请稍后再试。",
        }


async def _mock_reply(messages: list[ChatMessage]) -> AsyncIterator[dict[str, Any]]:
    """没有 API Key 时的假回复。

    这里使用 LangChain 的 GenericFakeChatModel，而不是手写字符串循环：
    - 它实现了和真实模型相同的 BaseChatModel 接口；
    - 可以用它学习 `astream` 的返回结构；
    - 单元测试里也常用来替代真实 API，稳定且不花钱。

    注意：mock 模式不经过 create_agent（假模型不支持工具调用），
    只演示“模型流式输出”这一层。
    """
    last = next((item.content for item in reversed(messages) if item.role == "user"), "")
    reply = (
        f"我听到你说「{last[:40]}」啦～ 现在是 mock 模式，"
        "在后端 .env 里填上 DEEPSEEK_API_KEY，我就会真正回答你了。"
    )

    # messages 参数要求是一个迭代器，每个元素是一条完整的 AIMessage；
    # 假模型会把它切成小块，模拟真实模型的流式输出。
    fake_model = GenericFakeChatModel(messages=iter([AIMessage(content=reply)]))

    async for chunk in fake_model.astream("mock"):
        text = _extract_text(chunk.content)
        if text:
            yield {"type": "delta", "content": text}
            # 稍微停顿，让前端看起来像真实打字；生产代码里不要加这种人为延迟。
            await asyncio.sleep(0.02)

    yield {"type": "done"}
