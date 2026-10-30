# 后端学习导读（LangChain + DeepSeek）

这份后端刻意写得“注释比代码多”，目标是让第一次接触 LangChain 的人能顺着文件读下去。

## 文件地图

| 文件 | 先看什么 |
| --- | --- |
| [app/config.py](app/config.py) | pydantic-settings 如何把 `.env` 变成带类型的配置 |
| [app/agent/model.py](app/agent/model.py) | `ChatDeepSeek` 的每个参数；`BaseChatModel` 的 `invoke / stream / astream` |
| [app/agent/prompts.py](app/agent/prompts.py) | 系统提示词的兼容入口（正文在 `data/personas/`，不进 Git） |
| [app/persona/](app/persona/) | 人设版本模型、文件加载、分层组装 |
| [app/agent/tools.py](app/agent/tools.py) | `@tool` 装饰器、`content_and_artifact`、工具异常处理 |
| [app/agent/core.py](app/agent/core.py) | `create_agent` 循环、`astream(stream_mode=[...])`、mock 模式 |
| [app/auth/jwt.py](app/auth/jwt.py) | JWT 三段结构、HS256 验签、PyJWT 的 `decode` 参数 |
| [app/api/deps.py](app/api/deps.py) | FastAPI 依赖注入、`HTTPBearer`、401/403/503 的区别 |
| [app/quota.py](app/quota.py) | SQLite 原子占位、`ON CONFLICT`、个人/全站额度与退款 |
| [app/runtime_settings.py](app/runtime_settings.py) | 后台可改参数的校验、原子写 JSON、损坏回退 |
| [app/agent/limits.py](app/agent/limits.py) | LangChain middleware：模型/工具调用次数上限 |
| [app/api/chat.py](app/api/chat.py) | SSE 协议、`EventSourceResponse`、认证/额度/退款顺序 |
| [app/api/quota.py](app/api/quota.py) | `GET /api/quota`：不消耗次数的额度查询 |
| [app/api/admin.py](app/api/admin.py) | `GET/PUT /api/admin/settings`：管理员 JWT + 运行时参数 |
| [app/channels/qq/](app/channels/qq/) | OneBot 11/NapCat 薄适配器：事件解析、触发、去重、短期历史 |
| [app/api/qq.py](app/api/qq.py) | `POST /api/channels/qq/events`：接收 NapCat 回调，token 校验 |
| [app/schemas.py](app/schemas.py) | Pydantic 校验与提示词注入防护 |
| [app/main.py](app/main.py) | FastAPI 入口、CORS、启动检查 |
| [scripts/mock_onebot.py](scripts/mock_onebot.py) | 本地假 NapCat：接收 `send_group_msg` 并落盘 |
| [scripts/qq_simulate.py](scripts/qq_simulate.py) | 伪造一条群消息发给本地后端 |
| [scripts/qq_probe.py](scripts/qq_probe.py) | 在服务器上探测 NapCat 的 `get_status` / `get_login_info` |
| [tests/](tests/) | JWT、额度并发、agent 防循环、接口 401/429、QQ 适配器 |
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
  ├─ QuotaStore.reserve(user_id, 单人额度, global_limit=全站额度)
  │    ├─ 管理员：直接放行，不计数，也不占全站额度
  │    ├─ 普通用户：个人 + 全站在同一个事务里原子占位
  │    ├─ 个人用满：429“今天的聊天额度已经用完啦”
  │    └─ 全站用满：429“今天全站的聊天额度已经用完了”
  ├─ asyncio.timeout(运行时设置的 AGENT_MAX_SECONDS) 包住流式生成
  │    └─ agent 内部还有模型/工具调用次数上限
  └─ finally：如果“一个字都没输出”，个人和全站额度一起退回
```

额度、模型名、调用上限和超时都来自 `RuntimeSettingsStore`：
`.env` 提供默认值，WordPress 后台保存后写入 `data/runtime_settings.json`，**下一次请求立即生效**，
不需要重启。文件损坏时逐字段回退到默认值，不会因为一个坏值整站瘫痪。

值得留意的四个设计：

1. **认证必须发生在 SSE 开始之前**。响应头一旦发出去，就改不了状态码了；
2. **`sub` 是唯一可信的用户标识**。昵称、`admin` 都来自签名 payload，但权限只看
   `sub` 和布尔 `admin`；
3. **额度按“请求”算，不按工具次数算**。内部循环再多次也只是 1 条，但 token 成本
   由 `AGENT_MAX_*` 兜底；
4. **全站额度只统计普通用户**。管理员聊天既不受个人额度限制，也不会挤占普通用户的全站额度。

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
- `test_quota.py`：个人/全站额度、跨用户、跨天、退款、全站满时个人额度回滚、40 个并发只放行 5 个；
- `test_chat_api.py`：401 / 200 / 429（个人与全站）/ 503 / 失败退款 / `GET /quota`；
- `test_runtime_settings.py`：后台参数范围、非法值整批拒绝、损坏文件逐字段回退；
- `test_admin_api.py`：非管理员 403、读取与更新设置；
- `test_prompts.py`：人设分层顺序、频道覆盖、管理员身份、页面上下文清洗；
- `test_persona_store.py`：版本选择、路径越界、文件损坏、内容与路径不一致；
- `test_agent_limits.py`：用假模型模拟“无限调工具”，验证保险丝会断、提示是中文；
- `test_qq_adapter.py`：群白名单、@/前缀触发、重复消息、短期历史、管理员身份、长消息拆条。

## 人设文件放哪里

真实人设正文**不在 Git 里**。后端只保留组装逻辑，正文从
`backend/data/personas/` 读取；根目录 `.gitignore` 已经忽略了整个
`backend/data/`。

第一次运行：

```bash
cd backend
cp -r personas.example data/personas
```

然后编辑 `data/personas/merlin/1.json`，把示例文字换成真实人设。
`data/personas/active.json` 决定当前启用哪个版本：

```json
{ "persona_id": "merlin", "version": 1 }
```

没有 `active.json` 时会加载 `PERSONA_ID`（默认 `merlin`）的最新版本。
每次改人设都建议新增版本号（例如 `2.json`），不要原地改旧文件；
这样日志、回滚和以后的人设评测才能复现。

字段说明见 [personas.example/README.md](personas.example/README.md)。

## 不登录 WordPress，本地预览提示词

改完提示词后，不必先部署才能看真实语气。`scripts/chat_preview.py` 直接调用
`stream_reply()`，跳过 FastAPI 的 JWT 校验和额度扣减，但仍然使用 `.env` 里的
DeepSeek Key 与本地知识库：

```bash
cd backend

# 单轮
uv run python scripts/chat_preview.py "你好呀"

# 连续多轮，会保留上下文
uv run python scripts/chat_preview.py "你好" "这里都有什么" "帮我找一篇讲 DDS 的文章"

# 不传问题进入交互模式；支持 /reset、/admin、/guest、exit
uv run python scripts/chat_preview.py

# 以普通访客身份测试
uv run python scripts/chat_preview.py --guest "你能做什么？"
```

它会真实调用 DeepSeek，产生少量 token 费用；如果 `MOCK_MODE=true` 或没有 Key，
回答来自假模型，测不出人设。脚本会同时打印检索来源和跳转按钮，方便确认工具是否被调用。

## QQ 通道（实验性，默认关闭）

QQ 走 OneBot 11 协议，NapCat 负责登录 QQ 和收发消息。我们不把 NapCat 的代码
嵌入后端，只做一层“薄适配器”：

```
QQ 群
  │  @梅林
  ▼
NapCat ──反向 HTTP──▶ POST /api/channels/qq/events   （app/api/qq.py）
                              │
                              ▼
                    QQAdapter 解析/去重/白名单
                              │
                              ▼
                    stream_reply()（同一套人设、知识库、工具）
                              │
                              ▼
NapCat ◀──正向 HTTP── POST /send_group_msg            （app/channels/qq/client.py）
```

这层只处理**白名单群里的文本消息**，默认拒绝：
- `QQ_ALLOWED_GROUPS` 为空时，任何群都不回；
- 只处理 `message_type=group`，私聊/图片/表情先忽略；
- `message` 需要是 OneBot 的数组格式（NapCat 默认就是）；CQ 码字符串暂不解析；
- 群消息只作为 `role=user` 的内容，不会进入 system prompt，也不会改变管理员身份；
- 同一个 `message_id` 只处理一次，避免 NapCat 重发导致重复回复；
- 短期历史只存在内存里，进程重启就清空；账号绑定和长期记忆还没做。

**回调鉴权**：`QQ_EVENT_TOKEN` 同时支持三种带法——`Authorization: Bearer`、
`X-OneBot-Token`，以及 **NapCat 的 HTTP 客户端实际使用的 `X-Signature: sha1=<hex>`**。
最后一种是 `HMAC-SHA1(key=token, msg=原始请求体)`，NapCat 从不发明文 token，
所以只校验前两种的话，真实消息会全部收到 401。

**注意**：当前 QQ 消息**不经过**网页的每日额度/全站额度，也没有内容审核。
在接入生产前必须补上独立额度和限流，否则白名单群里的用户可以无限调用模型。
这也是它默认关闭的原因。

### 本地没有 NapCat 怎么联调

有两种模式，按需要选一种：

**A. 纯内存 mock（最少依赖）**

`.env` 里：

```dotenv
QQ_ENABLED=true
QQ_ALLOWED_GROUPS=你的测试群号
QQ_TRIGGER=at
QQ_EVENT_TOKEN=dev-token
QQ_MOCK_MODE=true
QQ_MOCK_OUTBOX=data/qq_mock_outbox.jsonl
```

启动后端后，用 `scripts/qq_simulate.py` 伪造事件。适配器不会发 HTTP，
而是把“本来要发的群消息”写进 `data/qq_mock_outbox.jsonl`：

```bash
uv run uvicorn app.main:app --reload --port 18080
# 另一个终端
uv run python scripts/qq_simulate.py --group 你的测试群号 --text "在吗"
cat data/qq_mock_outbox.jsonl
```

**B. 假 NapCat（把 HTTP 这一半也走通）**

保持 `QQ_MOCK_MODE=false`，并让后端指向 `scripts/mock_onebot.py`：

```dotenv
QQ_ONEBOT_BASE_URL=http://127.0.0.1:3000
QQ_ONEBOT_TOKEN=
QQ_MOCK_MODE=false
```

```bash
# 终端 1：后端
uv run uvicorn app.main:app --reload --port 18080
# 终端 2：假 NapCat
uv run python scripts/mock_onebot.py
# 终端 3：伪造群消息
uv run python scripts/qq_simulate.py --group 你的测试群号 --text "在吗"
```

假 NapCat 会在终端打印 `send_group_msg`，并写入
`data/qq_mock_outbox.jsonl`。它不连接 QQ，也不会登录账号。

### 服务器上探测 NapCat

NapCat 已经装好但没启动时，`qq_probe.py` 只能告诉你“连不上”，不会替你启动它。
启动 NapCat 属于服务器运行状态变更，需要先征得同意。启动后，在服务器的
`backend/` 目录执行：

```bash
uv run python scripts/qq_probe.py --base-url http://127.0.0.1:3000
```

它只调用 `get_status` 和 `get_login_info`，默认不发消息。要验证真实发送，
必须同时加 `--send-test <群号> --confirm`，避免误发到群里。

QQ 通道目前**没有**接入生产：还需要 WordPress 侧的“QQ 通道”设置页、
群白名单管理、独立额度、账号绑定和内容审核。默认 `QQ_ENABLED=false`，
不影响现有网页聊天。

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
