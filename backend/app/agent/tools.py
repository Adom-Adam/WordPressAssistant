"""Agent 工具（Tools）。

工具 = 模型可以主动调用的普通 Python 函数。LangChain 通过 `@tool` 装饰器把函数
包装成 BaseTool 对象，并自动生成“工具说明书”：

1. **函数名**      → 工具名（模型看到的名字）；
2. **函数 docstring** → 工具描述（模型根据它判断什么时候该调用）；
3. **类型注解**    → 参数 schema（模型据此生成 JSON 参数）；
4. **返回值**      → 会被包装成 ToolMessage 交还给模型，模型再基于结果继续回答。

写好 docstring 是工具能不能被正确调用的关键：
- 第一行写“这个工具做什么”；
- 再写“什么时候用 / 什么时候不要用”；
- Args 里逐个解释参数。
这些文字是给模型看的，不是给人看的。

本项目目前有三个工具：
- `get_current_datetime`：回答“现在几点”；
- `search_site`：检索本地知识库（文章正文 + 图片描述）；
- `navigate_to_article`：生成一个前端跳转按钮。

====================================================================
重点：response_format="content_and_artifact"
====================================================================

普通工具的返回值会同时给模型和前端看。但检索结果里有很多字段（标题、URL、
摘要、图片地址、分数）——模型其实只需要读其中一部分，而前端需要结构化的数据。

LangChain 允许工具返回一个二元组：
    (content, artifact)

- content：字符串，放进 ToolMessage 的正文，**模型**能看到；
- artifact：任意可序列化对象，**模型看不到**，但会保存在 ToolMessage.artifact 里，
  我们可以在 core.py 的流式循环里把它取出来，转成 SSE 的 citations/action 事件。

这样模型负责“组织语言”，前端负责“显示来源卡片”，两边互不干扰。
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

from app.config import get_settings
from app.kb.search import format_results, list_recent
from app.kb.search import search_site as kb_search


@tool
def get_current_datetime(timezone_name: str = "Asia/Shanghai") -> str:
    """获取指定时区的当前日期和时间。

    什么时候用：用户问“现在几点”“今天几号”“今天星期几”等与当前时间有关的问题。
    什么时候不用：历史时间、日期计算、倒计时——这些应该直接根据用户给的日期推理。

    Args:
        timezone_name: IANA 时区名称，例如 "Asia/Shanghai"、"UTC"、"America/New_York"。
    """
    # 工具内部一定要“自己兜住异常”，不要让异常抛给 agent。
    # 因为工具报错会让整个图执行中断，前端只能看到“服务不可用”；
    # 返回一段错误说明，模型反而能据此向用户解释或换一种方式重试。
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return f"未知时区 {timezone_name!r}，请使用 IANA 时区名，例如 Asia/Shanghai 或 UTC。"

    now = datetime.now(timezone)
    weekday = "一二三四五六日"[now.weekday()]
    return f"{now:%Y-%m-%d %H:%M:%S}（星期{weekday}，{timezone_name}）"


@tool(response_format="content_and_artifact")
def search_site(query: str, limit: int = 4) -> tuple[str, list[dict]]:
    """检索这个博客里已经发布的文章和文章图片。

    什么时候用：
    - 用户问“站内有没有写过……”“帮我找一篇关于……的文章”；
    - 用户问某个技术概念，而博客里可能有相关学习笔记；
    - 用户想找某张图、某个示意图、某段截图。
    什么时候不用：闲聊、问时间、与博客内容无关的通用问题。

    返回内容包含文章标题、URL 和命中摘要，可以直接据此回答。
    来源卡片由系统自动附上，**不要在回答里自己编链接**。
    如果没有结果，就如实告诉用户站内没有找到。

    如果用户只是问“这里有什么”“最近写了什么”这类宽泛问题，没有具体关键词，
    可以把 query 传成空字符串或“最近”，工具会按时间返回最近的文章目录。

    Args:
        query: 检索关键词。用用户问题里的核心技术词，不要把整句口语原样塞进来。
        limit: 最多返回几篇文章，默认 4，范围 1~10。
    """
    # 参数来自模型，可能为空、可能超出范围，工具内部要先做一次兜底。
    query = (query or "").strip()
    limit = max(1, min(int(limit or 4), 10))

    # 宽泛问题没有关键词可检索，直接列最近文章，避免模型凭印象编分类。
    # 和 kb.search 一样至少取四篇：模型有时会传 limit=1，
    # 只看到一篇就容易误判“馆里没几篇”。
    if not query:
        results = list_recent(max(limit, 4))
        return (format_results(results), results)

    try:
        results = kb_search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 工具不应把异常抛给 agent
        # 数据库损坏、索引未建立等都属于“服务端问题”，给模型一句可读的说明。
        return (f"知识库检索失败（{type(exc).__name__}），请稍后再试。", [])

    # format_results 只保留模型需要读的文字；
    # results 原样作为 artifact，供 core.py 转成 citations 事件。
    return (format_results(results), results)


@tool(response_format="content_and_artifact")
def navigate_to_article(url: str, label: str = "") -> tuple[str, dict | None]:
    """在聊天窗口里生成一个“打开站内文章”的按钮。

    什么时候用：用户明确说“带我去”“打开这篇”“跳转过去”时。
    什么时候不用：只是询问内容、还没决定要看时，不要主动塞跳转按钮。

    重要：url 必须来自 search_site 的检索结果，**不要自己拼或猜测**。
    只允许本站地址；外部链接会被拒绝。

    Args:
        url: 文章完整地址，例如 https://374969782.xyz/archives/152/，
             也可以传站内路径 /archives/152/。
        label: 按钮文字，例如“打开 DDS 那篇”。不传就用默认文字。
    """
    settings = get_settings()
    site = settings.wp_site_url.rstrip("/")
    url = (url or "").strip()
    if not url:
        return ("没有提供要跳转的地址。", None)

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        # 完整 URL：只允许和 WP_SITE_URL 同一个站点。
        site_parsed = urlparse(site)
        if parsed.netloc.lower() != site_parsed.netloc.lower():
            return (f"拒绝跳转：{url} 不是本站地址。", None)
        absolute = url
    else:
        # 站内相对路径：拼成绝对地址，前端在任何页面都能正确打开。
        absolute = urljoin(site + "/", url.lstrip("/"))

    label = (label or "").strip() or "带我去看看"
    artifact = {"type": "navigate", "url": absolute, "label": label}
    return (f"已经为「{label}」准备好跳转按钮：{absolute}", artifact)


# ----------------------------------------------------------------------
# 工具注册表：create_agent 需要的是一个列表。
# 以后新增工具时，只要在这里 append 即可，模型会自动看到它的描述。
# ----------------------------------------------------------------------
TOOLS = [get_current_datetime, search_site, navigate_to_article]
