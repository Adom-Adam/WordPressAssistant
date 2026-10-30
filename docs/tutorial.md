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
WP_SITE_URL=https://374969782.xyz
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
- 开放公开注册前，建议同时准备邮箱验证或验证码，否则很容易收到垃圾账号；
- 管理员账号仍然是 WordPress 的 Administrator，不需要另建一套“AI 管理员”账号。

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
| QQ | — | 绑定功能尚未实现，先占一列 |

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

---

## 11. 踩坑速查表

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

---

## 12. 一次性命令清单（新机器复现）

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

## 13. 设计说明：为什么这么做

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
