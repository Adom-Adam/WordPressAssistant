# 技术架构

## 目标

在 WordPress（Argon 主题）博客左下角放一个 Live2D 小助手：

- 形象在最上层，但**不拦截下层页面的点击、滚动、文本选择**；
- 鼠标未悬停时控件半透明，悬停时清晰；
- 支持换模型、文本对话、语音输入；
- AI 能回答站内文章相关问题，**给出引用来源与跳转按钮**；
- 最终可平滑迁移到云服务器上的现有 WordPress，不破坏原有主题与功能。

## 非目标（当前阶段）

- 不做独立的用户系统、不做多轮长期记忆持久化；
- 不接管 WordPress 的搜索、评论等既有功能；
- 不在浏览器端保存 DeepSeek API Key；
- 不支持 Cubism 2（`.model.json`）。

## 三层结构

### 1. WordPress 插件（PHP，薄层）

位置：`wordpress-plugin/wp-live2d-ai/`

职责只有三件事：

1. 在**前台页面** enqueue 构建产物 `assets/app/app.js`、`app.css`、Cubism Core；
2. 通过 `wp_add_inline_script` 注入运行时配置（API 地址、资源地址、站点名、语言）；
3. 提供「设置 → Live2D 小助手」页面，配置 AI 服务地址。

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
- **换模型**：构建前扫描 `public/live2d/models/**/*.model3.json` 生成 `manifest.json`，上拉框由此渲染。
- **语音输入**：浏览器 `SpeechRecognition` / `webkitSpeechRecognition`，识别结果填入输入框，用户确认后发送。
- **样式**：全部作用域在 `.l2d-root` 下，使用 CSS 变量（天蓝色系），不会污染主题样式。

### 3. 后端（FastAPI + LangChain）

位置：`backend/`

- `POST /api/chat`：接收对话历史与当前页面信息，返回 **SSE 流**。
- 对话由 **LangChain agent** 驱动：
  - `ChatDeepSeek`（`langchain-deepseek`）封装 DeepSeek 的 OpenAI 兼容接口；
  - `create_agent(model, tools, system_prompt)` 把「模型 → 工具 → 模型」的循环
    编成 LangGraph 状态图；
  - `agent.astream(..., stream_mode="messages")` 产出 `AIMessageChunk` 增量，
    转成前端的 `delta` 事件。
- 工具用 `@tool` 装饰器定义：函数名是工具名，docstring 是给模型看的说明，
  类型注解自动生成参数 schema。当前内置 `get_current_datetime` 作为链路示例，
  后续新增 `search_site` / `navigate` 只需加入 `TOOLS` 列表。
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

响应（`text/event-stream`，每个事件一行 `data:`）：

```text
data: {"type":"delta","content":"有"}
data: {"type":"delta","content":"的～"}
data: {"type":"citations","citations":[{"title":"Docker 入门","url":"/archives/42/","post_id":42}]}
data: {"type":"action","action":{"type":"navigate","url":"/archives/42/","label":"带我去看看 →"}}
data: {"type":"done"}
data: {"type":"error","message":"..."}
```

前端已实现 `delta / citations / action / error / done` 五种事件，后端后续只需按此格式输出。

## 知识库与工具（第二阶段）

### 文章同步

两种方式互补：

1. **增量钩子**：插件在 `save_post` / `publish_post` 时调用后端 `/api/kb/sync`，只更新该文章；
2. **定时兜底**：后端定时通过 WordPress REST API（`/wp-json/wp/v2/posts?per_page=...&_embed`）或 sitemap 全量增量扫描。

不采用「每次提问现场爬站」：查询时只查索引，才快且稳定。

### 索引与检索

初版建议：SQLite + FTS5（`tokenize='trigram'` 对中文友好），每条 chunk 保存：

```text
post_id, title, url, chunk_index, content
```

后续可加向量检索（本地 embedding 模型或外部 embedding API）做混合召回。
**引用来源直接来自索引元数据**，不是让模型自由编造。

### 工具设计

- `search_site(query)`：检索文章，返回带 `post_id/title/url/excerpt` 的结果；
- `get_post(post_id)`：取文章摘要/正文片段；
- `navigate(url, label)`：产生 `action` 事件，前端渲染跳转按钮（不自动跳转，避免打断阅读）。

Agent 循环：模型决定调用工具 → 后端执行 → 结果回填 → 生成最终回答。
最终回答中的引用通过 `citations` 事件单独下发，前端气泡下方渲染来源卡片。

## 可移植性

- **插件 + 构建产物**：`pnpm build` 后整个 `wp-live2d-ai` 目录是自包含的，复制即可安装；
- **后端**：纯 Python，`pyproject.toml` + `.env`，可在任意机器用 `uv sync` 重建；部署用 systemd 或 Docker 均可；
- **路径**：前端资源地址由插件注入，不写死域名；API 默认同域 `/ai-api`，由 Nginx 反代，生产环境无 CORS 问题；
- **模型**：与文章内容无关，单独目录，替换模型不需要改代码。

## 安全

- DeepSeek Key 只在后端 `.env`，前端与 WordPress 数据库都不保存；
- 生产环境同域反代，CORS 仅保留本地开发地址；
- 上线前建议加：请求频率限制、单次消息长度限制（已有 8000 字上限）、后端来源校验（可选，用 WordPress nonce + 共享密钥）；
- 语音识别依赖浏览器厂商的在线服务，介意隐私可在设置中隐藏语音按钮（后续加配置项）。
