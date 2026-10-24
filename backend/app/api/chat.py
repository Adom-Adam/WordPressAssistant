"""对话接口：把内部事件字典转换成 SSE 流。

SSE（Server-Sent Events）是一种“服务器持续往浏览器推文本”的 HTTP 协议。
它的报文格式非常简单，每个事件就是几行文本，以空行结束：

    data: {"type": "delta", "content": "你"}

    data: {"type": "delta", "content": "好"}

    data: {"type": "done"}

前端用 fetch + ReadableStream 读取，按空行切分，再 JSON.parse。

为什么不用 WebSocket：
- 对话是单向流（服务器 → 浏览器），SSE 足够；
- SSE 走普通 HTTP/HTTPS，Nginx 反向代理配置简单；
- 浏览器断线重连、代理穿透都比 WebSocket 省事。

`EventSourceResponse` 来自 sse-starlette，它替我们处理了：
- 正确的 Content-Type: text/event-stream；
- 客户端断开时取消生成器（避免协程泄漏）；
- `ping=15`：每 15 秒发一个注释行作为心跳，防止 Nginx/负载均衡
  把长时间没有数据的连接当成“僵死连接”掐掉。
"""

import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.agent.core import stream_reply
from app.schemas import ChatRequest

router = APIRouter()


@router.post("/chat")
async def chat(request: ChatRequest) -> EventSourceResponse:
    """接收对话请求，返回流式响应。

    这个函数本身**不会等待 AI 回复生成完**，它只做两件事：

    1. 创建一个异步生成器 `event_stream`；
    2. 立刻返回 `EventSourceResponse`（响应头先发给浏览器）。

    真正的“边生成边发送”由 `EventSourceResponse` 驱动：它不断向
    `event_stream` 索取下一项，每拿到一项就写成一个 SSE 事件并 flush 出去。
    """

    # ------------------------------------------------------------------
    # 为什么要在端点里再定义一个 async 函数？
    #
    # 1. `async def` + `yield` = **异步生成器函数**。
    #    注意：调用 `event_stream()` 时，函数体并不会立刻执行，
    #    只会创建一个“异步生成器对象”；真正开始执行，是在
    #    `EventSourceResponse` 迭代它的时候。这正是“惰性生产”的关键。
    #
    # 2. 不能把 `yield` 直接写在 `chat` 里：
    #    路径函数一旦包含 `yield`，它自己就变成了异步生成器函数，
    #    而 FastAPI 要求路径函数返回 Response（或可序列化数据），
    #    不会自动把生成器包装成流式响应。所以必须分成两层：
    #    “外层立刻返回 Response，内层按需产出数据”。
    #
    # 3. 为什么用内层函数（闭包）：
    #    可以直接读取外层的 `request`，不必再把 messages / page_url
    #    等参数重新传一遍。如果以后要单独测试或复用，也可以把它提到
    #    模块级，改成：
    #        async def _event_stream(request: ChatRequest): ...
    #        return EventSourceResponse(_event_stream(request), ping=15)
    # ------------------------------------------------------------------
    async def event_stream():
        # 这里只做“内部事件 → SSE 数据帧”的转换：
        # - agent 层产出 {"type": "delta", ...} 这种业务事件；
        # - API 层负责把它序列化成传输格式。
        # 两层解耦后，以后换传输方式（WebSocket 等）不用改 AI 代码。
        #
        # ensure_ascii=False 让中文以 UTF-8 原样输出，
        # 否则会变成 你好 这类转义（能工作，但调试时难读）。
        async for event in stream_reply(
            messages=request.messages,
            page_url=request.page_url,
            page_title=request.page_title,
        ):
            yield {"data": json.dumps(event, ensure_ascii=False)}

    # ping=15：每 15 秒发送一个 SSE 注释行作为心跳，
    # 防止 Nginx/负载均衡把长时间没有数据的连接当成僵死连接断开。
    return EventSourceResponse(event_stream(), ping=15)
