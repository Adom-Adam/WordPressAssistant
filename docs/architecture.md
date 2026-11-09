# 技术架构

## 目标

在 WordPress（Argon 主题）博客左下角放一个 Live2D 小助手：

- 形象在最上层，但**不拦截下层页面的点击、滚动、文本选择**；
- 鼠标未悬停时控件半透明，悬停时清晰；
- 支持管理员在后台选择 Live2D 形象、文本对话、语音输入；
- AI 能回答站内文章相关问题，**给出引用来源与跳转按钮**；
- 最终可平滑迁移到云服务器上的现有 WordPress，不破坏原有主题与功能。

## 非目标（当前阶段）

- 不自己做独立的用户系统：注册、登录、找回密码复用 WordPress 用户表（认证与额度接入见下文）；不做多轮长期记忆持久化；
- 不接管 WordPress 的搜索、评论等既有功能；
- 不在浏览器端保存 DeepSeek API Key；
- 不支持 Cubism 2（`.model.json`）；
- 暂不做「上传图片找相似图」（先把文字检索图片做稳）；
- 暂不做 TTS 语音播报与语音打断。

## 三层结构

### 1. WordPress 插件（PHP，薄层）

位置：`wordpress-plugin/wp-live2d-ai/`

职责保持很薄：

1. 在**前台页面** enqueue 构建产物 `assets/app/app.js`、`app.css`、Cubism Core；
2. 通过 `wp_add_inline_script` 注入运行时配置
   （API 地址、资源地址、人设名字、语言、形象尺寸、模型、登录态）；
3. 提供「设置 → Live2D 小助手」页面：显示设置（含人设名字、形象和模型）
   保存到 WordPress options；额度等运行参数由服务器端签 2 分钟管理员 JWT
   后转发给 Python 后端。

它不包含任何 AI 逻辑，也不修改 Argon 主题文件。主题升级、WordPress 升级都不受影响。
可用过滤器：

- `l2d_ai_should_load`：返回 `false` 可在指定页面关闭助手；
- `l2d_ai_frontend_config`：覆盖注入到前端的配置；
- `l2d_ai_api_base`（体现在设置项）也可以直接用过滤器覆盖。

### 2. 前端（Vite + TypeScript + PixiJS）

位置：`frontend/`

- **渲染**：`pixi.js@6.5.10` + `pixi-live2d-display@0.4.0`（Cubism 4 入口，兼容 Cubism 3/4 模型）。
- **Cubism Core**：`public/live2d/core/live2dcubismcore.min.js`，必须在业务脚本之前加载。
- **层级与交互**：根容器 `pointer-events: none`，Canvas 也 `pointer-events: none`；
  只有按钮、菜单、对话面板 `pointer-events: auto`。因此形象覆盖在页面上但不影响下层交互。
- **视线跟随**：监听 `mousemove` 手动调用 `model.focus()`，不依赖 PIXI 的交互系统。
- **控件透明度**：默认 `opacity: .28`，`:hover` / `:focus-within` / 对话打开时变为 `1`；触屏设备默认更高不透明度。
- **选择模型**：构建前扫描 `public/live2d/models/**/*.model3.json` 和
  `public/spine/models/**/*.json`，分别生成 `manifest.json`；
  管理员在插件设置页选择形象引擎和具体模型，插件把 id 注入前端，前台不提供切换入口。
- **Spine**：`@esotericsoftware/spine-player@4.2.120` 在独立的 `.l2d-spine` 容器里
  创建自己的 canvas/WebGL 上下文，只播放骨架自带的循环动画；由于官方 Pixi 运行时
  面向 Pixi 7/8，没有为了 Spine 升级现有的 PixiJS 6。Spine 失败时自动回退 Live2D。
  构建时只复制 `.json/.atlas/图片`，不会把素材目录里的脚本和备份文件发布出去。
- **故障隔离**：Live2D/Spine 管理器都用动态 `import()` 加载。Cubism Core 缺失、
  模型损坏或 Spine 加载失败时，形象层单独降级，聊天面板、语音和来源浮层仍然可用。
- **语音输入**：浏览器 `SpeechRecognition` / `webkitSpeechRecognition`，识别结果填入输入框，用户确认后发送。
- **样式**：全部作用域在 `.l2d-root` 下，使用 CSS 变量（天蓝色系），不会污染主题样式。
- **尺寸**：插件注入 `--l2d-width/--l2d-height`；900px 以下再按视口收缩，
  560px 以下聊天面板才接近全宽，避免在窄桌面窗口挡住正文。
- **额度提示**：用户栏显示“今日剩余 N 条”；打开面板时查询、每次回答后从响应头刷新。
- **代码块**：按 ``` 围栏切成 `<pre><code>`，内容一律 `textContent` 写入，
  不引入 Markdown 解析器，也不执行模型生成的 HTML。

### 3. 后端（FastAPI + LangChain）

位置：`backend/`

- `POST /api/chat`：接收对话历史与当前页面信息，返回 **SSE 流**。
- 对话由 **LangChain agent** 驱动：
  - `ChatDeepSeek`（`langchain-deepseek`）封装 DeepSeek 的 OpenAI 兼容接口；
  - 默认模型 `deepseek-flash`，同时用于聊天和图片理解；
  - 通过 `extra_body={"thinking": {"type": "disabled"}}` 关闭 V4.1 的思考模式，
    降低延迟和 token 消耗，也避免思维链占满 `max_tokens` 导致正文为空；
  - 系统提示词不再写死在代码里，而是从 `data/personas/<id>/<version>.json`
    加载（该目录被 `.gitignore` 忽略）。`app/persona/composer.py` 按
    “核心人设 → 背景 → 规则 → 频道覆盖 → 访客身份 → 页面上下文”组装；
    频道由调用方传入：网页 `web`、QQ 私聊 `qq_private`、QQ 群 `qq_group`，
    没有对应覆盖层的频道只是少一段，不会报错；
  - `create_agent(model, tools, system_prompt)` 把「模型 → 工具 → 模型」的循环
    编成 LangGraph 状态图；
  - `agent.astream(..., stream_mode=["messages", "updates"])`：
    `messages` 产出 `AIMessageChunk` 增量，转成 `delta` 事件；
    `updates` 提供 `ToolMessage`，从 `artifact` 提取来源和跳转动作。
- 工具用 `@tool` 装饰器定义：函数名是工具名，docstring 是给模型看的说明，
  类型注解自动生成参数 schema。当前有三个工具：
  `get_current_datetime`、`search_site`、`navigate_to_article`。
- 检索工具使用 `response_format="content_and_artifact"`：
  content 给模型读，artifact 给程序生成 `citations` / `action` 事件，
  避免模型改写或编造链接。
- **API Key 只存在于服务端环境变量**，浏览器永远拿不到。
- 没有 key 或 `MOCK_MODE=true` 时用 LangChain 的 `GenericFakeChatModel`
  模拟流式输出，方便前端联调。
- 代码注释按“教学”标准编写，阅读顺序见 [backend/README.md](../backend/README.md)。

#### 接口约定

请求：

```json
{
  "messages": [{ "role": "user", "content": "站内有讲 Docker 的文章吗？" }],
  "page_url": "https://blog.example.com/archives/123/",
  "page_title": "某篇文章"
}
```

启用登录后还需要 `Authorization: Bearer <JWT>`；未登录或 token 失效返回 401。

响应（`text/event-stream`，每个事件一行 `data:`）：

```text
data: {"type":"delta","content":"有"}
data: {"type":"delta","content":"的～"}
data: {"type":"citations","citations":[{"title":"Docker 入门","url":"https://blog.example.com/archives/42/","post_id":42,"excerpt":"……","image_url":null,"kind":"article"}]}
data: {"type":"action","action":{"type":"navigate","url":"https://blog.example.com/archives/42/","label":"带我去看看 →"}}
data: {"type":"done"}
data: {"type":"error","message":"..."}
```

前端已实现 `delta / citations / action / error / done` 五种事件。
来源在气泡下方显示为 `[1] [2]` 这样的小序号；鼠标悬浮或键盘聚焦时弹出卡片，
展示标题、正文摘要和图片（有 `image_url` 才显示，没有就完全省略），
点击卡片里的“打开文章”在新标签页打开。

其他接口：

- `GET /api/quota`：返回当天个人/全站剩余额度，不消耗次数，需要登录；
- `GET /api/admin/settings`：管理员读取当前运营参数与只读运行信息；
- `PUT /api/admin/settings`：管理员更新参数，后端校验后原子写入 JSON。

聊天响应的 SSE 头里还会带 `X-Quota-Limit`、`X-Quota-Remaining`、
`X-Global-Quota-Limit`、`X-Global-Quota-Remaining`（管理员为 `X-Quota-Unlimited: 1`），
前端因此不需要为刷新额度额外发请求。

## 知识库与工具

### 文章同步

当前实现：命令行全量重建，代码在 [backend/app/kb/sync.py](../backend/app/kb/sync.py)。

```bash
cd backend
uv run python -m app.kb.sync
```

流程：

1. 通过 WordPress REST API 分页拉取已发布文章。脚本会依次尝试
   `/wp-json/wp/v2/posts`、`/index.php/wp-json/wp/v2/posts`、
   `/?rest_route=/wp/v2/posts`，兼容没有开启伪静态的站点；
2. HTML 正文清洗成纯文本，按 600 字左右切块，相邻块重叠 80 字；
3. 提取文章图片（兼容 Argon 懒加载的 `data-original`），调用
   `deepseek-flash` 生成中文描述、OCR 和关键词；
4. 文本块和图片块一起写入 SQLite FTS5；
5. 图片描述按 URL 缓存，重复同步不重复计费。

每次同步默认全量重建（先清空文章与索引，保留图片缓存），
能自然处理删文、改标题、删图等情况。文章上千篇后可改为按 `modified_gmt` 增量更新。

未来两种触发方式可以互补：

1. **增量钩子**：插件在 `save_post` / `publish_post` 时调用后端同步接口；
2. **定时兜底**：后端定时扫描 REST API。

不采用「每次提问现场爬站」：查询时只查索引，才快且稳定。

### 索引与检索

SQLite FTS5，`tokenize='unicode61 remove_diacritics 2'`。
中文先用 jieba 分词，以空格写入 `tokens` 列，查询时做同样处理。
每条 chunk 保存：

```text
post_id, chunk_index, kind, image_url, url, title, body, tokens
```

检索顺序：AND → OR（按命中比例过滤）→ LIKE 兜底 → 按文章去重 →
标题命中加成重排。图片块命中时，结果会带上 `image_url`，前端显示缩略图。

后续可加向量检索（本地 embedding 模型或外部 embedding API）做混合召回。
**引用来源直接来自索引元数据**，不是让模型自由编造。

更详细的原理、配置和排错见 [knowledge-base.md](knowledge-base.md)。

### 工具设计

- `search_site(query, limit)`：检索文章/图片块，content 给模型，
  artifact 是带 `post_id/title/url/excerpt/kind/image_url` 的结构化结果；
- `navigate_to_article(url, label)`：校验 URL 必须与 `WP_SITE_URL` 同站，
  artifact 产生 `action` 事件，前端渲染按钮（不自动跳转，避免打断阅读）；
- `get_current_datetime(timezone_name)`：回答当前时间。

Agent 循环：模型决定调用工具 → 后端执行 → 结果回填 → 生成最终回答。
来源通过 `citations` 事件单独下发，前端在气泡下方渲染小序号与悬浮来源卡片。

## 认证与额度（已实现）

这部分复用 WordPress 的身份体系，不另建用户表，也**不让浏览器保存任何密钥**。

```text
浏览器（已登录 WordPress）
  ├─ POST /wp-json/l2d-ai/v1/token   Cookie + X-WP-Nonce
  │      └─ WordPress 校验登录态，签发短期 JWT
  └─ POST /ai-api/chat               Authorization: Bearer <JWT>
         └─ FastAPI 校验 JWT、扣额度、再调用模型

WordPress 后台设置页
  └─ 管理员 JWT → /ai-api/admin/settings
```

约定：

- **身份**：注册、登录、找回密码、邮箱验证全部使用 WordPress 原生能力；
  默认新用户角色为 Subscriber，管理员为 Administrator；
- **令牌**：插件通过 `POST /wp-json/l2d-ai/v1/token` 签发短时效 JWT，
  `sub` 为用户 ID，`exp` 控制有效期，并标记是否拥有 `manage_options`；
  token 接口必须校验 Cookie + nonce，并返回 `Cache-Control: no-store`；
- **JWT 密钥**只存在于后端和 WordPress 服务端配置中，绝不注入前端；
- **个人额度**：Python 后端按 WordPress 用户 ID 统计每日消息数，
  SQLite 用一条 `INSERT … ON CONFLICT … WHERE used < limit` 原子占位，
  并发下不会超额；请求在输出第一个字之前失败会自动退款；
- **全站额度**：所有普通用户当天合计的上限，和个人额度在**同一个事务**里占位；
  全站满了会回滚个人计数，避免“没聊成却扣了一条”。管理员不占全站额度；
- **管理员**：拥有 `manage_options` 的账户不受个人/全站额度限制，但聊天请求仍要经过 JWT 校验；
- **游客**：前端显示登录提示，后端直接返回 401，不调用模型；
- **额度展示**：打开面板时 `GET /api/quota` 查询（不消耗次数）；
  每次聊天直接从响应头 `X-Quota-*` / `X-Global-Quota-*` 刷新，不额外发请求；
- **防失控**：单轮模型调用、工具调用、超时都有上限（默认 6 / 6 / 60 秒）；
  达到上限会优雅收尾，避免一次请求陷入死循环而烧掉大量 token；
- **后台配置**：WordPress 设置页可改模型名、单用户日额度、全站日额度、
  模型/工具调用上限和超时；保存后写入后端 `data/runtime_settings.json`，立即生效；
  支持恢复默认值提示，非法值整批拒绝；
- **QQ 关系评估**：设置页“QQ 关系评估”分区可改 `appraisal_enabled`（每轮私聊是否在掷骰子前调用一次轻量模型）、
  `appraisal_timeout_seconds`（0.2–5 秒，超时回退规则）、`appraisal_global_daily_budget` 与
  `appraisal_per_user_daily_budget`（全站 / 每人每日评估次数，`0` 表示不限）。
  评估预算独立于聊天额度，用尽只跳过评估，不影响回复；
- **API Key 与端口**：属于部署配置，后台只读展示，不提供在线修改，也不回显 Key；
- **形象尺寸**：后台三档预设（200×360 / 260×460 / 330×600）或自定义像素，
  由插件注入前端，手机端仍有响应式收缩；
- **防刷**：登录/注册/聊天接口限流，注册加验证码或邮箱验证；
  页面缓存必须排除 token 接口和 `/ai-api/`。

## 身份分级与额外额度（已实现）

额度分三层，彼此在**同一个 SQLite 事务**里结算：

| 层 | 谁有 | 刷新方式 | 在哪里配置 |
| --- | --- | --- | --- |
| 每日额度 | 所有已登录用户 | 每天 0 点按站点时区刷新 | WP 设置页（普通 / 会员两组上限） |
| 额外额度 | 被管理员发放或兑换码兑换过的人 | **永不刷新**，用一条少一条 | WP 用户页发放，或兑换码 |
| 全站额度 | 全站合计（管理员不计） | 每天刷新 | WP 设置页，0 表示不限 |

关键约定：

- **身份**：`user`（普通）/ `member`（会员）/ `admin`（WordPress 管理员）。
  `member` 是后端自己的概念——WordPress 那边仍然是 Subscriber；
- **会员一律有期限**：授予时必须给天数，续期从「原到期日」和「今天」里较晚的那个往后加，
  提前续费不会把剩余时间吞掉；
- **到期自动降级**：读取等级时发现 `tier_expires_at` 已过，就直接写回 `user`。
  没有定时任务，也不需要 cron——代价是第一次读到过期数据的那次请求顺手做一次写操作；
- **等级不进 JWT**：token 有 15 分钟有效期，管理员刚改完等级，旧 token 还带着旧身份。
  后端每次请求现查等级，慢一点但不会出现「升了会员却没生效」；
- **扣费顺序**：先每日额度，再额外额度。先用会过期的，再用不会过期的；
- **全站额度是硬上限**：花每日额度和花额外额度**都**计入全站计数。
  全站满了谁都停，包括手里还有额外额度的人——它防的是整体账单失控，不是个人用量；
  全站额度放在事务里**最后**占，占不到就整笔回滚，不会出现「扣了额度却没聊成」；
- **记录可查**：每次额外额度增减都写一条流水（`credit_ledger`），
  后台能看到余额，但流水只留在数据库里，不提供界面——需要时直接查表；
- **邮箱不出后端**：后端只存等级、余额、备注、注册 IP（IP 存在 WordPress 用户元数据里），
  接口不返回邮箱；后台页把 WordPress 的用户列表和后端档案按用户 ID 合并显示。

## 注册防护（已实现）

开放注册后，插件在 `registration_errors` 上做两道检查，命中就拒绝这次注册：

1. **邮箱域名黑名单**：一行一个域名，子域同样命中
   （写 `example.com` 也会拦下 `someone@mail.example.com`）；
2. **同一 IP 的注册频率**：每小时上限、每天上限、两次注册最小间隔，三项都可以设成 0 关闭。

设计上的取舍：

- IP 只取 `REMOTE_ADDR`，不信任 `X-Forwarded-For`——后者客户端可以随便写，
  用它做限制等于让对手自己选计数桶。代价是站点前有反向代理时
  `REMOTE_ADDR` 是代理地址，限制会变成全站共享，这时把对应项设成 0；
- 计数用 transient 存「窗口起点 + 次数」，而不是 `set_transient($key, $n + 1, $ttl)`：
  后者每次都会重置过期时间，访问足够频繁时窗口永远不过期，变成累计计数；
- 拦截提示对用户是中性的「该邮箱暂不支持注册」，不说明命中了哪条规则；
  具体原因写进后台「最近拦截记录」（最多 50 条）；
- 不做自动删号：删除是不可逆的，拦截记录留给站长判断

## 可移植性

- **插件 + 构建产物**：`pnpm build` 后整个 `wp-live2d-ai` 目录是自包含的，复制即可安装；
- **后端**：纯 Python，`pyproject.toml` + `.env`，可在任意机器用 `uv sync` 重建；部署用 systemd 或 Docker 均可；
- **路径**：前端资源地址由插件注入，不写死域名；API 默认同域 `/ai-api`，由 Nginx 反代，生产环境无 CORS 问题；
- **模型**：与文章内容无关，单独目录，替换模型不需要改代码。

## 站外通道（实验性）

QQ 走 OneBot 11，NapCat 只负责 QQ 登录和收发，后端通过薄适配器接入：

```
NapCat ──事件──▶ POST /api/channels/qq/events ──▶ QQAdapter ──▶ stream_reply()
   ▲                                                              │
   └──────────────── send_group_msg ◀────────────────────────────┘
```

- 适配器只做事件解析、白名单、触发方式、去重、短期历史、命令分发和消息拆条；
- 人设、知识库、工具完全复用网页链路，不复制一套提示词；
- 调用 agent 时带 `channel="qq_private"` 或 `"qq_group"`，页面上下文一律为空。
  人设里对应的覆盖层负责把场景从“图书馆”换成“通讯法阵”——否则梅林会把
  网页那套背景（书架、访客、翻书）原样搬到 QQ 里；
- 默认关闭，`QQ_ALLOWED_GROUPS` 为空时不回复任何群；私聊不受群白名单限制；
- 本地没有 NapCat 时用 `QQ_MOCK_MODE=true` 或 `scripts/mock_onebot.py` 降级联调。

### 命令与触发

一条消息进来后的处理顺序是固定的，顺序本身就是规则：

```
去重 → 触发判断 → 斜杠命令 → 绑定判定 → 额度 → agent
```

- 命令以 `/` 或 `\` 开头（全角 `／`、`＼` 经 NFKC 归一化后同样识别），
  由正则提取**命令名**后交给注册表（`app/channels/qq/commands.py` 的 `COMMANDS`）；
  命令永不进模型、也不进对话历史，加新命令只需往里加一条 `Command(...)`；
- 群里必须 @ 梅林才触发；私聊任意文本都算正文；
- 未绑定用户：群里只得到“去私聊”的一句引导，私聊里每收到一条消息就回一次绑定流程，
  从不主动推送；已绑定用户群聊、私聊都能正常对话。

### 绑定

```
WordPress 个人资料页                     后端
  明文绑定码 (usermeta)  ──PUT /me/bind-code──▶  SHA-256 存在 user_profile
  显示 /绑定 XXXX-XXXX
                            QQ 私聊 /绑定 XXXX-XXXX
                                     ─────────▶  算哈希比对 → 写 qq_uin
```

- 明文只存在 WordPress（用户随时可看、可刷新），后端只存哈希；
- 后端的 `is_admin` 取自插件签发的 JWT 的 `admin` 声明，只用于 QQ 额度豁免；
- 同一 QQ 只能绑一个账号（`qq_taken`），一个账号也只能有一个 QQ（`account_taken`）；
- 解绑/换绑只开放给网页端：QQ 那头无法证明“我是我”，允许它在 QQ 侧换绑等于
  谁先私聊谁就能顶掉别人。

### 额度

QQ 有两种计法，由后台开关 `qq_quota_shared` 选择：

| 模式 | 按人 | 全站 |
| --- | --- | --- |
| 共享（`qq_quota_shared=true`） | 网页那套 `daily_quota` / `member_daily_quota` | 网页全站额度 + QQ 全站额度 |
| 独立（默认） | `qq_daily_quota` / `qq_member_daily_quota`（或 `qq_unlimited` 跳过） | 仅 QQ 全站额度 |

- `qq_global_daily_quota` 在**两种模式下都生效**，计数独立于网页全站额度；
- 管理员（JWT `admin`）不占任何计数；
- agent 没产出正文（报错/断流）时自动退回这次占用，避免一次故障吃掉全天额度。

## 安全

- DeepSeek Key 只在后端 `.env`，前端与 WordPress 数据库都不保存；
- 生产环境同域反代，CORS 仅保留本地开发地址；
- 已有：JWT 身份校验、单用户/全站每日额度、管理员不限量、单次消息长度上限（8000 字）、
  agent 调用次数与时长上限；上线后仍建议在 Nginx 加请求频率限制；
- 语音识别依赖浏览器厂商的在线服务，介意隐私可在设置中隐藏语音按钮（后续加配置项）。
