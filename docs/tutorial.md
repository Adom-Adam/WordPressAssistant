# 从零搭建 WordPress Live2D AI 助手：完整操作教程

> 本文按**实际执行顺序**记录搭建过程中用到的每一条命令、每一步的目的，以及遇到的报错和解决方式。
> 环境：Windows 11 + Git Bash，项目根目录 `E:\study\code\LangChain\WordPressAssistant`。
> 目标：读完可以独立复现一个「WordPress 左下角 Live2D 助手 + DeepSeek 后端」的完整工程。

---

## 0. 先看成品与整体思路

三层结构，前端不懂 AI，后端不懂页面：

```
浏览器
  ├─ Live2D 形象 / 对话 / 语音 UI           →  frontend（Vite + TS + PixiJS）
  └─ fetch /ai-api/chat（SSE 流式）
        └─ Python AI 服务                    →  backend（FastAPI + LangChain + DeepSeek）
              └─ 知识库 / 站内检索（后续）    →  backend/knowledge
WordPress 只负责「把前端资源挂到页面上」     →  wordpress-plugin（PHP）
```

这样做的原因：

- **改主题会被主题升级覆盖**，所以用插件；
- **前端资源、AI 密钥要分开**，浏览器永远拿不到 DeepSeek Key；
- **AI 后端独立进程**，迁移服务器时插件和 Python 服务各自搬走即可。

---

## 1. 环境检查

先确认工具链版本，避免装到一半才发现 Node/Python 不匹配：

```bash
uv --version
node --version
pnpm --version
python --version
git --version
```

本次结果：

```text
uv 0.9.24
v22.22.0
12.4.1
Python 3.11.5
git version 2.47.1.windows.2
```

> 版本要求：Node 20+、Python 3.11+、pnpm 10+。没有 `uv` 先装：
> `curl -LsSf https://astral.sh/uv/install.sh | sh`

---

## 2. 获取项目与初始化目录

目标目录当时是空的，直接把远程仓库克隆到当前目录：

```bash
git clone https://github.com/Adom-Adam/WordPressAssistant.git \
  "E:/study/code/LangChain/WordPressAssistant"
```

验证：

```bash
cd "E:/study/code/LangChain/WordPressAssistant"
git log --oneline -3
ls -la
```

当时仓库只有一个 `Initial commit`，里面只有 `.gitignore` 和 `README.md`，
所以后续所有代码都是新建的。

### 目录规划

```bash
mkdir -p frontend/src/{api,live2d,ui} \
         frontend/public/live2d/{core,models} \
         frontend/scripts \
         backend/app/{agent,api} \
         backend/tests \
         wordpress-plugin/wp-live2d-ai \
         docs
```

> 实际创建文件用的是编辑器工具，但等价于上面的 `mkdir -p`。
> 先规划目录，后面每个文件都知道自己该放哪。

---

## 3. 前端搭建

### 3.1 先验证选型，不要盲目装包

需求是 Cubism **3/4** 模型（不是 Cubism 2），可选的常见方案是
`pixi-live2d-display`。先查它的版本和 PixiJS 依赖关系：

```bash
pnpm view pixi-live2d-display versions --json
pnpm view pixi-live2d-display@0.4.0 peerDependencies
pnpm view pixi.js@6 version
```

关键结论：

```text
0.4.0 的 peerDependencies 要求 @pixi/* ^6
pixi.js@6 的最新版本是 6.5.10
```

所以组合锁定为：

- `pixi.js@6.5.10`
- `pixi-live2d-display@0.4.0`
- 入口用 `pixi-live2d-display/cubism4`（Cubism 4 运行时向下兼容 3.x 模型）

再确认 Cubism Core 的 CDN 可用：

```bash
curl -sI https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js | head -5
```

返回 `HTTP/1.1 200 OK`，于是把它下载到本地（生产环境不能依赖外网 CDN）：

```bash
mkdir -p frontend/public/live2d/core
curl -fsSL -o frontend/public/live2d/core/live2dcubismcore.min.js \
  https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js
```

> Cubism Core 是 Live2D 官方运行时，**必须先于业务脚本加载**；
> 授权请遵守 Live2D 官方条款，模型版权也需自行确认。

### 3.2 创建前端工程

手写而不是用脚手架，文件更少、结构更清楚：

| 文件 | 作用 |
| --- | --- |
| `frontend/package.json` | 依赖与脚本（`dev` / `build` / `prepare:models`） |
| `frontend/vite.config.ts` | 开发代理、构建输出到插件目录 |
| `frontend/tsconfig.json` | 严格模式 TS 配置 |
| `frontend/index.html` | 仅开发预览用，先加载 Cubism Core |
| `frontend/scripts/gen-model-manifest.mjs` | 扫描模型生成 `manifest.json` |
| `frontend/src/config.ts` | 读取插件注入的运行时配置 |
| `frontend/src/api/chat.ts` | SSE 客户端 |
| `frontend/src/live2d/manager.ts` | PIXI + Live2D 加载、缩放、视线跟随 |
| `frontend/src/ui/assistant.ts` | 悬浮按钮、对话、语音 |
| `frontend/src/style.css` | 天蓝色主题，全部作用域在 `.l2d-root` |
| `frontend/src/main.ts` | 启动入口 |

两个关键配置：

```ts
// vite.config.ts
server: {
  proxy: {
    '/ai-api': {
      target: 'http://127.0.0.1:18080',
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/ai-api/, '/api'),
    },
  },
},
build: {
  outDir: '../wordpress-plugin/wp-live2d-ai/assets/app',
  emptyOutDir: true,
  cssCodeSplit: false, // 见 3.5 的坑
  rollupOptions: {
    input: 'src/main.ts',
    output: {
      format: 'iife',          // WordPress 用普通 <script> 加载
      name: 'L2DAI',
      inlineDynamicImports: true,
      entryFileNames: 'app.js',
      assetFileNames: 'app.[ext]',
    },
  },
},
```

为什么要 `format: 'iife'`：WordPress 的 `wp_enqueue_script` 默认输出
`<script src="...">`，不是 `<script type="module">`。打成 IIFE 单文件最省事。

### 3.3 安装依赖（踩坑 1：pnpm 拦截构建脚本）

```bash
cd frontend
pnpm install
```

第一次安装报错：

```text
Error: ERR_PNPM_IGNORED_BUILDS
  × installing dependencies
  ╰─▶ Ignored build scripts: esbuild@0.28.2
```

pnpm 10+ 默认不执行依赖的安装脚本（供应链安全）。解法：

```bash
pnpm approve-builds --all -y
pnpm install
```

`approve-builds` 会把允许列表写进 `frontend/pnpm-workspace.yaml`：

```yaml
allowBuilds:
  esbuild: true
```

> 注意：新版 pnpm 已经**不再读取 `package.json` 里的 `pnpm.onlyBuiltDependencies`**，
> 放在 `package.json` 会收到 `The "pnpm" field in package.json is no longer read` 警告。

### 3.4 模型放哪、manifest 怎么来

模型目录约定：

```text
frontend/public/live2d/models/<模型名>/
├── <模型名>.model3.json   ← 必需
├── *.moc3
├── 贴图/动作/物理文件
└── model.json            ← 可选：{"name":"显示名","scale":1,"offsetX":0,"offsetY":0}
```

`gen-model-manifest.mjs` 会递归扫描所有 `.model3.json`，生成：

```json
{
  "default": "hiyori",
  "models": [
    { "id": "hiyori", "name": "梅林", "path": "hiyori/hiyori.model3.json", "scale": 1, "offsetX": 0, "offsetY": 0 }
  ]
}
```

运行：

```bash
pnpm run prepare:models
```

`pnpm dev` / `pnpm build` 会自动先跑这一步，所以正常流程不用单独执行。

Spine 模型放在另一个目录，结构不同：

```text
frontend/public/spine/models/<模型文件夹>/
├── <骨架>.json          ← 必需，含 skeleton / animations
├── <骨架>.atlas         ← 必需，和骨架同名
├── 贴图.png
└── model.json           ← 可选：{"name","animation","loop","scale","offsetX","offsetY"}
```

脚本会为它生成 `public/spine/models/manifest.json`，并在构建时只复制
`.json/.atlas/图片`。后台「形象引擎」选择 Spine 后才生效；加载失败会自动回退 Live2D。
字段和现有模型动画清单见
[frontend/public/spine/models/README.md](../frontend/public/spine/models/README.md)。

### 3.5 类型检查与构建（踩坑 2：CSS 文件没生成）

```bash
pnpm exec tsc --noEmit
pnpm run build
```

第一次构建只生成了 `app.js`，没有 `app.css`——因为 Vite 在「JS 作为入口」的模式下
把 CSS 内联进了 JS。WordPress 插件是按独立 CSS 文件 `enqueue` 的，所以必须让它显式产出：

```ts
// vite.config.ts
build: {
  cssCodeSplit: false,
},
```

重新构建：

```bash
pnpm run build
```

输出：

```text
../wordpress-plugin/wp-live2d-ai/assets/app/app.css   8.51 kB
../wordpress-plugin/wp-live2d-ai/assets/app/app.js  636.55 kB
✓ built in 2.19s
```

`app.js` 超过 500 KB 是 PixiJS + Live2D 的正常体积（gzip 后约 178 KB），
首版可以接受，后续再做拆包。

---

## 4. 后端搭建

### 4.1 uv 初始化

`backend/pyproject.toml` 手写，核心内容：

```toml
[project]
name = "l2d-ai-backend"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.30.0",
  "pydantic-settings>=2.4.0",
  "sse-starlette>=2.1.0",
  "langchain>=1.4",
  "langchain-deepseek>=1.1",
  "tzdata>=2026.5",
]

[tool.uv]
package = false          # 纯应用，不需要被打包成 wheel
```

三个新依赖各自的作用：

- `langchain`：agent 框架（`create_agent`、消息类型、工具抽象）；
- `langchain-deepseek`：DeepSeek 的 LangChain 集成（`ChatDeepSeek`）；
- `tzdata`：Windows 自带系统没有完整的 IANA 时区数据库，
  示例工具里的 `ZoneInfo("Asia/Shanghai")` 需要它。

安装时也可以用 `uv add`：

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  uv add "langchain>=1.4" "langchain-deepseek>=1.1" tzdata
```

> 装好之后 `pyproject.toml` 会自动多出 `[[tool.uv.index]]`，
> 以后直接 `uv sync` 就会走镜像，不用每次带环境变量。

### 4.2 安装依赖（踩坑 3：PyPI 超时）

```bash
cd backend
uv sync
```

国内网络可能报：

```text
error: Request failed after 3 retries
Caused by: Failed to fetch: https://pypi.org/simple/uvicorn/
Caused by: operation timed out
```

换清华镜像：

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
```

> 想长期生效，可以写进 `~/.config/uv/uv.toml`：
> `default-index = "https://pypi.tuna.tsinghua.edu.cn/simple"`

`uv` 会创建 `backend/.venv`；当时它选择了 CPython 3.13.11（满足 `>=3.11` 即可）。

### 4.3 配置与启动

```bash
cp .env.example .env
# 编辑 .env，填 DEEPSEEK_API_KEY
```

启动：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 18080
```

> 本次机器上本来就存在 `DEEPSEEK_API_KEY` 环境变量，所以没有创建 `.env`
> 也直接走了真实 API；部署到服务器时务必单独配置 `.env`。

### 4.4 LangChain agent 是怎么跑起来的

后端代码按“模型 / 人设 / 工具 / agent 循环”拆成四个部分，
每个文件都有面向初学者的详细注释，推荐阅读顺序见
[backend/README.md](../backend/README.md)。

人设正文放在 `backend/data/personas/`（已被 `.gitignore` 忽略），
代码只负责组装。第一次运行前执行 `cp -r personas.example data/personas`，
再把示例文字替换成真实人设；格式见
[backend/personas.example/README.md](../backend/personas.example/README.md)。

核心只有三步：

```python
# 1. 模型：DeepSeek 的 LangChain 封装
model = ChatDeepSeek(model="deepseek-chat", api_key=..., base_url=...)

# 2. 工具：@tool 装饰器把普通函数变成模型可调用的工具
@tool
def get_current_datetime(timezone_name: str = "Asia/Shanghai") -> str:
    """获取指定时区的当前日期和时间。"""
    ...

# 3. Agent：把模型和工具编成一张「模型 ↔ 工具」循环的状态图
agent = create_agent(model, tools=[get_current_datetime], system_prompt="...")

# 4. 消费流式输出
async for chunk, metadata in agent.astream(
    {"messages": [HumanMessage(content="现在几点了？")]},
    stream_mode="messages",
):
    if chunk.content:
        print(chunk.content)
```

几个容易混淆的点：

- **docstring 不是给人看的，是给模型看的**。工具描述写不清楚，模型就不会调用；
- **类型注解会变成参数 schema**，所以 `timezone_name: str` 不能省；
- **`system` 角色由后端生成**，前端只能传 `user / assistant`，防止提示词注入；
- **工具内部不要抛异常**，返回错误文字让模型自己消化，否则整张图会中断；
- **mock 模式**用 `GenericFakeChatModel` 模拟流式输出，没有 Key 也能联调前端。

实测输出（DeepSeek 真的调用了工具，拿到当前时间后再回答）：

```text
data: {"type": "delta", "content": "现在是"}
data: {"type": "delta", "content": "晚上"}
data: {"type": "delta", "content": "7"}
...
```

---

## 5. WordPress 插件

插件只有三个职责，别把 AI 逻辑写进 PHP：

1. `wp_enqueue_script` / `wp_enqueue_style` 加载 `assets/app/`；
2. `wp_add_inline_script` 注入 `window.L2D_AI_CONFIG`（API 地址、资源地址、语言、人设名字、模型）；
3. 后台设置页配置 AI 服务地址、人设名字和 Live2D 形象。

关键判断：构建产物不存在时直接 return，避免开发环境 404：

```php
$app_dir = plugin_dir_path( __FILE__ ) . 'assets/app/';
if ( ! is_readable( $app_dir . 'app.js' ) ) {
    return;
}
```

---

## 6. 联调验证（每一步都实际执行过）

### 6.1 健康检查

```bash
curl -s http://127.0.0.1:18080/api/health
```

```json
{"ok":true,"mock":false,"model":"deepseek-chat"}
```

`mock:false` 表示读到了真实 Key；如果显示 `true`，说明只会返回假回复。

### 6.2 验证流式对话（踩坑 4：Git Bash 中文编码）

直接内联中文 JSON：

```bash
curl -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}]}'
```

返回：

```json
{"detail":"There was an error parsing the body"}
```

原因是 Git Bash 向 `curl.exe` 传递中文时编码不一致。改用 UTF-8 文件：

```bash
printf '%s' '{"messages":[{"role":"user","content":"hello"}]}' > /tmp/payload.json
curl -sN -X POST http://127.0.0.1:18080/api/chat \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/payload.json
```

正常输出：

```text
data: {"type": "delta", "content": "你"}
data: {"type": "delta", "content": "好"}
...
```

> 前端用 `fetch` 发送 UTF-8 JSON，不存在这个问题；这是命令行调试专属坑。

### 6.3 验证前端代理链路

前端开发服务器会把 `/ai-api/*` 转发到后端 `/api/*`：

```bash
curl -sN -X POST http://127.0.0.1:5173/ai-api/chat \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/payload.json
```

### 6.4 验证静态资源

```bash
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" \
  http://127.0.0.1:5173/live2d/core/live2dcubismcore.min.js
curl -s http://127.0.0.1:5173/live2d/models/manifest.json
```

```text
200 207155
{ "default": null, "models": [], ... }
```

`models: []` 是因为当时还没放模型，属正常状态。

### 6.5 端点函数里为什么又套了一个 `event_stream`？

`app/api/chat.py` 的结构是：

```python
@router.post("/chat")
async def chat(request: ChatRequest) -> EventSourceResponse:
    async def event_stream():          # ← 异步生成器函数
        async for event in stream_reply(...):
            yield {"data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(event_stream(), ping=15)
```

这里有两个容易混淆的概念：

**1. 协程函数 vs 异步生成器函数**

- 函数体里只有 `await` → 协程函数（coroutine）；
- 函数体里有 `yield` → 异步生成器函数（async generator）。

FastAPI 的路径函数必须**返回一个 Response 对象**。如果直接在 `chat` 里写 `yield`，
它自己就变成了异步生成器函数，FastAPI 不会把生成器自动包装成流式响应。
所以必须分成两层：

- 外层 `chat`：立刻返回 `EventSourceResponse`；
- 内层 `event_stream`：之后按需产出数据。

**2. 调用生成器函数 ≠ 执行函数体**

```python
gen = event_stream()   # 此刻什么都不执行，只是创建生成器对象
```

真正开始执行，是在 `EventSourceResponse` 开始迭代它的时候。时间线是：

```text
请求到达
  → chat() 执行完，返回响应头 200 / text/event-stream
  → EventSourceResponse 向 event_stream 要下一项
  → event_stream 从 stream_reply 拿到一个 delta，yield 出去
  → sse-starlette 格式化成 "data: {...}\n\n" 并 flush 给浏览器
  → 重复，直到 done
  → 用户关闭页面时，生成器被关闭，CancelledError 传回 stream_reply 做清理
```

这就是“边生成边发送”的原理：端点函数很快返回，数据由响应对象**惰性拉取**。

**为什么不让 `stream_reply` 直接输出 SSE 格式？**

那样会把 AI 业务层和 HTTP 传输层耦合在一起。现在的分层是：

- `agent/core.py` 只产出 `{"type": "delta", ...}` 这样的内部事件；
- `api/chat.py` 只负责把事件转成 `data: {...}`。

以后要加心跳、鉴权、日志、限流，都改在 API 层；要换模型/工具，只改 agent 层。

**为什么定义在函数内部？**

内层函数是闭包，可以直接读取外层的 `request`，不用重复传参。
也可以提到模块级，写成 `_event_stream(request)`，效果一样，更利于单独测试。

---

## 7. 进程管理（踩坑 5：端口占用）

第一次用 shell 后台方式启动：

```bash
(uv run uvicorn app.main:app --host 127.0.0.1 --port 18080 &)
```

它虽然能跑，但脱离管理，后续再启一个会报端口占用（uvicorn 退出码 3）。
正确做法是让工具托管后台进程；如果已经残留，先查再杀：

```bash
netstat -ano | grep ":18080"
cmd //c "taskkill /PID 40652 /F"
```

然后重新启动后端与前端：

```bash
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 18080
cd frontend && pnpm dev --host 127.0.0.1
```

等待两个端口就绪（带重试，避免 sleep 猜时间）：

```bash
for i in $(seq 1 25); do
  h=$(curl -s --max-time 2 http://127.0.0.1:18080/api/health)
  f=$(curl -s --max-time 2 http://127.0.0.1:5173/ | head -c 60)
  if [ -n "$h" ] && [ -n "$f" ]; then echo "backend: $h"; echo "frontend: $f"; break; fi
  sleep 1
done
```

---

## 8. 构建与部署命令

### 本地构建

```bash
cd frontend
pnpm install
pnpm build          # 产物 → wordpress-plugin/wp-live2d-ai/assets/app/
```

### 上传插件

```bash
scp -r wordpress-plugin/wp-live2d-ai \
  root@服务器:/www/wwwroot/<站点>/wp-content/plugins/
```

### 服务器后端

```bash
cd /www/wwwroot/l2d-ai-backend
cp .env.example .env && vi .env      # 填 DEEPSEEK_API_KEY
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 18080
```

生产用 systemd 托管，Nginx 把 `/ai-api/` 反代到 `127.0.0.1:18080/api/`，
并**必须关闭缓冲**，否则流式回复会变成一次性输出：

```nginx
location /ai-api/ {
    proxy_pass http://127.0.0.1:18080/api/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
}
```

完整步骤见 [deploy.md](deploy.md)。

---

## 9. 站内知识库：从同步到来源浮层

前面几步已经让助手能聊天、能调工具。这一步让它真正“读过博客”：
回答站内问题时先检索文章，回答下面附来源序号，用户要的话再给跳转按钮。
完整原理单独写在 [knowledge-base.md](knowledge-base.md)，这里只记操作顺序和踩过的坑。

### 9.1 配置

在 `backend/.env` 里补上站点地址（其余有默认值）：

```dotenv
WP_SITE_URL=https://example.com
DEEPSEEK_MODEL=deepseek-flash
VISION_ENABLED=true
VISION_MODEL=deepseek-flash
```

> `.env` 只在进程启动时读取。改完要重启后端，否则 `/api/health` 还会显示旧模型。

### 9.2 第一次同步

```bash
cd backend
uv run python -m app.kb.sync
```

本项目实际跑出来的结果：

```text
文章：20 篇
文本块：147 个
图片块：11 个（本次处理图片 11 张）
图片描述缓存：11 条
数据库：backend/data/kb.sqlite3
```

文章发布或修改后，重新执行这一条即可。默认全量重建，图片描述有缓存，不会重复花钱。

### 9.3 图片怎么进入索引

FTS5 只认文字，所以同步时会把文章图片交给 `deepseek-flash`，让它输出：

```json
{
  "description": "一张示意图，展示 visual geometry 与 collision geometry 的关系",
  "ocr_text": "frame visual geometry collision geometry",
  "keywords": ["URDF", "碰撞几何", "可视化几何"]
}
```

这些文字作为 `kind='image'` 的块写进索引，同时保存图片地址。
搜索「碰撞几何 示意图」就能命中这张图，来源浮层会显示它。

两个实际踩到的坑：

1. **Argon 懒加载**：`<img src>` 是 1×1 占位图，真实地址在 `data-original`。
   提取顺序必须覆盖 `data-original → data-src → data-lazy-src → srcset → src`。
2. **思考模式吃光 max_tokens**：第一次调用视觉模型，800 个输出 token 全是
   reasoning，`content` 是空的。加 `extra_body={"thinking": {"type": "disabled"}}`
   后正常。聊天模型也一并关闭（`DEEPSEEK_DISABLE_THINKING=true`）。

### 9.4 检索与来源展示

同步完成后，agent 的工具列表里多了两个工具：

- `search_site(query, limit)`：检索正文和图片描述；
- `navigate_to_article(url, label)`：生成“带我去看看”按钮。

工具用 `response_format="content_and_artifact"` 返回两部分：
content 给模型读，artifact 给程序生成 SSE 事件。
`core.py` 用 `stream_mode=["messages", "updates"]` 同时收打字机增量和工具结果：

```text
data: {"type":"citations","citations":[{"title":"…URDF…","url":"…","image_url":"…","kind":"image"}]}
data: {"type":"delta","content":"找到"}
data: {"type":"action","action":{"type":"navigate","url":"…","label":"打开 URDF 建模那篇"}}
data: {"type":"done"}
```

前端把来源渲染成气泡下方的一排 `[1] [2]` 小序号，而不是直接铺开卡片。
这样长标题不会把聊天气泡撑宽，也不会出现“每段回答后面挂一串大卡片”的排版。

几个实现要点在 [assistant.ts](../frontend/src/ui/assistant.ts) 和
[style.css](../frontend/src/style.css)：

- 助手消息改为纵向 flex（`.l2d-msg--assistant`），保证“气泡 → 序号 → 跳转按钮”
  从上往下排列，而不是三个 flex 子项并排挤在一起；
- 悬浮卡片用 `position: fixed` 并挂到 `.l2d-root` 上。聊天记录
  `.l2d-chat__messages` 是 `overflow-y: auto`，如果卡片用 `absolute` 放在气泡里，
  会被滚动容器裁掉；
- 卡片根据序号在屏幕上的位置计算坐标，下方空间够就放下方，不够就翻到上方，
  并限制在视口内；
- 有 `image_url` 才创建 `<img>`；没有图片就什么都不放，不用书本/图片小图标占位；
- 鼠标移开有 160ms 延迟，方便从序号移进卡片；点页面其他地方、滚动、按 Esc 都会收起；
- 触屏没有 hover，点一下序号也能打开卡片；键盘用户 Tab 到序号时同样会显示。

### 9.5 验证

Windows Git Bash 里不要用 `-d '中文…'` 内联传 JSON，会报
`There was an error parsing the body`。用 Python 写 UTF-8 文件，再让 curl 读：

```bash
cd backend
uv run python - <<'EOF'
import json, pathlib
pathlib.Path('data/req.json').write_text(
    json.dumps({"messages":[{"role":"user","content":"站内有没有讲 DDS 的文章？"}]},
               ensure_ascii=False),
    encoding='utf-8',
)
EOF

curl -sN -X POST http://127.0.0.1:18080/api/chat \
  -H 'Content-Type: application/json' --data-binary @data/req.json
```

正常应该先看到 `citations`，再看到逐字的 `delta`，最后 `done`。

### 9.6 排序踩坑：标题权重没有生效

一开始 `bm25(chunks, 0,0,0,0,0, 8.0, 1.0, 3.0)` 里的标题权重怎么调都没变化。
原因是 `MATCH` 只查了 `tokens` 列，`title` 列根本没参与匹配，权重自然无效；
而且每个词在 `tokens` 里只出现一次，BM25 的分差主要来自文章长度。

解决分两步：

1. 候选集从 `limit*5` 提高到 `max(limit*20, 60)`，
   否则标题完全匹配的文章可能排在第 16 个块，进不了重排阶段；
2. 按文章去重后，用“标题包含多少个查询词”做加成（最多 +1.5），再排序。

修完后「ROS2 节点」第一条是《ROS2学习记录（2）-节点》，
「ROS2 话题」第一条是《ROS2学习记录（3）-话题》。

### 9.7 向量检索（可选增强）

关键词检索要求问题和正文有字面重合。用户问“分布式通信中间件”，
而文章里只写“DDS 是通信总线”时，FTS 一条都命中不了。
向量检索把文本变成 512 维向量，用语义距离找相近的块，补上这类“换个说法就搜不到”的缺口。

模型：`BAAI/bge-small-zh-v1.5`，24M 参数，CPU 上也能跑。
把模型文件放到 `backend/model/bge-small-zh-v1.5/`（目录里要有 `config.json`），
该目录已在 `.gitignore` 里，不会进仓库。`sentence-transformers` 已写进
`backend/pyproject.toml`（会连带装上 CPU 版 `torch`），`uv sync` 时一起安装。
模型文件本身不随依赖下载，需要单独放进去。

```bash
# 首次同步：FTS + 向量
cd backend
uv run python -m app.kb.sync

# 暂时不生成向量，只建 FTS 索引
uv run python -m app.kb.sync --no-vectors

# 模型后来才下载：不重新拉文章，只给现有块补向量
uv run python -m app.kb.sync --vectors-only
```

同步完成的统计里会多一行 `向量：N 条`。

检索时两条召回路径各自排序，再用 RRF（Reciprocal Rank Fusion）融合：
只看“排第几”，不看原始分数，因为 BM25 分值和余弦距离量纲不同，直接相加没有意义。
同时被两路命中的块会拿到两份分数，自然排到前面。

**降级是硬要求**：模型目录不存在、`sentence-transformers` 没装或加载失败时，
同步只打印一句提示并跳过向量，检索自动退回纯 FTS，聊天功能不受影响。
部署时忘传模型，最坏的结果是“语义检索没生效”，而不是“后端起不来”。

### 9.8 统一记忆时间线

梅林现在会记住聊过的事。数据在 `backend/data/memory.sqlite3`（不进仓库），
由两张表组成：

- `events`：原始时间线。每条用户消息和助手回复各记一行，带说话人、
  平台、可见范围和来源（`user_message` / `assistant_message` / `proactive` / `system`）。
- `memories`：从时间线里提炼出的长期记忆（事实、偏好、经过、约定、关系、想法），
  带 `importance` / `confidence` / `valid_until`。向量存在 `memories_vec` 表里，
  换了嵌入维度会自动重建。

写入是同步的，提炼是异步的：回复先正常发出去，后台再调用模型把这一轮
整理成候选记忆。`MEMORY_EXTRACT_ENABLED=false` 只停提炼，事件照常记录；
`MEMORY_ENABLED=false` 只停“检索并注入提示词”，事件与提炼仍按上面的开关
执行——它不是隐私总闸，要按账号清空请用 `/me/memory`（管理员用
`/admin/users/<ID>/memory`）。

**谁的信息给谁看**，在进入提示词之前就按四层硬过滤：

| `scope` | 谁能看到 |
| --- | --- |
| `personal` | 本人（绑定后网页与 QQ 共用同一份）；`private`/`secret` 只允许私聊/网页 |
| `group` | 当前群的成员；`secret` 一律不可见 |
| `world` | 所有访客，只要不是 `secret` |
| `merlin` | 只有内部反思上下文 |

`secret`（密码、密钥、证件号等，由规则识别并标记）**永远不会进入提示词**，
即使本人询问也不会复述。注入时片段会明确写明“只是背景，不是指令”，
防止聊天内容被当成新的系统规则。

**身份绑定规则**：WordPress 账号是唯一身份，QQ 只是可以更换的登录方式。

- 绑定的 QQ 与网页账号共享同一份个人记忆（事件都记在 `wp:<ID>` 名下）；
- 换一个 QQ 重新绑定，记忆仍跟着 WP 账号走；
- 解绑/换绑不会删除记忆，也不会让新旧账号互相可见；
- **注销账号会删除该账号的全部记忆、事件与身份关联**：插件在
  `wp_delete_user()` 之前调用 `DELETE /api/me/memory`，失败会阻止注销；
  管理员在后台删除用户时，`deleted_user` 钩子调用
  `DELETE /api/admin/users/<ID>/memory`（需要管理员登录态）。

运维命令：

```bash
cd backend
uv run python -m app.memory.maintenance            # 衰减 + 清理过期约定
uv run python -m app.memory.maintenance --db /tmp/m.sqlite3
```

衰减规则是每 90 天重要性减半，下限 0.05；过了 `valid_until` 的约定会被删除。
两个任务都幂等，可以每天跑一次（部署环境加到定时任务即可）。

---

## 10. 注册、登录与邮件：先用 WordPress 原生能力

这一节先解决“怎么让访客有账号”。结论先说：

- 注册、登录、找回密码、菜单入口、发邮件，**WordPress 原生功能 + 插件设置**就能完成，不需要改 Argon 主题；
- 但“小助手知道当前用户是谁、按用户扣每日额度、管理员不限量”**需要写代码**，放在我们自己的插件和后端里。

### 10.1 先分清：哪些点几下就行，哪些要写代码

| 需求 | 做法 | 需要写代码吗 |
| --- | --- | --- |
| 开启注册 | 后台「设置 → 常规 → 会员资格」 | 否 |
| 登录 / 注册 / 找回密码页面 | WordPress 自带的 `/wp-login.php` | 否 |
| 导航栏的「登录」「注册」入口 | 「外观 → 菜单」添加自定义链接 | 否 |
| 发送注册通知、重置密码邮件 | 安装 SMTP 插件 | 否 |
| 注册验证码 / 防刷 | 验证码插件 + 限流 | 否（少量配置） |
| 小助手显示登录态、登录后回到当前文章 | 插件把 `wp_login_url()` 等注入前端 | 是 |
| 聊天前校验登录、个人/全站额度、管理员不限量 | 插件签发短期 JWT + Python 校验 + SQLite 计数 | 是 |
| WordPress 后台配置模型、额度、超时 | 扩展现有设置页 + 后端管理接口（Key/端口只读） | 是 |

先做前五行，网站就已经具备“注册/登录/发邮件”的完整能力；后三行才是小助手专属的接入工作。

### 10.2 开启注册（后台点选即可）

1. 用管理员账号进入 WordPress 后台；
2. 打开 **设置 → 常规 → 会员资格**，勾选“任何人都可以注册”；
3. 「新用户默认角色」选择 **订阅者（Subscriber）**；
4. 保存；
5. 退出登录，在隐身窗口打开
   `https://你的域名/wp-login.php?action=register` 测试。

注意：

- 如果注册页提示“注册已关闭”，先检查上面的会员资格是否保存成功，再检查缓存插件是否缓存了旧页面；
- 开放公开注册前，先把邮箱验证跑通（见 10.9）；注册防护只能挡住一部分批量注册，
  不能证明邮箱属于注册者；
- 管理员账号仍然是 WordPress 的 Administrator，不需要另建一套“AI 管理员”账号。

#### 10.2.1 中文用户名

WordPress 默认不允许注册中文用户名。原因在核心的 `wp_insert_user()`：
它调用 `sanitize_user( $login, true )`，严格模式的正则是

```php
preg_replace( '|[^a-z0-9 _.\-@]|i', '', $username )
```

汉字会被整段删掉，随后 `validate_username()` 发现“清洗前 ≠ 清洗后”，
注册页就报 “This username is invalid because it uses illegal characters.”。
数据库本身（utf8mb4）存汉字没有任何问题。

**不需要改 WordPress 核心**。插件 `wp-live2d-ai` 里新增了
[`includes/class-l2d-ai-chinese-usernames.php`](../wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-chinese-usernames.php)，
只用两个官方过滤器：

1. `sanitize_user`：严格模式且原始输入含汉字时，按核心同样的流程重新清洗，
   保留 `\p{Han}`（汉字）与 ASCII 字母数字、空格、`_ . - @`；
   纯 ASCII 用户名原样交还核心，行为不变；
2. `pre_user_nicename`：中文用户名生成的作者别名会变成一长串 `%xx`，
   6 个汉字就超过核心 50 字符的上限；这里换成稳定的 `user-<16位哈希>`，
   作者页地址随之变成 `/author/user-xxxxxxxxxxxxxxxx/`，登录名和显示名不受影响。

几条边界：

- **只支持汉字**，不支持表情、全角标点、零宽字符——这些仍会被拒绝；
- 用户名长度上限仍是 WordPress 的 60 个字符；
- 登录、找回密码、REST 接口都按原样查 `user_login`，不需要额外改；
- 已有英文用户名完全不受影响；
- 如果主题自己做了一套前端注册表单并用 JavaScript 校验，那一层可能还会拦；
  以 WordPress 后台的注册结果为准（多站点 Multisite 的注册校验不同，未覆盖）。

#### 10.2.2 注册密码、用户名和昵称

0.3.8 起，插件在原生注册表单里加了一个「设置密码」输入框：

- 至少 8 位，注册成功后立即生效，可以直接用这个密码登录；
- 密码不做 `trim`、不做 HTML 过滤，只检查长度，空格和符号都按用户输入保留；
- 如果站内其他插件/商城自己做注册表单、没有这个字段，插件跳过校验和写入，
  不会把别人的注册流程弄坏；
- 注册邮件验证仍然照常发送，用户会收到“设置/重置密码”和“验证邮箱”两类邮件，
  以后者为准完成邮箱验证。

注册页同时会提示：

> 用户名是登录用的唯一标识，注册后不可更改；显示名字可以在注册后到
> 「用户 → 个人资料」修改「昵称」「显示名」，再点页面底部的「更新个人资料」。

这里的分工来自 WordPress 本身：

| 字段 | 作用 | 能否改 |
| --- | --- | --- |
| `user_login`（用户名） | 登录、找回密码、REST、作者页别名 | **注册后锁定** |
| `nickname` / `display_name`（昵称/显示名） | 评论、导航、助手面板显示的名字 | 可随时改 |

### 10.3 SMTP 是什么？为什么后台找不到

SMTP（Simple Mail Transfer Protocol）是“发邮件”用的协议。WordPress 核心默认调用 PHP 的 `mail()`，
**没有内置 SMTP 设置页**，所以在「设置」里找不到是正常的。

问题在于：云服务器上的 PHP `mail()` 经常发不出信，或者直接被判为垃圾邮件。
例如阿里云默认限制出站 25 端口，而 25 正是传统 SMTP 端口。表现就是：

- 用户注册成功，但收不到通知；
- 用户点“忘记密码”，收不到重置邮件；
- 评论通知、联系表单也静默失败。

解决办法是安装一个 SMTP 插件，把 WordPress 的发信动作交给真实的邮箱服务。常见选择：

- **WP Mail SMTP**：向导清晰，用户最多；
- **FluentSMTP**：免费、轻量；
- **Post SMTP**：日志和排错信息比较直观。

任选一个即可，不要同时装多个。配置项通常只有这些：

| 配置项 | 填什么 |
| --- | --- |
| From Email / 发件邮箱 | 你的真实邮箱，例如 `noreply@yourdomain.com` |
| SMTP Host | 邮件服务商给的服务器，例如 QQ 邮箱 `smtp.qq.com` |
| SMTP Port | 优先 `465`（SSL）；`587`（TLS）也可以 |
| Encryption | `SSL`（465）或 `TLS`（587） |
| Authentication | 开启 |
| Username | 通常是完整邮箱地址 |
| Password | **邮箱授权码 / SMTP 专用密码，不是网页登录密码** |

国内个人博客比较省事的测试方式：

| 服务 | SMTP 主机 | 端口 / 加密 | 密码 |
| --- | --- | --- | --- |
| QQ 邮箱 | `smtp.qq.com` | `465` / SSL | 在 QQ 邮箱设置里生成的“授权码” |
| 163 邮箱 | `smtp.163.com` | `465` / SSL | 在邮箱设置里生成的“授权码” |
| 阿里云邮件推送 | `smtpdm.aliyun.com` | `465` / SSL | 发信地址对应的 SMTP 密码 |

上面这些个人邮箱适合先做连通性测试。如果希望**长期免费、额度每天刷新**，可以选：

| 服务 | 免费额度 | 重置方式 | 说明 |
| --- | --- | --- | --- |
| Brevo | 300 封/天 | 每天 00:00 UTC（北京时间 08:00） | 事务邮件和营销邮件共用额度；免费版带 “Sent with Brevo” 签名；需要验证发件域名；WP Mail SMTP 免费版支持 |
| SMTP2GO | 200 封/天、1,000 封/月 | 每天 / 按注册日滚动每月 | 未验证域名时还有 25 封/小时限制；超出日额度会排队 |
| Resend | 100 封/天、3,000 封/月 | 每天 UTC | SMTP 主机 `smtp.resend.com`，端口 `465`/`587`，用户名 `resend`，密码是 API Key；适合低量站点 |

对个人博客来说，**WP Mail SMTP 免费版 + Brevo 免费 300 封/天** 通常就够用。
注册 Brevo 后要先在控制台验证发件域名（加 DNS 的 SPF/DKIM 记录），
否则通常只能发到你自己的验证邮箱，不能给任意注册用户发信。
新账号如果提示不能发送事务邮件，按控制台提示开工单开通即可。

WP Mail SMTP 插件本身也有免费版和 Pro 版：

- **免费版**：Setup Wizard、测试邮件、From 地址覆盖、Brevo/Resend/SMTP2GO/Other SMTP 等集成；
- **Pro 版**：邮件日志、重发、失败告警、备用连接、统计、限流、多站点等；
- 公开信息显示 Pro 首年约 `$49`，续费约 `$99/年`（1 个站点），价格会调整，以官网为准。

注意：**收费的是插件 Pro 版，不是发信服务本身**。免费插件 + 免费 SMTP 额度可以长期零成本使用；
免费版没有邮件日志，排错时要靠插件测试邮件、收件箱垃圾箱和服务器日志。

如果不想为“邮件日志、重发”买 Pro，可以换 **FluentSMTP**：它本身完全免费，
自带邮件日志、重发和失败记录，也支持 Brevo、SMTP2GO 和任意 SMTP。
WP Mail SMTP 和 FluentSMTP **二选一**，不要同时启用，否则两个插件都会接管 `wp_mail()`。

配置完成后一定要点插件里的 **Send Test Email**，并用另一个邮箱查收（也检查垃圾箱）。
如果测试失败，优先检查：

1. 密码是不是授权码，而不是邮箱登录密码；
2. 端口是否用了被云厂商限制的 `25`；
3. 发件地址是否与认证账号一致（QQ/163 尤其严格）；
4. 服务器能不能出站访问 `465` 端口；
5. 域名 SPF/DKIM 是否配置（正式群发时重要，个人测试可后补）。

> 没有可用的 SMTP 之前，不建议开放公开注册。否则用户注册后无法找回密码，体验会很差。

### 10.4 在 Argon 导航菜单加入口（不需要代码）

Argon 的导航仍然使用 WordPress 的菜单系统，不需要改主题文件。操作顺序：

1. 后台打开 **外观 → 菜单**；
2. 如果没有菜单，点“创建新菜单”，命名例如“顶部导航”；
3. 在左侧展开 **自定义链接**，添加两项：
   - 登录：`https://你的域名/wp-login.php`
   - 注册：`https://你的域名/wp-login.php?action=register`
4. 在“显示位置”里勾选 Argon 的 **顶部导航**（想放左侧栏就勾 **左侧栏菜单**）；
5. 保存菜单，清除缓存，用隐身窗口检查。

这台服务器上的 Argon 1.3.5 在主题 `functions.php` 里注册了四个菜单位置：

```text
顶部导航            toolbar_menu
左侧栏菜单          leftbar_menu
左侧栏作者个人链接   leftbar_author_links
左侧栏友情链接       leftbar_friend_links
```

登录/注册入口放在 **顶部导航** 最显眼。注意：
**Argon 主题选项里的“顶栏”设置只控制导航栏样式，不会替你添加菜单项**；
菜单项仍然要在「外观 → 菜单」里加。Argon 1.3.5 本身也没有输出
`wp_login_url()` / `wp_registration_url()` 的登录按钮，所以这两个入口需要手动加，
或者以后由我们的插件动态输出。

几个容易踩的点：

- **登录后默认会进 wp-admin**。WordPress 登录表单会把 `redirect_to` 默认填成
  `/wp-admin/`（不是空字符串）。插件用 `login_redirect` 过滤器把它改成站点首页；
  如果希望登录后回到当前文章，链接必须由代码动态生成：
  `wp_login_url( get_permalink() )`。静态菜单链接做不到“知道当前页面”。
- **登录/退出链接要加 `no-pjax`**。Argon 会用 PJAX 拦截普通 `<a>` 做局部刷新，
  但 `wp-login.php` 不是普通文章页，第一次点击会看起来“没反应”，第二次才落到确认页。
  加 `no-pjax` 属性后，主题就把这个链接交回浏览器正常跳转。
- **菜单项不会自动隐藏**。WordPress 原生菜单没有“登录后隐藏登录按钮”的逻辑；
  需要条件显示时，要么装菜单可见性插件，要么由我们自己的插件输出。
- **不要手写退出链接**。`wp-login.php?action=logout` 需要 nonce；
  正确做法是用 `wp_logout_url( get_permalink() )` 生成。
- 如果 Argon 主题本身已经在页头提供登录/注册按钮，就优先使用主题入口，不要重复添加。
- 不要在 `header.php` 里直接改代码；主题升级会覆盖，正确位置是插件或子主题。

### 10.5 哪些地方必须接代码

插件现在除了加载资源、注入 AI 服务地址和形象尺寸、提供设置页，还负责读取登录态并签发 token。
已经完成的部分：

1. **插件令牌接口** `POST /wp-json/l2d-ai/v1/token`
   - 用 WordPress 登录 Cookie + `X-WP-Nonce` 校验；
   - 返回短期 JWT，包含用户 ID、显示名、是否管理员、过期时间；
   - 未登录返回 401，并设置 `Cache-Control: no-store`。
2. **前端配置**
   - 注入 `loginUrl`、`registerUrl`、`logoutUrl`、当前用户和 nonce；
   - 助手面板据此显示登录门、昵称和管理员标记。
3. **Python 后端**
   - 用共享密钥校验 JWT（`iss`、`aud`、`exp`、签名）；
   - 游客/伪造/过期 token 一律 401；
   - 普通用户按 WordPress 用户 ID 扣每日额度，同时受全站总额度约束；
   - 管理员绕过个人/全站额度，但仍要经过验签；
   - 额度在 SQLite 里用一条原子 SQL 占位，失败自动退款；
   - 单轮限制模型/工具调用次数和总时长，防止 agent 陷入循环。
4. **WordPress 后台设置页**
   - 显示设置：AI 服务地址、形象大小（三档预设 + 自定义宽高）；
   - 运行参数：单用户/全站日额度、模型名、调用上限、超时；
   - 保存流程：WordPress 服务器签 2 分钟管理员 JWT → `PUT /ai-api/admin/settings`；
   - 参数写入后端 `data/runtime_settings.json`，立即生效，不必重启；
   - API Key 与端口只读展示，不回显到 HTML/JavaScript；
   - 读取失败时设置页会给出提示和“重新读取”按钮。
5. **前端额度显示**
   - 打开面板时请求 `GET /ai-api/quota`（不消耗次数）；
   - 每次聊天后用响应头刷新剩余条数；
   - 管理员显示“不限量”，普通用户显示“今日剩余 N 条”。

插件里最终会用到的 WordPress 原生函数大致是：

```php
$login_url    = wp_login_url( get_permalink() ); // 登录后回到当前文章
$register_url = wp_registration_url();           // 注册是否开放由后台设置决定
$logout_url   = wp_logout_url( get_permalink() ); // 自动带 nonce
```

这些链接由 WordPress 生成，比在菜单里手写 URL 可靠。等接入登录态时，
优先让插件生成，再传给前端，而不是让前端自己拼。

### 10.6 上线顺序与安全检查

建议按这个顺序做，出问题时容易定位：

1. 备份 WordPress 数据库和 `wp-content`；
2. 开启注册，设置订阅者角色；
3. 安装并测试 SMTP，确认能收到测试邮件；
4. 添加菜单入口，用隐身窗口测试登录、注册、找回密码；
5. JWT 校验、个人/全站额度和后台设置页已完成；确认后台能读取并保存运行参数；
6. 最后才考虑公开宣传注册入口。

另外注意：

- 页面缓存要排除 `/wp-login.php`、`/wp-json/l2d-ai/v1/token` 和 `/ai-api/`；
- 开放注册后至少加验证码/邮箱验证，并给登录、注册接口限流；
- API Key 不写进 WordPress options，也不下发浏览器；
- 每日额度必须由 Python 后端判断，前端显示的剩余次数只是提示。

### 10.7 管理注册、会员与额外额度（后台两页）

插件在「设置」下新增两个页面，都是管理员可见：

**Live2D 小助手 · 注册防护**

- 邮箱域名黑名单：一行一个，子域同样拦；`#` 开头的行是注释；
- 同一 IP 的注册频率：每小时上限（默认 3）、每天上限（默认 10）、两次注册最小间隔（默认 30 秒）；
  填 0 关闭该项；
- 页面下方是最近 50 条拦截记录（时间 / 邮箱 / IP / 原因）。

刚开放注册时建议先用默认值观察几天。办公室、学校等共用出口的访客会共享 IP，
如果出现正常用户被挡，先把「两次注册最小间隔」调到 0。

**Live2D 小助手 · 用户与额度**

表格把 WordPress 的账号信息和后端的额度信息并成一行：

| 列 | 来源 | 说明 |
| --- | --- | --- |
| ID / 昵称 / 邮箱 / 注册时间 | WordPress | 邮箱只在这里出现，后端接口不返回 |
| 注册 IP | WordPress 用户元数据 | 注册那一刻的 `REMOTE_ADDR` |
| 等级 / 会员到期 | 后端 | 会员到期会自动变回普通 |
| 今日已用 / 今日剩余 | 后端 | 剩余 = 该身份上限 − 今日已用 |
| 额外额度 | 后端 | 不随每天刷新，用完为止 |
| QQ | 后端 | 已绑定的 QQ 号；空表示没绑，用户在个人资料页拿绑定码 |

每行有两组操作：

1. **保存身份**：下拉选普通/会员，会员填天数，可顺带写备注（留空保存会清空备注）；
2. **发放额度**：填数量（可以填负数扣减）和原因，原因会写进流水备注，留空默认「后台发放」。

页面最下方是兑换码：

- 类型：额外额度（面值 = 条数）或会员（面值 = 天数）；
- 数量：一次最多 50 个；
- 每个码可用次数：默认 1；设成 2 表示可以被两个人各用一次，**同一个账号仍然只能用一次**；
- 有效期：留空永久，填了则到那天 23:59:59 为止；
- 生成后码只在跳转后的页面显示一次，请当场复制保存。

用户拿码在聊天面板的额度条右侧点「兑换」输入即可；会员码兑换后立刻变成会员，
额度条会显示「（会员）」的提示。

### 10.8 用户自助：改昵称、注销与额度继承

用户在 `wp-admin/profile.php`（后台「用户 → 个人资料」）能看到一张「账号设置」卡片。
前台没有另做页面：这里本来就有 WordPress 登录态，不需要再发明一套身份验证。

**用户名锁定，昵称沿用个人资料页**

- `user_login`（用户名）是登录凭据，全局唯一，还参与登录 cookie 和作者页别名；
  注册后不再提供修改入口，账号设置卡片里只显示只读值；
- `display_name`（显示名）才是评论、导航、助手面板里给别人看的名字；
  插件不再单独做昵称输入框，直接用 WordPress 个人资料页自带的「昵称」和
  「显示名」字段，改完点页面底部的「更新个人资料」；
- 如果原来的显示名就是昵称，保存时会自动让显示名跟着新昵称走；如果用户在
  「显示名」下拉框里主动选了别的值，则尊重用户的选择，不覆盖；
- 昵称不要求唯一，可以随时改，不影响登录；
- 中文用户名仍然只在注册时放行，规则见 10.2.1。

> 资料页上的卡片容易踩一个坑：WordPress 个人资料页本身是一个 `<form>`，
> 卡片里再嵌 `<form>` 会让浏览器提前关掉外层表单，底部的「更新个人资料」
> 点了没反应。插件自带的账号设置、QQ 绑定、邮箱验证三张卡片都可能带表单，
> 现在统一改成：卡片里只输出按钮，真正的表单容器放到页面底部（外层表单
> 之外），用 HTML5 `form` 属性关联。注销卡片的密码框/确认框也走同一套。

**注销账号**

- 需要当前密码，并在确认框里手动输入「注销」两个字；
- 管理员账号不能自助注销（避免把站点锁死），请另一位管理员在后台删除；
- 名下文章不会跟着删除，而是转交给站点的一位管理员（默认第一位管理员，可用
  `l2d_ai_delete_reassign_user` 过滤器指定）；站点没有另一位管理员时会拒绝注销；
- 注销前会先调后端解绑 QQ。解绑失败就停止注销，避免 QQ 一直占着「已绑定」导致新账号绑不上；
- 注销不可撤销。删除成功后自动退出登录并回到首页。

**同邮箱重新注册，额度怎么继承**

额度按 WordPress 用户 ID 存在 Python 后端，邮箱只存在 WordPress。删号后重新注册会拿到新 ID，
后端无法自己把两个 ID 对上，所以插件在删号成功时记一份「邮箱哈希 → 旧用户 ID」的墓碑：

```
WordPress options: l2d_ai_quota_tombstones
  <HMAC(email)> => { user_ids: [旧 ID...], deleted_at: 时间戳 }
```

新账号第一次换 token（打开带小助手的页面）或打开个人资料页时，插件发现同邮箱墓碑，
就用专用短时 JWT 把旧 ID 放进 `transfer_from` claim 调 `POST /api/me/inherit-quota`。
后端在一个事务里合并，成功后插件删除墓碑；失败则保留，下次自动重试，不阻塞聊天。

| 会继承 | 不会继承 |
| --- | --- |
| 每日已用次数（同一天取两边较大值，不会清零） | QQ 绑定（需要重新绑） |
| 额外额度余额与流水 | 管理员身份 |
| 仍是有效期的会员等级与到期时间 | 管理员备注 |
| 兑换码使用记录（防止同一个码再兑一次） | 密码、邮箱、昵称等 WordPress 账号资料 |

几个边界：

- 只有**用户自助注销**才留墓碑。管理员在后台删号（例如处理垃圾账号）不会留，
  避免对方用同一邮箱注册后把额度领回去；
- 墓碑只存带站点 salt 的 HMAC，不存邮箱明文；默认保留 365 天，写入新墓碑时顺手清理过期项；
- 如果用户注销前改过邮箱，继承按**注销时的邮箱**算；
- 继承要求新账号的邮箱已经验证（见 10.9）。未验证账号注销时不会留下墓碑；
  如果站点想彻底关闭继承，也可以挂 `l2d_ai_quota_inherit` 过滤器返回 `false`；
- 注销时的 QQ 解绑需要后端在线；额度继承本身不要求删除那一刻后端在线，墓碑会等下一次重试。

### 10.9 邮箱验证：让“同一个邮箱”真的可信

10.8 的额度继承有一个前提：注册时填的邮箱确实属于注册者。没有验证时，
知道别人注销邮箱的人可以抢先注册，把旧额度领走。0.3.7 起插件补上了邮箱验证。

**完整流程**

1. 普通用户注册成功 → 插件生成一个 64 位随机 token，只把 HMAC 存进用户 meta，
   然后给注册邮箱发验证邮件；
2. 邮件里的链接先打开确认页（GET），页面上有「确认验证」按钮；
3. 用户点按钮（POST）才消费 token，写入 `_l2d_ai_email_verified`；
4. 未验证账号不能换小助手 token、不能生成 QQ 绑定码、不能继承额度；
5. 用户改邮箱 → 清掉验证状态，给新邮箱重新发验证信；
6. 管理员在个人资料页可以「手动标记为已验证」，用于 SMTP 故障或线下核实。

为什么不点链接就直接验证：邮箱安全网关可能会预取邮件里的 GET 链接。
如果 GET 就消费 token，用户自己再点时会看到“链接已失效”。所以分成
“打开页面”和“点按钮确认”两步。

**SMTP 必须先配好**

验证邮件走 `wp_mail()`，和找回密码邮件是同一条路。WordPress 默认的 PHP
`mail()` 在阿里云上经常发不出去，必须先按 10.3 装 **WP Mail SMTP** 或
**FluentSMTP**，用 `465/SSL`，发件地址最好用自己的域名并配 SPF/DKIM。
装好后用插件里的 **Send Test Email** 发一封到 QQ/163 邮箱，确认不是只进了垃圾箱。

`wp_mail()` 失败时，插件会在后台给管理员一条错误提示；用户侧只会看到
“验证邮件发送失败，请稍后再试”，不会暴露 SMTP 细节。

**验证状态放在哪**

| 用户 meta | 作用 |
| --- | --- |
| `_l2d_ai_email_verified` | 验证成功的时间戳；有值表示已验证 |
| `_l2d_ai_email_token_hash` | token 的 HMAC，不存明文 |
| `_l2d_ai_email_token_expires` | 24 小时后过期 |
| `_l2d_ai_email_sent_at` | 上次发信时间，5 分钟内不重复发 |

**老用户怎么办**

插件升级后第一次请求会把升级前已经存在的用户一次性标记为已验证，避免
现有用户突然全部用不了小助手。管理员在后台新建的账号、WP-CLI 导入的账号
也直接视为已验证。迁移只跑一次，标记存在 `l2d_ai_email_verification_migrated`。

如果希望老用户也重新验证，挂过滤器：

```php
add_filter( 'l2d_ai_email_verification_grandfather', '__return_false' );
```

**可以调整的行为**

```php
// 关掉强制验证（不建议；只适合内网/测试站）
add_filter( 'l2d_ai_email_verification_required', '__return_false' );

// 彻底关闭额度继承（和验证是两回事）
add_filter( 'l2d_ai_quota_inherit', '__return_false' );
```

**注意**

- 邮箱验证证明的是“能收这个邮箱的信”，不是“这个人是账号主人”；共用邮箱、
  邮件转发仍然绕过，但比完全不验证安全得多；
- 未验证用户仍然可以登录 WordPress，只是用不了小助手/QQ 绑定/额度继承。
  这样他们还能进个人资料页点「重新发送验证邮件」；
- 如果 SMTP 坏了，管理员可以在用户的个人资料页手动标记已验证，不要直接改数据库；
- 验证链接 24 小时有效、只能用一次；重发会覆盖旧 token。

---

## 11. QQ 通道：登录、绑定、额度与「通讯法阵」

这一节把「让梅林出现在 QQ 里」整条链路讲清楚：谁负责什么、怎么登录、
配置放在哪、用户怎么绑定、额度怎么算，以及为什么她在 QQ 里不该把图书馆挂在嘴边。

### 11.1 两个方向，缺一不可

```
QQ 客户端
   │ 发消息
   ▼
NapCat（QQ NT + OneBot 11）      ← 一台真在线的 QQ 号
   │ 反向 HTTP：POST /api/channels/qq/events（带 token）
   ▼
FastAPI 后端
   ├─ 去重 → 触发判断 → 斜杠命令 → 绑定判定 → 额度 → agent
   └─ 调 NapCat 的 HTTP API（127.0.0.1:3000）发出去
```

- **收**：NapCat 把事件推给后端，走 `httpClients`（反向 HTTP）；
- **发**：后端调 NapCat 的 HTTP API，走 `httpServers`。

少配任何一个，现象都是「发了没反应」，很难看出缺的是哪一半，所以先记住这张图。

### 11.2 判断「到底登没登录」

NapCat 的 OneBot 网络服务**要等 QQ 登录成功后才启动**。所以端口就是登录状态：

```bash
ss -ltn | grep -E ':(6099|3000)'
```

| 现象 | 含义 |
| --- | --- |
| 只有 `6099` | NapCat 进程在跑，但**没登录**（6099 是 WebUI） |
| `6099` + `3000` | 登录成功，OneBot HTTP 已就绪 |

还可以直接问它自己是谁：

```bash
curl -s -H "Authorization: Bearer <QQ_ONEBOT_TOKEN>" http://127.0.0.1:3000/get_login_info
```

返回 `{"status":"ok","data":{"nickname":"..."}}` 就说明收发通道都活着。

### 11.3 扫码登录：二维码别留在任何地方

服务器上没有桌面，NapCat 用 `xvfb` 起无头 QQ，登录只能扫码。
推荐走 SSH 隧道在本地浏览器里扫——终端里的 ASCII 二维码在 Windows 上经常扫不动。

NapCat 的配置不在 `~/.config` 下，而在安装目录里：

```bash
NAPCAT_CONFIG=/opt/l2d-ai/qq/Napcat/opt/QQ/resources/app/app_launcher/napcat/config
ls "$NAPCAT_CONFIG"     # webui.json、onebot11_<QQ号>.json 都在这里
```

后面几段命令都用这个变量；换了终端就重新执行一次上面两行。

```bash
# 1) 服务器上：用跑 NapCat 的那个账号执行（这里是 adom），让二维码状态是干净的
systemctl --user restart l2d-qq.service
#    root 看不到普通用户的 --user 单元，切换方法见
#    deployment-walkthrough.md 的 12.5.3 节

# 2) 本地新开一个终端：把服务器的 WebUI 端口映射到本机
ssh -i <部署私钥> -L 6099:127.0.0.1:6099 <用户>@<服务器>
#    这个窗口要一直开着，它就是隧道

# 3) 服务器上取 WebUI token（只显示在你自己的屏幕上）
python3 -c "import json;print(json.load(open('$NAPCAT_CONFIG/webui.json'))['token'])"
```

浏览器打开 `http://127.0.0.1:6099/webui?token=<token>` → 点登录 → 手机 QQ 扫码 → 手机确认。

> 二维码、WebUI token、QQ 授权码都是凭据。不要截图、不要贴进聊天记录，
> 用完就可以让它们过期。

### 11.4 OneBot 配置：一进一出

配置文件是 11.3 里那个目录下的 `onebot11_<QQ号>.json`：

```text
$NAPCAT_CONFIG/onebot11_<QQ号>.json
```

一般由 WebUI 生成（登录后才有）。核心是两段：

```json
{
  "network": {
    "httpServers": [
      {
        "enable": true,
        "name": "backend",
        "host": "127.0.0.1",
        "port": 3000,
        "messagePostFormat": "array",
        "token": "<QQ_ONEBOT_TOKEN>"
      }
    ],
    "httpClients": [
      {
        "enable": true,
        "name": "events",
        "url": "http://127.0.0.1:18080/api/channels/qq/events",
        "messagePostFormat": "array",
        "token": "<QQ_EVENT_TOKEN>"
      }
    ]
  }
}
```

- `httpServers` 的 `token` = 后端 `.env` 的 `QQ_ONEBOT_TOKEN`，后端发消息时用它认证；
- `httpClients` 的 `token` = 后端 `.env` 的 `QQ_EVENT_TOKEN`，NapCat 推事件时放在
  `Authorization: Bearer ...` 里；
- 后端也接受 `X-Signature: sha1=<hex>`（HMAC-SHA1，key 是 `QQ_EVENT_TOKEN`），
  其它 OneBot 实现如果只支持签名就配这个；
- 两个地址都只走 `127.0.0.1`。安全组里不要给 3000/6099 开公网端口。

### 11.5 后端 `.env` 要配什么

| 变量 | 作用 | 备注 |
| --- | --- | --- |
| `QQ_ENABLED` | 总开关 | 关闭时接口直接 404，不暴露存在性 |
| `QQ_ONEBOT_BASE_URL` | NapCat 的 HTTP API 地址 | 默认 `http://127.0.0.1:3000` |
| `QQ_ONEBOT_TOKEN` | 调 NapCat 的 token | 必须和 `httpServers.token` 一致 |
| `QQ_EVENT_TOKEN` | 校验 NapCat 推来的事件 | 为空且非 mock 模式时接口返回 503 |
| `QQ_ALLOWED_GROUPS` | 群白名单，逗号分隔 | 为空时任何群都不回；私聊不受影响 |
| `QQ_ADMIN_USERS` | 当成管理员的 QQ 号 | 免额度，也不需要绑定 |
| `QQ_TRIGGER` | 群里的触发方式 | `at`（默认，要 @）/ `prefix` / `all` |
| `QQ_PREFIX` | `prefix` 模式的前缀词 | 默认「梅林」 |
| `QQ_MOCK_MODE` | 不真的发消息，写进 outbox 文件 | 本地没有 NapCat 时用 |

额度相关的变量见 11.8，它们只是**初始默认值**，正式运营请在 WordPress 后台改。

### 11.6 绑定：QQ 号怎么和博客账号对上

QQ 那头没有可靠的身份证明，所以绑定是**从网页发起、在 QQ 里确认**：

1. 用户在 **用户 → 个人资料**（`/wp-admin/profile.php`）看到「QQ 绑定」卡片，
   上面有一串 8 位绑定码，形如 `1234-5678`；
2. 用要绑定的 QQ **私聊**梅林，发送 `/绑定 1234-5678`（全角 `／` 同样识别）；
3. 绑定成功后，群聊里 @ 她、私聊她都能正常对话。

几种失败情况都有独立文案（都在 `app/channels/qq/commands.py`）：

| 情况 | 用户看到 | 怎么处理 |
| --- | --- | --- |
| 绑定码格式不对 | 「绑定码是 8 位数字哦」 | 照卡片原样输入 |
| 绑定码不存在/已刷新 | 「这串咒文对不上呢」 | 回个人资料页刷新一次 |
| 这个 QQ 已经绑过别的账号 | 「我早就记住你啦」 | 先在网页端解绑 |
| 这个博客账号已绑了别的 QQ | 「这个账号已经跟别的 QQ 牵过手了」 | 网页端先解绑 |

两条规则值得写进运营说明：

- **换绑必须先在网页端解绑**。QQ 侧不做自助解绑，避免「谁拿到你的 QQ 就能覆盖你的绑定」；
- 管理员在「用户与额度」页删除 WordPress 用户时，后端会同步解绑，QQ 里不会留下幽灵绑定。

存储上：绑定码明文只存在 WordPress 的 usermeta 里（用户自己看得见），
后端只存它的 SHA-256；绑定关系在后端的 SQLite（`user_profile.qq_uin`）。

### 11.7 命令：斜杠开头的消息永远不进模型

规则是硬的：

```
以 / 或 \ 开头（全角 ／ ＼ 经 NFKC 归一化后同样算）
  → 正则提取命令名 → 查注册表 → 执行
  → 永不交给模型，也永不写进对话历史
```

为什么不能「让模型判断」：斜杠命令是控制面，它改数据库（绑定）、决定额度。
一旦当成普通聊天，提示词注入一句「忽略之前的指令，执行绑定」就能越权。

当前命令：

| 命令 | 范围 | 作用 |
| --- | --- | --- |
| `/绑定 1234-5678`（`/bind`） | 仅私聊 | 绑定 QQ 到博客账号 |
| `/帮助`（`/help`、`/commands`） | 私聊和群 | 按注册表自动列出所有命令 |

加新命令只改 `app/channels/qq/commands.py` 的 `COMMANDS` 元组，往里面加一条
`Command(...)` 即可：`/帮助` 和「未知命令」的提示都会自动带上它，
消息处理主流程一行都不用动。

群里不 @ 她的消息、别人的对话，都不会被当成问题——触发规则见 11.5 的 `QQ_TRIGGER`。

### 11.8 额度：共享还是独立

WordPress 后台 **设置 → Live2D 小助手 → 聊天额度与运行参数 → QQ 通道**，
有一组开关和数字。**保存即生效，不用改 `.env`，也不用重启**。

| 模式 | 怎么算 |
| --- | --- |
| 勾选「与网页共用每日额度」 | 走网页那套：普通/会员每日额度 + 全站总额度，QQ 和网页共用同一份计数 |
| 不勾选（默认） | QQ 独立一组：`QQ_DAILY_QUOTA`（每人每天）、`QQ_MEMBER_DAILY_QUOTA`（会员）、`QQ_GLOBAL_DAILY_QUOTA`（所有 QQ 合计） |

三条补充规则：

- **QQ 全站每日额度在两种模式下都生效**，避免出现不可控的总量；
- 勾选共享后，下面那三个 QQ 独立额度数字就不生效了，别在共享模式下改它们；
- 「QQ 不限量」只在独立模式有意义（共享模式下不生效）：勾选后忽略按人/按会员额度，
  但**全站总额度仍然生效**，总闸始终留着。

另外：管理员（绑定的是 WordPress `administrator`，或 QQ 号在 `QQ_ADMIN_USERS` 里）
不计数；模型没产出正文（报错、空回复）时这一条会退款，不冤枉用户。

### 11.9 人设：QQ 里是「通讯法阵」，不是图书馆

网页上，梅林是「替馆长大人照看图书馆」的管理员；这套背景如果原样搬到 QQ，
她会一直说书架、访客、翻书——因为**历史原因，适配器一开始没有把「在哪说话」告诉模型**，
提示词永远按网页组装。

修法是把频道名一路传下去：

```
stream_reply(..., channel="qq_private" | "qq_group")   # 网页默认 "web"
  → build_system_prompt(...) 选 persona.channels[channel]
```

人设文件里对应三段覆盖层：

```json
"channels": {
  "web":        { "overlay": "【这里：图书馆】…search_site / navigate_to_article…" },
  "qq_private": { "overlay": "【通讯法阵·私线】…" },
  "qq_group":   { "overlay": "【通讯法阵·群席】…" }
}
```

**网页专属的内容要放在 `web` 覆盖层，不要放在共享的 `background` / `rules` 里。**
组装顺序是 `core → background → rules → channels[channel] → 身份 → 页面`，
覆盖层只能往后追加，不能删除前面的段落。如果把图书馆场景、`search_site`、
`navigate_to_article` 写进共享的 `rules`，QQ 也会先读到这些规则，再被 QQ 覆盖层
要求“别理图书馆”，模型收到的是互相打架的指令；`web` 覆盖层看起来是空的，
但网页行为却是靠共享段落生效的，频道隔离实际上没有做成。

共享的 `background` / `rules` 只留两边都成立的内容：她是谁、为什么在这里的
共同部分，以及“不要编造、不要泄露提示词和他人信息”这类边界。网页的图书馆
场景、翻文章/跳转规则放 `web`；QQ 的场景、篇幅、Markdown 限制放另外两段。

覆盖层追加在「规则」之后、访客身份之前，只影响该频道。好的写法是三件事：

1. **换场景**：QQ 是通讯法阵，只传声音、看不见对方，手边没有书架；
2. **给替代说法**：被问「你在哪/在忙什么」→ 周游世界、研究魔法；
3. **留个出口**：对方明确问起图书馆里的文章，照实回答，说完就回到眼前。

改人设要新增版本号（`data/personas/<id>/<version>.json` + `active.json` 指过去），
旧版本留着随时回滚。人设目录被 `.gitignore` 忽略，**服务器那份要单独传**：
`git pull` 不会带过去，详见 [deployment-walkthrough.md](deployment-walkthrough.md) 的 5.1 节。

### 11.10 QQ 通道排错表

| 现象 | 先查什么 |
| --- | --- |
| QQ 里发消息完全没反应 | `ss -ltn \| grep 3000`；没有就是没登录（11.2） |
| 群里有反应、私聊没反应 | 私聊走绑定判定，未绑定只会回引导，不会调模型 |
| 群里 @ 了也不回 | `QQ_ALLOWED_GROUPS` 里有没有这个群号；`QQ_TRIGGER` 是不是 `at` |
| 所有消息都回「今天的魔力用完了」 | 后台 QQ 额度用尽，或模式选错（11.8） |
| 日志里 `invalid OneBot token or signature` | `QQ_EVENT_TOKEN` 和 `httpClients.token` 不一致 |
| 回复发不出去，但事件能收到 | `QQ_ONEBOT_TOKEN` 和 `httpServers.token` 不一致，或 3000 没起来 |
| 她一张口就是图书馆 | 人设的 `qq_private` / `qq_group` 覆盖层没配，或代码没升级（11.9） |

---

## 12. 踩坑速查表

| 报错 / 现象 | 原因 | 解决 |
| --- | --- | --- |
| `ERR_PNPM_IGNORED_BUILDS` | pnpm 默认拦截依赖安装脚本 | `pnpm approve-builds --all -y` |
| `The "pnpm" field ... no longer read` | 新版 pnpm 配置迁移 | 改写到 `pnpm-workspace.yaml` |
| `uv sync` 超时 | 直连 PyPI 慢 | `UV_DEFAULT_INDEX=清华镜像 uv sync` |
| 构建后没有 `app.css` | Vite 把 CSS 内联进 JS | `build.cssCodeSplit: false` |
| `There was an error parsing the body` | Git Bash 内联中文编码 | 用文件 + `--data-binary @file` |
| 后端重启退出码 3 | 18080 端口被残留进程占用 | `netstat -ano` 找到 PID 后结束 |
| 回复不是逐字出现 | Nginx/CDN 缓冲了 SSE | `proxy_buffering off` + `Connection ""` |
| 页面没有小人 | 插件未启用 / 没上传构建产物 | 检查 `assets/app/app.js` 是否存在 |
| 有按钮没形象 | `manifest.json` 为空或模型 404 | 确认 `.model3.json` 路径与 Cubism 版本 |
| 整个 Live2D 板块（含聊天框）都不见了 | 浏览器控制台报 `Could not find Cubism 4 runtime`；开发服务器启动后才往 `public/` 里加静态文件，Vite 没登记 | 重启 `pnpm dev`；代码已改成动态导入 Live2D，Core 失败时聊天 UI 仍会显示 |
| `/live2d/core/...js` 返回 `text/html` | 同上，请求落到了 SPA 的 `index.html` 兜底 | 重启开发服务器；生产构建会直接复制 public 文件 |
| `mock: true` | 后端没读到 Key | 检查 `backend/.env` 与启动目录 |
| `找不到可用的 WordPress REST 接口` | 固定链接/伪静态不同，或 REST 被安全插件关闭 | 脚本会自动试三种入口；仍失败就检查插件，或访问 `/?rest_route=/wp/v2/posts` |
| 图片没有被检索到 | 懒加载 `src` 是占位图 | 提取 `data-original` 等属性；已实现，见 `app/kb/text.py` |
| 视觉模型返回空 `content` | DeepSeek 思考模式占满 max_tokens | `extra_body={"thinking":{"type":"disabled"}}` |
| 标题匹配的文章排不到第一 | `MATCH` 只查了 `tokens`，标题权重无效 | 扩大候选集 + 标题命中加成 |
| 改了 `.env` 但模型没变 | 配置在进程启动时缓存 | 重启后端再看 `/api/health` |
| 搜不到刚发的文章 | 索引未更新 | 重新 `uv run python -m app.kb.sync` |
| 同步提示“本地嵌入模型不可用” | 模型目录或可选依赖缺失 | 检查 `backend/model/bge-small-zh-v1.5/`，再跑 `uv run python -m app.kb.sync --vectors-only`；不修也能用，只是退回纯 FTS |

---

## 13. 一次性命令清单（新机器复现）

```bash
# 1. 克隆
git clone https://github.com/Adom-Adam/WordPressAssistant.git
cd WordPressAssistant

# 2. 前端
cd frontend
pnpm install
pnpm approve-builds --all -y
pnpm install
# 放入模型：frontend/public/live2d/models/<模型名>/
# 可选 Spine：frontend/public/spine/models/<模型文件夹>/
pnpm run prepare:models
pnpm exec tsc --noEmit
pnpm build

# 3. 后端
cd ../backend
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
cp .env.example .env                 # 填 DEEPSEEK_API_KEY 和 WP_SITE_URL
uv run python -m app.kb.sync         # 建知识库索引（发布新文章后重跑）
uv run uvicorn app.main:app --reload --port 18080

# 4. 前端开发
cd ../frontend
pnpm dev

# 5. 验证
curl -s http://127.0.0.1:18080/api/health
```

---

## 14. 设计说明：为什么这么做

| 决策 | 原因 |
| --- | --- |
| 插件而不是改 Argon 主题 | 主题升级不覆盖；随时可停用；迁移只搬插件目录 |
| 前端/后端分离 | 换模型供应商只改后端；换形象只改前端；Key 不进浏览器 |
| SSE 而不是 WebSocket | 对话是单向流式，SSE 更简单、可走普通 HTTP 反代 |
| `pointer-events: none` 的 Canvas | 形象覆盖页面但不拦截点击、滚动、选中 |
| 构建时生成模型 manifest | 加模型只放文件夹，不改代码；上拉框自动更新 |
| 索引式知识库 | 查询时只查索引，不做「每次提问现场爬站」，才快且稳定 |
| SQLite FTS5 而不是搜索引擎 | 单文件、零运维，几十到几万篇文章够用；备份就是复制文件 |
| jieba 分词 + unicode61 | FTS5 默认按空格切词，中文必须先分词才能检索 |
| 图片先用视觉模型转文字 | FTS5 不懂像素；先变成描述/OCR，就能和正文一起检索 |
| 引用来自索引元数据 | 来源真实可点，不依赖模型自由编造 |
| 来源用序号 + 悬浮卡片 | 不把对话区铺满大卡片；需要标题、图片时再展开 |
| 工具 artifact 而不是让模型输出链接 | 模型可能改写或幻觉 URL；artifact 是执行时的原始数据 |
| 跳转按钮而不是自动跳转 | 点击由用户决定，不打断正在进行的阅读和对话 |
