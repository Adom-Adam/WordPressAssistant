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

放好后 `pnpm dev` 或 `pnpm build` 会自动生成 `manifest.json`。前台不提供换模型按钮；
管理员在 WordPress 后台「设置 → Live2D 小助手 → Live2D 形象」里从清单中选择一个，所有访客都会看到它。
**不要**用 Cubism 2 的 `.model.json` 格式，本项目只带 Cubism 3/4 运行时。

### Spine 模型（可选，已接入）

Spine 4.2 模型放在：

```
frontend/public/spine/models/<模型文件夹>/
├── <骨架>.json          ← 必需，包含 skeleton / animations
├── <骨架>.atlas         ← 必需，和骨架同名
├── 贴图.png
└── model.json           ← 可选：显示名、默认动画、是否循环、偏移
```

`model.json` 可选内容：

```json
{
  "name": "月之拾忆少女",
  "animation": "主体动画",
  "loop": true,
  "scale": 1,
  "offsetX": 0,
  "offsetY": 0
}
```

`pnpm dev` / `pnpm build` 会自动生成 `spine/models/manifest.json`。管理员在
「设置 → Live2D 小助手 → 形象引擎」选择 Spine，再从「Spine 模型」里选具体模型；
Spine 加载失败时会自动回退到 Live2D，聊天功能不受影响。

现有 Spine 模型只有自带动画循环，没有 Live2D 那种口型、眨眼和表情参数，
所以目前是“会动的立绘”。构建时只复制 `.json` / `.atlas` / 图片，
`test.py`、`.bak`、`desktop.ini` 之类的杂项不会进入插件产物。
模型清单、动画情况和授权说明见
[frontend/public/spine/models/README.md](frontend/public/spine/models/README.md)。

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
cp -r personas.example data/personas   # 首次：复制人设格式示例，再替换成真实人设
uv sync
uv run uvicorn app.main:app --reload --port 18080
uv run pytest -q               # 认证、额度、防循环测试（不调用真实模型）
```

真实人设正文放在 `backend/data/personas/`，该目录已被 `.gitignore` 忽略，
不会上传到 GitHub。格式和版本切换见
[backend/personas.example/README.md](backend/personas.example/README.md)。

QQ 通道默认关闭。本地没有 NapCat 时，用 `backend/scripts/mock_onebot.py`
和 `backend/scripts/qq_simulate.py` 降级联调，步骤见
[backend/README.md](backend/README.md) 的“QQ 通道”一节。

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

产物输出到 `wordpress-plugin/wp-live2d-ai/assets/app/`（含 Live2D/Spine 模型与 Cubism Core）。
整个 `wp-live2d-ai` 文件夹丢进 `wp-content/plugins/` 启用即可。
服务器上要手动改哪些配置、怎么验证，见 [docs/deployment-walkthrough.md](docs/deployment-walkthrough.md)。

## 当前状态

- [x] Live2D 渲染、视线跟随、管理员后台选择模型
- [x] Spine 4.2 渲染与动画循环（可选引擎，失败自动回退 Live2D）
- [x] 形象大小三档预设 + 自定义像素（WordPress 后台可调）
- [x] 半透明悬浮控件（悬停显示）
- [x] 文本对话（SSE 流式）
- [x] 语音输入（浏览器 Web Speech API，单句识别后手动发送）
- [x] LangChain agent（`create_agent`）+ DeepSeek（`deepseek-flash`）流式对话
- [x] 人设文件版本化（正文放 `backend/data/personas/`，不进 Git）
- [x] 工具调用链路（时间 / 站内检索 / 跳转）
- [x] 站内知识库（WordPress 同步 + SQLite FTS5 + jieba 中文分词）
- [x] 文章图片理解（`deepseek-flash` 生成描述/OCR，图片可被文字检索）
- [x] 引用来源序号 + 悬浮卡片（标题/摘要/图片，新标签页打开）
- [x] “带我去看看”跳转按钮（只允许本站 URL）
- [x] mock 模式（无 Key 也能离线联调）
- [x] WordPress 插件接入（设置页、资源注入、不改主题）
- [x] WordPress 登录态注入 + 短时 JWT 换取（前端登录门已生效）
- [x] 后端 JWT 校验、每日额度、管理员不限量、失败退款
- [x] 全站每日总额度（只统计普通用户，管理员不计入）
- [x] 聊天窗口实时显示剩余额度（打开面板时查询 + 每次回答用响应头刷新）
- [x] agent 防失控（模型/工具调用上限 + 单轮超时）
- [x] WordPress 后台配置模型、单用户/全站额度、调用上限与超时（立即生效）
- [x] 后端端口、API Key、JWT 状态在设置页只读展示
- [x] 代码块按纯文本渲染（不引入 Markdown 解析器，避免 XSS）
- [x] QQ OneBot 薄适配器 + 本地 mock（实验性，默认关闭）
- [x] 服务器 NapCat 连通性验证（2026-10-30：实测群内 @ 可正常回复）
- [ ] QQ 通道生产接入（独立额度、内容审核、账号绑定，缺一不可）
- [ ] 上传图片找相似图（CLIP / 向量检索）
- [ ] AI 语音播报（TTS）
- [ ] 语音打断（连续收音 + 停止播报 + 取消请求）
- [ ] 文章发布后自动增量同步
- [x] 服务器部署（systemd + Nginx 同域反代）
- [ ] 自动化构建/同步流水线

## 后续待办（尚未排期）

### 知识库自动增量同步

现在文章发布后需要手动跑同步脚本，否则梅林检索不到新内容。目标是：

- WordPress 在文章发布、更新、删除时通知后端；
- 后端只重建受影响的那一篇，而不是整库重扫；
- 通知失败可重试，删除文章时同步删除索引；
- 后台能看到最近一次同步时间和失败原因。

### TTS 语音播报

- 网页端：把回复转成语音播放，可开关、可打断；
- 优先用云端 TTS（按字符计费），不在 1.6 GB 内存的服务器上跑本地语音模型；
- 如果接入 QQ，可复用同一套 TTS，把回复作为语音消息发送；
- 注意缓存常用短句，避免重复计费。

### 多平台接入（设想）

让同一个梅林出现在 QQ、Telegram 等平台，核心 agent、知识库和工具保持不变，
只增加平台适配层。需要先解决身份映射、独立额度、群聊礼仪和内容审核，
详见后续设计讨论。

### QQ 账号绑定与主动消息（设想）

- **账号绑定**：WordPress 用户 ID 是现成的唯一标识，但对外使用单独的随机“馆内编号”，
  避免暴露连续的用户 ID。已登录用户在博客里生成一次性绑定码，再在 QQ 私聊里发给梅林；
  未绑定的 QQ 不进入对话，只收到绑定提示。绑定后网页与 QQ 共用同一份关系状态和记忆，
  梅林才能认出“这是同一个人”。
- **主动消息**：技术上可以通过 OneBot 主动发送私聊/群消息，但必须先有绑定关系、
  明确的接收意愿、频率上限、静默时段和退订方式；群聊还需要群主同意。
  该能力风险较高，等绑定和记忆稳定后再单独设计，不随 QQ 通道一起上线。

## 说明

- 前端与 AI 完全解耦：换模型、换主题样式不影响后端；换模型供应商只需改 `backend/`。
- 注册、登录和找回密码复用 WordPress 用户体系；助手只消费登录态，不接触密码。
- Cubism Core 的授权请遵循 Live2D 官方条款；模型版权请自行确认。
- 模型文件较大，默认不纳入 Git（见 `.gitignore`），部署时随构建产物一起同步。
