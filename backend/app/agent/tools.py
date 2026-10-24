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

本项目当前只放了一个演示工具，用来验证 agent 的工具调用链路。
后续知识库功能会在这里新增：
- `search_site(query)`  → 检索站内文章，返回 title/url/post_id
- `navigate(url)`       → 让前端渲染跳转按钮
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool


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


# ----------------------------------------------------------------------
# 工具注册表：create_agent 需要的是一个列表。
# 以后新增工具时，只要在这里 append 即可，模型会自动看到它的描述。
# ----------------------------------------------------------------------
TOOLS = [get_current_datetime]


# ----------------------------------------------------------------------
# 后续工具的写法示例（现在还没有知识库，所以先注释掉）：
#
# @tool
# async def search_site(query: str, limit: int = 5) -> list[dict]:
#     """检索站内文章。
#
#     什么时候用：用户询问博客里是否写过某个主题、想找某篇文章时。
#
#     Args:
#         query: 检索关键词或自然语言问题。
#         limit: 最多返回几条结果。
#     """
#     # 异步工具：适合查数据库、调 HTTP 接口，不会阻塞 FastAPI 的事件循环。
#     # 返回 list[dict] 时，LangChain 会自动序列化后放进 ToolMessage。
#     return await knowledge_base.search(query, limit=limit)
#
# @tool
# def navigate_to_article(url: str, label: str = "") -> str:
#     """生成一个“跳转到站内文章”的按钮。
#
#     Args:
#         url: 站内相对路径，例如 /archives/42/。
#         label: 按钮文字。
#     """
#     # 工具本身不跳转，只是把动作通过事件发给前端，由用户点击确认。
#     return f"已准备跳转：{label or url} -> {url}"
# ----------------------------------------------------------------------
