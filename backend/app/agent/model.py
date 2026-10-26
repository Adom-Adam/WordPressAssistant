"""LangChain 聊天模型的创建。

LangChain 的核心抽象之一是 **BaseChatModel**（聊天模型）：
不同厂商的模型（OpenAI、DeepSeek、Anthropic、本地 Ollama……）都实现同一套接口，
业务代码只依赖这层接口，就能方便地换模型。

ChatDeepSeek 是 LangChain 官方提供的 DeepSeek 集成（独立的 pip 包
`langchain-deepseek`）。它继承自 ChatOpenAI 的实现，但自动处理了 DeepSeek
的 base_url、鉴权方式和返回值格式。

常用方法（后面在 core.py 里会用到）：
- `model.invoke(messages)`        → 同步、一次性返回 AIMessage
- `model.stream(messages)`        → 同步、逐块返回 AIMessageChunk（本项目的流式实现基础）
- `model.ainvoke / astream`       → 上面两个的异步版本（FastAPI 里必须用异步，避免阻塞事件循环）
- `model.bind_tools([...])`       → 把工具描述绑定给模型；create_agent 内部会自动调用
"""

from functools import lru_cache

from langchain_deepseek import ChatDeepSeek

from app.config import get_settings


@lru_cache(maxsize=8)
def build_chat_model(model_name: str | None = None) -> ChatDeepSeek:
    """创建一个可复用的 DeepSeek 聊天模型实例。

    参数 model_name 来自运行时设置（管理员可在 WP 后台修改）。
    用 lru_cache(maxsize=8) 按模型名分别缓存：同一个模型只创建一次，
    切换模型时也不会复用旧实例。传 None 时使用 .env 里的默认模型。

    参数说明（其余参数保持默认即可）：

    - model:        模型名称。`deepseek-flash` 支持对话、工具调用和图片理解。
    - api_key:      DeepSeek 控制台创建的 Key。这里从 Settings 读，
                    避免把 Key 硬编码在代码里。
    - base_url:     DeepSeek 的 OpenAI 兼容接口地址。
    - temperature:  采样温度，0 更稳定，1 更发散。助手用 0.7 比较自然。
    - max_tokens:   单次回复的最大 token 数，防止模型输出过长。
    - timeout:      单次请求超时，避免请求一直挂起。
    - max_retries:  遇到网络抖动/限流时，LangChain 内部自动重试的次数。
    - extra_body:   传给底层 HTTP 请求的额外 JSON 字段。

    DeepSeek V4.1 默认开启“思考模式”。如果不关掉，模型会先花很多 token
    生成思维链，再输出正文；对闲聊和工具调用来说既慢又贵，所以这里通过
    `extra_body={"thinking": {"type": "disabled"}}` 显式关闭。
    这个字段不属于 OpenAI 标准参数，因此必须放在 extra_body 里透传。

    为什么要 `lru_cache`：
    模型实例内部持有 HTTP 客户端，创建成本较高；而它是无状态的，
    可以安全地在多个请求之间复用。缓存键包含模型名，
    所以管理员切换模型后会得到一个新实例，不会误用旧配置。
    """

    settings = get_settings()

    # 先收集通用参数，再按配置决定是否加入 DeepSeek 特有的“关闭思考”字段。
    # 这样以后如果要做复杂推理，只要把 DEEPSEEK_DISABLE_THINKING 设为 false 即可。
    kwargs: dict[str, object] = {
        "model": model_name or settings.deepseek_model,
        "api_key": settings.deepseek_api_key,
        "base_url": settings.deepseek_base_url,
        "temperature": 0.7,
        "max_tokens": 800,
        "timeout": settings.request_timeout,
        "max_retries": 2,
    }
    if settings.deepseek_disable_thinking:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    return ChatDeepSeek(**kwargs)  # type: ignore[arg-type]
