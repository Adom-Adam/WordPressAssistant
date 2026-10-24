# 后端学习导读（LangChain + DeepSeek）

这份后端刻意写得“注释比代码多”，目标是让第一次接触 LangChain 的人能顺着文件读下去。

## 文件地图

| 文件 | 先看什么 |
| --- | --- |
| [app/config.py](app/config.py) | pydantic-settings 如何把 `.env` 变成带类型的配置 |
| [app/agent/model.py](app/agent/model.py) | `ChatDeepSeek` 的每个参数；`BaseChatModel` 的 `invoke / stream / astream` |
| [app/agent/prompts.py](app/agent/prompts.py) | System / User / Assistant / Tool 四种消息角色的区别 |
| [app/agent/tools.py](app/agent/tools.py) | `@tool` 装饰器、docstring 为什么是给模型看的、工具异常处理 |
| [app/agent/core.py](app/agent/core.py) | `create_agent` 的 agent 循环、`astream(stream_mode="messages")`、mock 模式 |
| [app/api/chat.py](app/api/chat.py) | SSE 协议与 `EventSourceResponse` |
| [app/schemas.py](app/schemas.py) | Pydantic 校验与提示词注入防护 |
| [app/main.py](app/main.py) | FastAPI 入口、CORS、路由前缀 |

## 一图看懂 Agent 循环

```
用户消息
   │
   ▼
┌─────────┐  模型返回 tool_calls   ┌─────────┐
│  model  │ ─────────────────────▶ │  tools  │
└─────────┘ ◀───────────────────── └─────────┘
   │            工具结果回填
   │ 模型不再调用工具
   ▼
最终回答（流式）
```

`create_agent(model, tools, system_prompt)` 会把这个循环编成一张 LangGraph 状态图。
我们唯一需要消费的是：

```python
async for chunk, metadata in agent.astream(
    {"messages": messages},
    stream_mode="messages",
):
    ...
```

`stream_mode="messages"` 产出的是模型 token 的增量 `AIMessageChunk`。
工具调用参数的 chunk 内容为空，所以只处理非空 `content` 就能得到给用户看的文字。

## 关键概念速查

- **消息角色**：`SystemMessage` 定人设，`HumanMessage` 是用户，`AIMessage` 是模型回复，
  `ToolMessage` 是工具结果（agent 自动管理）。
- **`@tool`**：把普通函数变成模型可调用的工具。函数名是工具名，docstring 是工具说明，
  类型注解自动生成参数 schema。
- **`bind_tools`**：把工具描述告诉模型。`create_agent` 内部会自动做。
- **`astream`**：异步流式输出。FastAPI 里必须用异步版本，否则会阻塞事件循环。
- **mock 模式**：没有 Key 时用 `GenericFakeChatModel` 模拟流式输出，方便离线联调。

## 怎么加一个新工具

在 [app/agent/tools.py](app/agent/tools.py) 里写函数，然后加进 `TOOLS` 列表：

```python
@tool
async def search_site(query: str, limit: int = 5) -> list[dict]:
    """检索站内文章。

    什么时候用：用户想找博客里写过的某篇文章时。

    Args:
        query: 检索关键词。
        limit: 返回条数上限。
    """
    ...
```

注意事项：

1. docstring 第一行要能独立说明用途；
2. 参数必须有类型注解和说明；
3. 工具内部自己兜住异常，返回错误文字，不要让异常冒到 agent；
4. 需要查数据库/发 HTTP 请求时用 `async def`，不要阻塞事件循环。

## 后续知识库接入点

- 文章同步：插件在 `save_post` 时调用后端 `/api/kb/sync`；或后端定时扫 WordPress REST API。
- 索引：SQLite FTS5（trigram 对中文友好），每条 chunk 带 `post_id/title/url`。
- 引用事件：监听 LangGraph 的 `stream_mode="updates"`，从 `ToolMessage` 提取来源，
  输出 `{"type": "citations", ...}`；前端已经支持渲染。
- 跳转事件：工具返回导航意图，后端输出 `{"type": "action", ...}`；前端渲染按钮，不自动跳转。
