"""请求/响应的数据模型。

Pydantic 的 BaseModel 会在请求进入业务代码之前完成校验和类型转换：
- 字段缺失、类型不对，FastAPI 会自动返回 422 和清晰的错误信息；
- `Field(min_length=...)` / `max_length=...` 用来限制长度，防止超大请求拖垮模型调用；
- `Literal[...]` 限定枚举值，客户端不能伪造 `system` / `tool` 角色，
  这也是一种提示词注入防护。
"""

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """对话中的一条消息。"""

    # 只允许前端传 user / assistant 两种角色。
    # system 由后端根据人设和页面上下文生成；tool 由 LangChain 内部管理。
    role: Literal["user", "assistant"]

    # 单条消息最多 8000 字符。max_length 在 Pydantic v2 中作用于字符串长度。
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    """POST /api/chat 的请求体。"""

    # 一次最多 40 条，后端还会按 agent_max_history 再截取最近 N 条。
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)

    # 当前页面信息：让助手知道用户正在看哪篇文章。
    # 不传也完全没问题，属于可选的增强上下文。
    page_url: str | None = Field(default=None, max_length=2048)
    page_title: str | None = Field(default=None, max_length=512)


class Citation(BaseModel):
    """知识库引用的结构。

    这是 `citations` SSE 事件里每一项的形状：
    - title/url 用来渲染来源卡片、点击跳转；
    - excerpt 是命中位置的摘要；
    - image_url 是命中的图片地址，前端把它显示成缩略图；
    - kind 区分“正文命中”和“图片描述命中”。

    字段使用 snake_case，和 `search_site` 工具返回的字典保持一致，
    避免在 agent 层来回做无意义的命名转换。
    """

    title: str
    url: str
    post_id: int | None = None
    excerpt: str | None = None
    image_url: str | None = None
    kind: Literal["article", "image"] | None = None
