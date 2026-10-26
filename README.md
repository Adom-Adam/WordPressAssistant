# WordPress Live2D AI 小助手

给 WordPress（Argon 主题）博客加一个住在左下角的 Live2D AI 助手的完整工程：
**前端只负责展示与交互，AI 逻辑全部在后端**，通过一个轻量 WordPress 插件接入，不改主题、不动 WordPress 核心。

```
浏览器
  ├─ Live2D 形象 + 对话/语音/换模型 UI      ← frontend（Vite + TypeScript + PixiJS）
  └─ fetch /ai-api/chat （SSE 流式）
        └─ Python AI 服务                  ← backend（FastAPI + LangChain + DeepSeek）
              ├─ 站内知识库（SQLite FTS5，含图片描述）
              └─ 检索 / 来源卡片 / 跳转工具
```

## 目录

| 路径 | 作用 |
| --- | --- |
| [frontend/](frontend/) | Live2D 形象、悬浮控件、对话面板、语音输入 |
| [backend/](backend/) | FastAPI + LangChain agent（DeepSeek），流式对话与工具调用；[学习导读](backend/README.md) |
| [wordpress-plugin/wp-live2d-ai/](wordpress-plugin/wp-live2d-ai/) | WordPress 插件：加载构建产物、注入配置、设置页 |
| [docs/architecture.md](docs/architecture.md) | 技术架构与知识库/工具设计 |
| [docs/knowledge-base.md](docs/knowledge-base.md) | 知识库原理：FTS5、中文分词、图片理解、检索与引用 |
| [docs/tutorial.md](docs/tutorial.md) | 教学教程：全部命令、搭建顺序、注册/登录/SMTP 与踩坑记录 |
| [docs/deploy.md](docs/deploy.md) | 部署概览（简版：宝塔 + Nginx + systemd） |
| [docs/deployment-walkthrough.md](docs/deployment-walkthrough.md) | **从零部署实操**：服务器手动设置清单、JWT 密钥对齐、systemd/Nginx、插件上传与验证、排查 |

## Live2D 模型放哪里

把 **Cubism 3/4** 模型（含 `.model3.json` 的那套文件）放到：

```
frontend/public/live2d/models/<模型文件夹>/
```

例如：

```
frontend/public/live2d/models/
└── hiyori/
    ├── hiyori.model3.json      ← 必需
    ├── hiyori.moc3
    ├── hiyori.2048/texture_00.png
    └── model.json              ← 可选：显示名/缩放
```

`model.json` 可选内容：

```json
{ "name": "梅林", "scale": 1.0, "offsetX": 0, "offsetY": 0 }
```

放好后 `pnpm dev` 或 `pnpm build` 会自动生成 `manifest.json`，页面上的「更换模型」上拉框会列出全部模型。
**不要**用 Cubism 2 的 `.model.json` 格式，本项目只带 Cubism 3/4 运行时。

## 本地开发

需要：Node 20+、pnpm、Python 3.11+、uv。

### Windows 一键启动

双击根目录的 `dev.bat` 即可启动后端和前端、等待健康检查通过并打开浏览器。
第一次使用如果缺少 `backend/.env`，脚本会从 `.env.example` 复制一份。

也可以带参数使用：

```bat
dev.bat            :: 启动前后端
dev.bat menu       :: 打开菜单（同步知识库、构建插件、安装依赖等）
dev.bat backend    :: 只启动后端
dev.bat frontend   :: 只启动前端
dev.bat sync       :: 同步知识库，可加 --no-vision
dev.bat build      :: 构建 WordPress 插件资源
dev.bat install    :: 安装 / 更新依赖
dev.bat doctor     :: 检查 uv、pnpm、.env、索引和端口
```

> 中文界面由 `scripts/dev.ps1` 输出；`dev.bat` 只负责设置 UTF-8 代码页并调用它。
> 两个服务会各自打开一个命令行窗口，关闭窗口即可停止服务。

**1. 后端**

```bash
cd backend
cp .env.example .env          # 填入 DEEPSEEK_API_KEY；不填则自动 mock
uv sync
uv run uvicorn app.main:app --reload --port 18080
```

**2. 建立知识库索引**

`.env` 里填写 `WP_SITE_URL`，然后在 `backend/` 下运行：

```bash
uv run python -m app.kb.sync
```

它会拉取全部已发布文章，建立正文和图片索引。发布或修改文章后重新运行一次即可。
想先跳过图片理解、只建文字索引，加 `--no-vision`。
详细原理与参数见 [docs/knowledge-base.md](docs/knowledge-base.md)。

**3. 前端**

```bash
cd frontend
pnpm install
pnpm dev
```

打开 http://127.0.0.1:5173 即可看到左下角助手。`/ai-api` 已由 Vite 代理到后端 `/api`。

**4. 构建给 WordPress 用**

```bash
cd frontend
pnpm build
```

产物输出到 `wordpress-plugin/wp-live2d-ai/assets/app/`（含模型与 Cubism Core）。
整个 `wp-live2d-ai` 文件夹丢进 `wp-content/plugins/` 启用即可。
服务器上要手动改哪些配置、怎么验证，见 [docs/deployment-walkthrough.md](docs/deployment-walkthrough.md)。

## 当前状态

- [x] Live2D 渲染、视线跟随、更换模型上拉框
- [x] 半透明悬浮控件（悬停显示）
- [x] 文本对话（SSE 流式）
- [x] 语音输入（浏览器 Web Speech API，单句识别后手动发送）
- [x] LangChain agent（`create_agent`）+ DeepSeek（`deepseek-flash`）流式对话
- [x] 工具调用链路（时间 / 站内检索 / 跳转）
- [x] 站内知识库（WordPress 同步 + SQLite FTS5 + jieba 中文分词）
- [x] 文章图片理解（`deepseek-flash` 生成描述/OCR，图片可被文字检索）
- [x] 引用来源序号 + 悬浮卡片（标题/摘要/图片，新标签页打开）
- [x] “带我去看看”跳转按钮（只允许本站 URL）
- [x] mock 模式（无 Key 也能离线联调）
- [x] WordPress 插件接入（设置页、资源注入、不改主题）
- [x] WordPress 登录态注入 + 短时 JWT 换取（前端登录门已生效）
- [ ] 后端 JWT 校验、每日额度与管理员不限量
- [ ] WordPress 后台配置模型、额度与 API Key（端口只读）
- [ ] 上传图片找相似图（CLIP / 向量检索）
- [ ] AI 语音播报（TTS）
- [ ] 语音打断（连续收音 + 停止播报 + 取消请求）
- [ ] 文章发布后自动增量同步
- [ ] 服务器部署与自动化同步

## 说明

- 前端与 AI 完全解耦：换模型、换主题样式不影响后端；换模型供应商只需改 `backend/`。
- 注册、登录和找回密码复用 WordPress 用户体系；助手只消费登录态，不接触密码。
- Cubism Core 的授权请遵循 Live2D 官方条款；模型版权请自行确认。
- 模型文件较大，默认不纳入 Git（见 `.gitignore`），部署时随构建产物一起同步。
