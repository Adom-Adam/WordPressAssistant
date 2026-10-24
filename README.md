# WordPress Live2D AI 小助手

给 WordPress（Argon 主题）博客加一个住在左下角的 Live2D AI 助手的完整工程：
**前端只负责展示与交互，AI 逻辑全部在后端**，通过一个轻量 WordPress 插件接入，不改主题、不动 WordPress 核心。

```
浏览器
  ├─ Live2D 形象 + 对话/语音/换模型 UI      ← frontend（Vite + TypeScript + PixiJS）
  └─ fetch /ai-api/chat （SSE 流式）
        └─ Python AI 服务                  ← backend（FastAPI + LangChain + DeepSeek）
              └─ 知识库 / 站内检索 / 跳转工具（第二阶段）
```

## 目录

| 路径 | 作用 |
| --- | --- |
| [frontend/](frontend/) | Live2D 形象、悬浮控件、对话面板、语音输入 |
| [backend/](backend/) | FastAPI + LangChain agent（DeepSeek），流式对话与工具调用；[学习导读](backend/README.md) |
| [wordpress-plugin/wp-live2d-ai/](wordpress-plugin/wp-live2d-ai/) | WordPress 插件：加载构建产物、注入配置、设置页 |
| [docs/architecture.md](docs/architecture.md) | 技术架构与后续知识库/工具设计 |
| [docs/tutorial.md](docs/tutorial.md) | 教学教程：全部命令、搭建顺序与踩坑记录 |
| [docs/deploy.md](docs/deploy.md) | 部署到阿里云服务器（宝塔 + Nginx + systemd） |

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

**1. 后端**

```bash
cd backend
cp .env.example .env          # 填入 DEEPSEEK_API_KEY；不填则自动 mock
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

**2. 前端**

```bash
cd frontend
pnpm install
pnpm dev
```

打开 http://127.0.0.1:5173 即可看到左下角助手。`/ai-api` 已由 Vite 代理到后端 `/api`。

**3. 构建给 WordPress 用**

```bash
cd frontend
pnpm build
```

产物输出到 `wordpress-plugin/wp-live2d-ai/assets/app/`（含模型与 Cubism Core）。
整个 `wp-live2d-ai` 文件夹丢进 `wp-content/plugins/` 启用即可，详见 [docs/deploy.md](docs/deploy.md)。

## 当前状态

- [x] Live2D 渲染、视线跟随、更换模型上拉框
- [x] 半透明悬浮控件（悬停显示）
- [x] 文本对话（SSE 流式）
- [x] 语音输入（浏览器 Web Speech API，单句识别后手动发送）
- [x] LangChain agent（`create_agent`）+ DeepSeek 流式对话
- [x] 工具调用链路（内置 `get_current_datetime` 示例工具）
- [x] mock 模式（无 Key 也能离线联调）
- [x] WordPress 插件接入（设置页、资源注入、不改主题）
- [ ] 站内知识库（文章同步 + 检索）
- [ ] 引用来源展示与跳转工具
- [ ] AI 语音播报（TTS）
- [ ] 语音打断（连续收音 + 停止播报 + 取消请求）
- [ ] 更多 agent tools / skills
- [ ] 服务器部署与自动化同步

## 说明

- 前端与 AI 完全解耦：换模型、换主题样式不影响后端；换模型供应商只需改 `backend/`。
- Cubism Core 的授权请遵循 Live2D 官方条款；模型版权请自行确认。
- 模型文件较大，默认不纳入 Git（见 `.gitignore`），部署时随构建产物一起同步。
