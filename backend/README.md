# 后端学习导读（LangChain + DeepSeek）

这份后端刻意写得“注释比代码多”，目标是让第一次接触 LangChain 的人能顺着文件读下去。

## 文件地图

| 文件 | 先看什么 |
| --- | --- |
| [app/config.py](app/config.py) | pydantic-settings 如何把 `.env` 变成带类型的配置 |
| [app/agent/model.py](app/agent/model.py) | `ChatDeepSeek` 的每个参数；`BaseChatModel` 的 `invoke / stream / astream` |
| [app/agent/prompts.py](app/agent/prompts.py) | System / User / Assistant / Tool 四种消息角色的区别 |
| [app/agent/tools.py](app/agent/tools.py) | `@tool` 装饰器、`content_and_artifact`、工具异常处理 |
| [app/agent/core.py](app/agent/core.py) | `create_agent` 循环、`astream(stream_mode=[...])`、mock 模式 |
| [app/auth/jwt.py](app/auth/jwt.py) | JWT 三段结构、HS256 验签、PyJWT 的 `decode` 参数 |
| [app/api/deps.py](app/api/deps.py) | FastAPI 依赖注入、`HTTPBearer`、401/503 的区别 |
| [app/quota.py](app/quota.py) | SQLite 原子占位、`ON CONFLICT`、并发与退款 |
| [app/agent/limits.py](app/agent/limits.py) | LangChain middleware：模型/工具调用次数上限 |
| [app/api/chat.py](app/api/chat.py) | SSE 协议、`EventSourceResponse`、认证/额度/退款顺序 |
| [app/schemas.py](app/schemas.py) | Pydantic 校验与提示词注入防护 |
| [app/main.py](app/main.py) | FastAPI 入口、CORS、启动检查 |
| [tests/](tests/) | JWT、额度并发、agent 防循环、接口 401/429 |
| [app/kb/sync.py](app/kb/sync.py) | 从 WordPress REST 拉文章、切块、写入索引（命令行入口） |
| [app/kb/text.py](app/kb/text.py) | HTML 清洗、图片提取、jieba 分词、切块、摘要 |
| [app/kb/db.py](app/kb/db.py) | SQLite 连接与 FTS5 表结构、图片描述缓存 |
| [app/kb/vision.py](app/kb/vision.py) | 用 `deepseek-flash` 生成图片描述/OCR/关键词 |
| [app/kb/search.py](app/kb/search.py) | AND→OR→LIKE 检索、按文章去重、标题加成重排 |

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
async for mode, payload in agent.astream(
    {"messages": messages},
    stream_mode=["messages", "updates"],
):
    if mode == "messages":
        chunk, metadata = payload   # AIMessageChunk 增量
    else:
        ...                         # ToolMessage，取 artifact
```

`messages` 模式通常产出模型 token 的增量 `AIMessageChunk`；
middleware 在超限收尾时会注入一条完整的 `AIMessage`，两种都要接收。
工具调用参数的 chunk 内容为空，所以只处理非空 `content` 就能得到给用户看的文字。

`updates` 模式在工具执行完后给出 `ToolMessage`。检索工具的 artifact 里是结构化结果，
`core.py` 把它转成 `citations` 事件；导航工具的 artifact 转成 `action` 事件。
artifact 模型看不到，所以链接不会被模型改写或编造。

## 关键概念速查

- **消息角色**：`SystemMessage` 定人设，`HumanMessage` 是用户，`AIMessage` 是模型回复，
  `ToolMessage` 是工具结果（agent 自动管理）。
- **`@tool`**：把普通函数变成模型可调用的工具。函数名是工具名，docstring 是工具说明，
  类型注解自动生成参数 schema。
- **`bind_tools`**：把工具描述告诉模型。`create_agent` 内部会自动做。
- **`astream`**：异步流式输出。FastAPI 里必须用异步版本，否则会阻塞事件循环。
- **mock 模式**：没有 Key 时用 `GenericFakeChatModel` 模拟流式输出，方便离线联调。

## 认证与额度是怎么串起来的

一次聊天请求的完整顺序：

```text
POST /api/chat
  ├─ Depends(require_user)
  │    └─ HTTPBearer 取 token → verify_token() 验签
  │         └─ 失败：401；服务端没配密钥：503
  ├─ QuotaStore.reserve(user_id, DAILY_QUOTA)
  │    ├─ 管理员：直接放行，不计数
  │    ├─ 普通用户：一条原子 SQL 占位
  │    └─ 用满：429 + Retry-After
  ├─ asyncio.timeout(AGENT_MAX_SECONDS) 包住流式生成
  │    └─ agent 内部还有模型/工具调用次数上限
  └─ finally：如果“一个字都没输出”，退回额度
```

值得留意的三个设计：

1. **认证必须发生在 SSE 开始之前**。响应头一旦发出去，就改不了状态码了；
2. **`sub` 是唯一可信的用户标识**。昵称、`admin` 都来自签名 payload，但权限只看
   `sub` 和布尔 `admin`；
3. **额度按“请求”算，不按工具次数算**。内部循环再多次也只是 1 条，但 token 成本
   由 `AGENT_MAX_*` 兜底。

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
4. 耗时的网络请求用 `async def`，不要阻塞事件循环；很快的本地 SQLite 查询可以直接用
   普通 `def`，LangChain 会把同步工具放到线程池里执行。

## 跑测试

```bash
cd backend
uv sync                 # 首次会安装 dev 依赖（pytest、httpx）
uv run pytest -q
```

测试不调用真实模型，也不花钱：

- `test_jwt.py`：正常、过期、错密钥、错 `aud`、`alg=none`、篡改；
- `test_quota.py`：额度、跨用户、跨天、退款、40 个并发只放行 5 个；
- `test_chat_api.py`：401 / 200 / 429 / 503 / 失败退款；
- `test_agent_limits.py`：用假模型模拟“无限调工具”，验证保险丝会断、提示是中文。

## 知识库：已实现的部分

- **同步**：`uv run python -m app.kb.sync` 从 WordPress REST 拉文章，
  清洗、切块、分词后写入 `data/kb.sqlite3`（相对 `backend/`）。
- **索引**：SQLite FTS5 + jieba。中文先分词再写入 `tokens` 列；
  查询时对问题做同样处理。标题命中会额外加分。
- **图片**：同步时调用 `deepseek-flash` 生成描述/OCR/关键词，写成 `kind='image'` 的块，
  所以图片内容也能被文字检索到。
- **引用**：`search_site` 用 `content_and_artifact` 返回结果；
  `core.py` 从 `ToolMessage.artifact` 取出结构化数据，发 `citations` 事件。
- **跳转**：`navigate_to_article` 校验同站 URL，发 `action` 事件；
  前端渲染按钮，点击后新标签页打开。

完整原理、命令、配置与排错见 [docs/knowledge-base.md](../docs/knowledge-base.md)。
