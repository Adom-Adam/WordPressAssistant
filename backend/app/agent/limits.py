"""Agent 的“保险丝”：限制模型调用次数和工具调用次数。

====================================================================
为什么需要这一层
====================================================================

LangChain 的 agent 是一个循环：

    模型思考 → 要调用工具 → 工具返回 → 模型再思考 → ……

正常问答两三步就结束，但模型偶尔会陷入“再查一次”的循环。
如果没有上限，一次请求可能烧掉大量 token，或者一直不结束。

LangChain 1.x 提供了 `ModelCallLimitMiddleware` 和
`ToolCallLimitMiddleware`，我们不用改 agent 内部代码，只要把
middleware（中间件）传给 `create_agent` 即可。

====================================================================
为什么不直接用它自带的提示
====================================================================

内置的 `exit_behavior="end"` 会生成这样一句话并直接展示给用户：

    Model call limits exceeded: run limit (6/6)

这是给开发者看的技术文案，不适合博客读者。所以这里继承官方中间件，
只替换“最终那条 AI 消息”的内容，其余计数、跳转逻辑完全复用。
子类不复制任何内部实现，官方升级时也不容易坏。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.messages import AIMessage

# 用户看到的话。尽量温和、给出下一步，而不是只抛一个错误。
MODEL_LIMIT_MESSAGE = (
    "这轮对话我想得有点久，先在这里停一下啦～"
    "可以换个更具体的问题再问我一次。"
)
TOOL_LIMIT_MESSAGE = (
    "这轮对话里查资料次数有点多，我先停在这里啦～"
    "可以换个更具体的问题，或者直接告诉我想找什么。"
)


class FriendlyModelCallLimitMiddleware(ModelCallLimitMiddleware):
    """限制“模型被调用几次”，超限时替换成中文提示。"""

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """在每次模型调用前检查次数。

        `super().before_model()` 在没超限时返回 None；
        超限时返回一个带 `jump_to: "end"` 的字典，里面已经放好了
        LangChain 生成的英文 AIMessage。我们只把那条消息换掉。
        """
        result = super().before_model(state, runtime)
        if result and result.get("jump_to") == "end":
            result["messages"] = [AIMessage(content=MODEL_LIMIT_MESSAGE)]
        return result

    # 官方基类的 abefore_model 会调用 self.before_model，
    # 所以异步路径自动复用上面的替换逻辑，不需要再写一遍。


class FriendlyToolCallLimitMiddleware(ToolCallLimitMiddleware):
    """限制“工具被执行几次”，超限时替换成中文提示。"""

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """模型提出工具调用后，检查是否会超过次数上限。

        超限时，官方实现会返回：
        - 若干条 ToolMessage（告诉模型哪些调用被拦下了）；
        - 一条 AIMessage（真正显示给用户的技术提示）；
        - jump_to: "end"（立刻结束循环）。

        我们保留所有 ToolMessage，只把 AIMessage 换成中文。
        ToolMessage 是给模型看的，不会出现在聊天窗口里。
        """
        result = super().after_model(state, runtime)
        if result and result.get("jump_to") == "end":
            result["messages"] = [
                AIMessage(content=TOOL_LIMIT_MESSAGE)
                if isinstance(message, AIMessage)
                else message
                for message in result.get("messages", [])
            ]
        return result

    # 同样，基类的 aafter_model 会调用 self.after_model。
