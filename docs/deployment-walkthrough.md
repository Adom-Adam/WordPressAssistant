# 从零部署到服务器：前后端移植实操

这篇文档面向第一次把项目搬上服务器的人，按顺序覆盖：

- 服务器上要建哪些目录、用什么账号运行；
- 后端 `.env`、JWT 密钥、知识库怎么配；
- systemd 和 Nginx 怎么写；
- WordPress 插件怎么上传、`wp-config.php` 怎么改；
- 哪些设置必须**手动**在服务器上完成；
- 怎么验证、更新、回滚和排查。

> 适用版本：插件 `0.3.0` / 后端 `dd98f71` 之后。
>
> **当前边界（很重要）**：后端会校验 JWT，未登录/伪造/过期 token
> 一律 401；普通用户按北京时间每天限额，并受全站总额度约束；管理员不限量，
> 也不占全站额度。绕过前端直接 curl `/ai-api/chat` 同样会被拒绝。
> 部署时请确认 `.env` 里有 `L2D_AI_JWT_SECRET`，且和 `wp-config.php` 完全一致。
> 额度、模型名、调用上限和超时可以在 WordPress 后台修改，保存后立即生效。
>
> **部署记录（2026-10-26）**：生产服务器已更新到 `dd98f71`，安装 PyJWT 并重启
> `l2d-ai-backend`。已确认 `/api/health` 返回 `"auth_configured":true`，
> 匿名 `POST /ai-api/chat` 返回 `401`。额度、退款与调用上限均使用代码默认值
> （20 条/天、模型 6 次、工具 6 次、单轮 90 秒），无需额外写入 `.env`。

---

## 0. 部署后的结构

```text
公网 443
  └─ Nginx（宝塔站点）
       ├─ /                    → WordPress（PHP-FPM）
       │    ├─ wp-config.php   ← 放 JWT 共享密钥
       │    └─ wp-content/plugins/wp-live2d-ai/  ← 前端构建产物 + 插件
       │
       └─ /ai-api/             → 127.0.0.1:18080（FastAPI，systemd 托管）
            ├─ backend/.env    ← DeepSeek Key、JWT 密钥、额度配置
            └─ backend/data/   ← 知识库 + 每日额度 SQLite
```

需要先建立三个认识：

1. **前端不是独立服务**。它被构建成 `app.js` / `app.css`，由 WordPress 插件加载。
   服务器上不需要 Node，也不需要 5173 端口。
2. **后端不暴露公网端口**。它只监听 `127.0.0.1:18080`，公网只能通过 Nginx 的 `/ai-api/` 访问。
3. **密钥有两处**：DeepSeek API Key 只在后端 `.env`；JWT 共享密钥在后端 `.env` 和
   WordPress `wp-config.php` 各一份，两处必须完全一致。

---

## 1. 目录与端口规划

| 内容 | 推荐位置 | 属主 | 说明 |
| --- | --- | --- | --- |
| 仓库代码 | `/opt/l2d-ai/WordPressAssistant` | `l2dai:l2dai` | 不要放进网站根目录 |
| 后端配置 | `/opt/l2d-ai/WordPressAssistant/backend/.env` | `l2dai:l2dai`，权限 `640` | 含 API Key 和 JWT 密钥 |
| 知识库 + 额度库 | `/opt/l2d-ai/WordPressAssistant/backend/data/` | `l2dai:l2dai` | SQLite；知识库可重建，额度库删掉等于重置用量 |
| WordPress 插件 | `/www/wwwroot/<站点目录>/wp-content/plugins/wp-live2d-ai/` | `www:www` | 含构建产物和模型 |
| systemd 服务 | `/etc/systemd/system/l2d-ai-backend.service` | `root:root` | 开机自启、崩溃重启 |
| Nginx 配置 | 宝塔站点配置的 `server { }` | `root:root` | 用宝塔“配置文件”修改 |

**为什么后端不放 `/www/wwwroot`**：那里是 Nginx 的静态目录。`.env` 和 SQLite
一旦因为配置错误被当成静态文件，浏览器就能直接下载。`/opt` 不在任何站点的根目录下。

**端口**：

| 端口 | 用途 | 是否对公网开放 |
| --- | --- | --- |
| 22 | SSH | 是（建议限制来源 IP） |
| 80/443 | WordPress + `/ai-api` | 是 |
| 18080 | FastAPI | **否**，只监听 `127.0.0.1` |

阿里云安全组和宝塔防火墙都**不要**放行 18080。

### 1.1 `127.0.0.1` 是什么意思？uvicorn 不是可以直接开端口吗？

先分清两件事：**监听地址** 和 **端口**。

- **IP 地址**回答“哪台机器、哪块网卡”；
- **端口**回答“这台机器上的哪个程序”。

`uvicorn --host 127.0.0.1 --port 18080` 的意思是：
“我只在 **本机回环网卡** 上占用 18080 这个端口。”

服务器通常有几块“网卡”：

| 地址 | 含义 | 谁能访问 |
| --- | --- | --- |
| `127.0.0.1` | 回环地址（loopback），永远指向本机自己 | 只有服务器自己 |
| `0.0.0.0` | 所有网卡 | 本机 + 内网 + 公网（如果防火墙放行） |
| 服务器的内网/公网 IP | 某一块具体网卡 | 取决于网络和防火墙 |

所以：

```bash
# 在服务器上执行：能通
curl http://127.0.0.1:18080/api/health

# 从你自己的电脑执行：不通（连接被拒绝/超时）
curl http://<服务器公网IP>:18080/api/health
```

不是防火墙没开，而是 **uvicorn 根本没有在公网网卡上监听**。
即使把阿里云安全组和宝塔防火墙都放行，公网也连不上——因为那个“门”不存在。

uvicorn 确实可以直接监听公网：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 18080
```

但这通常是**不推荐**的，原因在下一节。这里没有 Docker，所以也不存在
“端口映射”这回事：`--port` 只是让进程监听端口；Docker 的 `-p 18080:18080`
才是 NAT 层的端口映射。

### 1.2 反向代理到底在做什么？

“反向代理”就是**服务器前面的前台**：

- 浏览器只认识 `https://你的域名`（80/443）；
- Nginx 在 443 上接待所有请求；
- 看到路径是 `/ai-api/...`，就转给本机的 `127.0.0.1:18080`；
- Python 处理完，把响应交回 Nginx；
- Nginx 再加密后发回浏览器。

```text
浏览器
  │  https://你的域名/ai-api/chat
  ▼
Nginx :443（公网）
  │  解密 TLS，匹配 location /ai-api/
  │  proxy_pass http://127.0.0.1:18080/api/chat
  ▼
uvicorn :18080（只监听回环）
  │  LangChain / DeepSeek / 知识库
  ▼
Nginx 把 SSE 流原样转回浏览器
```

它和“端口映射”不是一回事：

| | 端口映射（Docker/NAT） | 反向代理（Nginx） |
| --- | --- | --- |
| 工作层 | 传输层（TCP） | 应用层（HTTP） |
| 能否按路径分流 | 不能 | 能（`/ai-api/` → 18080） |
| TLS 证书 | 后端自己处理 | Nginx 统一处理 |
| 改路径 | 不能 | 能（`/ai-api/chat` → `/api/chat`） |
| 流式控制 | 不涉及 | 能关缓冲、调超时 |
| 日志/限流/WAF | 弱 | 强 |

**为什么这里要用反向代理：**

1. **同源**：浏览器只看到 `https://你的域名`，`/ai-api` 和 WordPress 同源，
   不会有 CORS，也不会出现“HTTPS 页面请求 HTTP 接口”的混合内容拦截。
2. **TLS 只在一处**：证书配在 Nginx；后端只说普通 HTTP，且只在回环上。
3. **SSE 可控**：聊天是流式响应，Nginx 必须关掉缓冲，否则会“等全部生成完才显示”。
4. **隐藏后端**：公网只开 80/443，18080 不暴露，攻击面更小。
5. **路径改写**：前端统一写 `/ai-api/chat`，后端仍然是 `/api/chat`。
6. **运维能力**：访问日志、限流、WAF、压缩、超时都在 Nginx 做。

一句话：**uvicorn 负责“算”，Nginx 负责“接待、加密、分流、限流”。**

---

## 2. 开始前准备

| 需要的东西 | 从哪里拿 | 注意 |
| --- | --- | --- |
| 服务器 root 或宝塔终端 | 宝塔面板 | 创建 systemd、改 Nginx 需要 root |
| SSH 密钥登录 | 本地 `~/.ssh/` | 不要打印私钥，不要把密码贴进聊天 |
| 域名与 HTTPS 证书 | 宝塔站点配置 | `/ai-api/` 与 WordPress 同域 |
| 站点根目录 | 宝塔 → 网站 → 根目录 | 下文用 `<站点目录>` 代替 |
| DeepSeek API Key | platform.deepseek.com | 只写服务器 `.env` |
| JWT 共享密钥 | `openssl rand -hex 32` | 后端 `.env` 与 `wp-config.php` 必须一致 |
| WordPress 管理员账号 | 后台 | 用于启用插件、修改设置 |

建议先确认服务器状态：

```bash
free -h          # 内存；本项目 1.5G 左右可以跑
df -h /          # 磁盘；知识库和 .venv 会占一些空间
timedatectl      # 建议 Asia/Shanghai；额度日界以后按北京时间算
python3 --version  # 不满足 3.11 也没关系，uv 会自己下载
```

> 不要为了跑后端去升级或替换系统自带的 Python。宝塔面板本身依赖它，
> `uv` 会在自己的目录里安装独立版本，互不影响。

---

## 3. 创建运行用户并安装 uv（root）

后端不建议用 root 跑，也不建议复用 `www`（那是 Web 服务账户）。
单独建一个不能登录 shell 的系统用户：

```bash
id l2dai >/dev/null 2>&1 || \
  useradd --system --create-home --home-dir /opt/l2d-ai --shell /usr/sbin/nologin l2dai

mkdir -p /opt/l2d-ai/.uv-cache
chown -R l2dai:l2dai /opt/l2d-ai
chmod 750 /opt/l2d-ai
```

安装 `uv` 到系统路径（这样 systemd 和普通用户都能找到）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
uv --version
```

安装基础工具（如果还没有）：

```bash
apt-get update
apt-get install -y git curl ca-certificates openssl
```

---

## 4. 拉代码并安装 Python 依赖

仓库是公开的，服务器上直接克隆：

```bash
git clone --depth 1 https://github.com/Adom-Adam/WordPressAssistant.git /opt/l2d-ai/WordPressAssistant
chown -R l2dai:l2dai /opt/l2d-ai/WordPressAssistant
```

> **路径必须前后一致。** 本文按实际部署目录 `/opt/l2d-ai/WordPressAssistant` 编写。
> 如果你换了目录，后面 systemd 的 `WorkingDirectory` 和 `ExecStart` 也要同步改；
> 这两处写错，服务会以 `203/EXEC` 或 `200/CHDIR` 启动失败。
>
> 如果目录已经存在（例如之前已经克隆过），不要再执行 `git clone`，用
> `git -C /opt/l2d-ai/WordPressAssistant pull` 更新即可。

安装依赖：

```bash
cd /opt/l2d-ai/WordPressAssistant/backend
runuser -u l2dai -- env UV_CACHE_DIR=/opt/l2d-ai/.uv-cache /usr/local/bin/uv sync --no-dev
```

- `uv sync` 会按 `pyproject.toml` / `uv.lock` 建 `.venv`，并自动下载合适的 Python（3.11+）；
- `--no-dev` 不安装 pytest 等开发依赖；
- `pyproject.toml` 里配置了清华 PyPI 镜像，国内下载更快；
- 用 `runuser -u l2dai` 是为了让 `.venv` 和缓存都归运行用户所有。
  有 sudo 时用 `sudo -u l2dai ...` 也可以。

建数据目录：

```bash
install -d -o l2dai -g l2dai /opt/l2d-ai/WordPressAssistant/backend/data
```

> **不要上传本地的 `.venv`、`node_modules`、`data/`**。`.venv` 里是 Windows 的二进制，
> 到 Linux 不能直接用；知识库建议在服务器上重新同步。

---

## 5. 写后端 `.env`

```bash
cd /opt/l2d-ai/WordPressAssistant/backend
install -o l2dai -g l2dai -m 640 .env.example .env
nano .env          # 没有 nano 就用 vi，或在宝塔文件管理里编辑
```

至少要改这些：

| 键 | 示例 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | `sk-...` | 只写服务器；不填会进入 mock 模式 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 初始默认值；后台“DeepSeek 模型名”可覆盖 |
| `WP_SITE_URL` | `https://你的域名` | 知识库同步要抓的站点 |
| `L2D_AI_JWT_SECRET` | 64 位十六进制 | 必须和 `wp-config.php` 完全一致 |
| `L2D_AI_JWT_ISSUER` | `l2d-ai-wordpress` | 与插件常量一致 |
| `L2D_AI_JWT_AUDIENCE` | `l2d-ai-backend` | 与插件常量一致 |
| `L2D_AI_JWT_TTL_SECONDS` | `900` | 与插件签发有效期一致 |
| `L2D_AI_JWT_LEEWAY_SECONDS` | `30` | 允许的服务器时钟偏差 |
| `DAILY_QUOTA` | `20` | 普通用户每天条数；管理员不限量；后台可覆盖 |
| `GLOBAL_DAILY_QUOTA` | `0` | 全站普通用户每天合计条数；0 = 不限；后台可覆盖 |
| `QUOTA_DB_PATH` | `data/quota.sqlite3` | 额度计数库；可随时删除重置 |
| `QUOTA_TIMEZONE` | `Asia/Shanghai` | 按哪个时区判断“新的一天” |
| `RUNTIME_SETTINGS_PATH` | `data/runtime_settings.json` | 后台保存的运营参数；不存在时用 `.env` 默认值 |
| `AGENT_MAX_MODEL_CALLS` | `6` | 单轮最多模型调用次数（防循环）；后台可覆盖 |
| `AGENT_MAX_TOOL_CALLS` | `6` | 单轮最多工具调用次数；后台可覆盖 |
| `AGENT_MAX_SECONDS` | `90` | 单轮最长秒数；后台可覆盖 |
| `MOCK_MODE` | `false` | 生产环境应为 false |

`CORS_ORIGINS` 保持默认即可：生产环境走同域 `/ai-api/`，不会触发跨域。
只有后端独立域名时才需要加博客域名。

> 额度、模型名、调用上限和超时的**初始值**来自 `.env`；一旦在 WordPress 后台保存过，
> 就以 `backend/data/runtime_settings.json` 为准，改 `.env` 不会覆盖它。
> 想彻底回到 `.env` 默认值，停止后端后删除这个 JSON 再启动（不要只改 `.env` 然后疑惑为什么没生效）。

> `.env` 权限必须是 `640` 或 `600`。后端进程按**当前工作目录**找 `.env`，
> 所以 systemd 的 `WorkingDirectory` 必须是 `backend/`，否则会出现“明明配了 Key 还是 mock”。

---

## 6. 对齐 JWT 共享密钥（手动）

生成一次（在服务器终端执行）：

```bash
openssl rand -hex 32
```

把结果分别写进两个地方：

1. **后端** `/opt/l2d-ai/WordPressAssistant/backend/.env`
   `L2D_AI_JWT_SECRET=<生成的值>`
2. **WordPress** `/www/wwwroot/<站点目录>/wp-config.php`
   在 `/* That's all, stop editing! Happy publishing. */` 之前加：

```php
define( 'L2D_AI_JWT_SECRET', '生成的值' );
```

注意事项：

- 两处**必须逐字符相同**，包括大小写；长度至少 32 字符；
- `wp-config.php` 属主是 `www:www`、权限 `640`，普通 SSH 用户改不了，用宝塔文件管理或宝塔终端；
- **不要**把密钥贴进聊天、提交到 Git 或写进插件设置页；
- 修改密钥会让已签发但未过期的 token 全部失效（最多影响 15 分钟）；
- 检查长度而不泄露内容：

```bash
grep '^L2D_AI_JWT_SECRET=' .env | awk -F= '{print "length:", length($2)}'
```

> 后端现在会读取并校验这个密钥。两处不一致时，所有聊天请求都会返回 401；
> 后端完全没配时返回 503。健康检查里的 `auth_configured` 可以快速判断是否配好。

---

## 7. 建立知识库索引

先只建文字索引，确认网络和文章抓取正常：

```bash
cd /opt/l2d-ai/WordPressAssistant/backend
runuser -u l2dai -- .venv/bin/python -m app.kb.sync --no-vision
```

确认没问题后，再跑包含图片理解的完整同步（会调用视觉模型，较慢、会产生 token 费用）：

```bash
runuser -u l2dai -- .venv/bin/python -m app.kb.sync
```

- 文章多时建议在 `tmux` / `screen` 里跑，避免 SSH 断开导致中断；
- 同步完成后 `data/kb.sqlite3` 应存在；
- 发布或修改文章后重新跑一次；
- 如果服务器抓不到自己的站点，先用 `curl -I https://你的域名/` 检查 DNS、证书和防火墙。

---

## 8. 用 systemd 托管后端

### 8.1 面向初学者：systemd 是什么？

你可以把 systemd 理解成服务器上的**服务管家**：

- 服务器开机时，它负责把该启动的服务拉起来；
- 进程崩溃时，它按规则重启；
- 它记录服务的标准输出和错误输出（日志）；
- 它统一管理“谁在跑、跑在哪个用户下、工作目录在哪”。

没有 systemd 时，常见做法是：

```bash
nohup uv run uvicorn app.main:app --port 18080 &
```

问题很明显：SSH 断开可能被杀、服务器重启不会自动起来、崩溃了没人管、
日志散落在某个 `nohup.out` 里、时间久了忘记自己开过几个进程。

systemd 用一份 **unit 文件**（就是一份“岗位说明书”）描述怎么运行服务。
文件放在 `/etc/systemd/system/`，名字通常叫 `<服务名>.service`。

unit 文件分三段：

| 段 | 作用 | 常见字段 |
| --- | --- | --- |
| `[Unit]` | 元信息、启动顺序 | `Description`、`After=network.target` |
| `[Service]` | 怎么运行 | `User`、`WorkingDirectory`、`ExecStart`、`Restart` |
| `[Install]` | 开机自启挂到哪个目标 | `WantedBy=multi-user.target` |

几个容易混淆的命令：

| 命令 | 含义 |
| --- | --- |
| `systemctl start l2d-ai-backend` | **现在**启动一次 |
| `systemctl enable l2d-ai-backend` | 设置**开机自启**（不会立刻启动） |
| `systemctl enable --now ...` | 两者一起做 |
| `systemctl restart ...` | 重启（改了 `.env` 或代码后） |
| `systemctl status ...` | 看状态、最近几行日志、退出码 |
| `systemctl disable ...` | 取消开机自启（不会立刻停止） |
| `systemctl daemon-reload` | **改完 unit 文件后必须执行**，让 systemd 重新读配置 |

日志用 `journalctl`：

```bash
journalctl -u l2d-ai-backend -f          # 实时跟踪
journalctl -u l2d-ai-backend -n 100      # 最近 100 行
journalctl -u l2d-ai-backend --since "10 min ago"
```

常见启动失败退出码：

| 退出码 | 含义 | 先检查 |
| --- | --- | --- |
| `203/EXEC` | 可执行文件不存在或没有执行权限 | `ExecStart` 路径、`.venv` 是否建好 |
| `200/CHDIR` | 工作目录不存在 | `WorkingDirectory` 路径 |
| `217/USER` | 运行用户不存在 | `User=l2dai` 是否创建 |
| `1` / `3` | Python 自己报错 | `journalctl` 里的 traceback、`.env` |

> **unit 文件的属主必须是 `root:root`、权限 `644`。**
> 如果它归 `www` 所有，WordPress 一旦被攻破，攻击者就能改这个文件，
> 让服务以任意用户运行。这是一个很容易被忽略的提权入口。

### 8.2 写 unit 文件

创建 `/etc/systemd/system/l2d-ai-backend.service`：

```ini
[Unit]
Description=Live2D AI Assistant Backend
After=network.target

[Service]
Type=simple
User=l2dai
Group=l2dai
UMask=0027
WorkingDirectory=/opt/l2d-ai/WordPressAssistant/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/l2d-ai/WordPressAssistant/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18080 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

保存后先修正权限（很重要）：

```bash
chown root:root /etc/systemd/system/l2d-ai-backend.service
chmod 644 /etc/systemd/system/l2d-ai-backend.service
```

每一行的理由：

| 配置 | 作用 |
| --- | --- |
| `User=l2dai` | 不用 root 运行，缩小被攻破后的影响 |
| `WorkingDirectory=.../backend` | 让程序找到 `.env` 和 `data/` |
| `--host 127.0.0.1` | 只允许本机访问，公网打不到 18080 |
| `--proxy-headers` | 正确识别 Nginx 传来的真实访客 IP |
| `Restart=always` | 崩溃或重启后自动拉起 |
| `NoNewPrivileges` / `PrivateTmp` | 基础加固，阻止提权和共享临时目录 |

### 8.3 启用与查看

```bash
systemctl daemon-reload
systemctl enable --now l2d-ai-backend
systemctl status l2d-ai-backend --no-pager
curl http://127.0.0.1:18080/api/health
```

`daemon-reload` 不能省：systemd 会缓存 unit 文件，改完不重新加载，
它仍然按旧配置启动。看到 `Active: inactive (dead)` 或 `failed` 时，
先 `systemctl status` 看退出码，再 `journalctl -u ...` 看 Python 的报错。

健康检查应返回：

```json
{"ok":true,"mock":false,"model":"deepseek-flash","auth_configured":true}
```

`mock:true` 说明没有读到 API Key，重点检查 `.env` 路径和权限。

看日志：

```bash
journalctl -u l2d-ai-backend -f
```

> 宝塔的“Python 项目管理器”也能托管，但 systemd 更透明、配置可版本化。
> 二选一即可，不要同时启动两个进程抢 18080。

---

## 9. Nginx 反向代理（手动）

宝塔 → 网站 → 你的站点 → 设置 → 配置文件，在 `server { }` 内、PHP 的
`include enable-php-*.conf;` 之前加入：

```nginx
location /ai-api/ {
    proxy_pass http://127.0.0.1:18080/api/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";

    # 流式输出（SSE）必须关闭缓冲，否则会等整段生成完才显示
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

关键点：

- `proxy_pass` 末尾的 `/` 不能少：`/ai-api/chat` → `/api/chat`；
- `proxy_buffering off` 是“逐字回复”的关键；
- 用宝塔的配置文件编辑器改，面板保存时会整体重写，直接改临时文件会被覆盖；
- 保存后宝塔会重载 Nginx；命令行检查用 `nginx -t`。

### 9.1 缓存与 CDN 排除

即使后端给 token 响应加了 `Cache-Control: no-store`，页面缓存或 CDN 仍可能缓存。
需要排除：

- `/wp-json/l2d-ai/*`（换 token）
- `/ai-api/*`（聊天流）
- 已登录用户的页面（WordPress 缓存插件通常按 Cookie 自动排除）

如果用了 CDN/WAF，确认它不会缓冲 SSE、不会拦截 `X-WP-Nonce` 请求头、
不会把长时间连接当成攻击。可以先临时绕过 CDN 测试，确认问题来源。

### 9.2 在宝塔里怎么操作？需要为后端新建站点吗？

**不需要新建站点。**

“新建站点”在宝塔里意味着新建一个域名（vhost）和网站根目录。后端没有独立域名，
也没有需要 Nginx 直接提供的静态文件；它只是现有 WordPress 站点下的一个路径
`/ai-api/`。给后端单独建站点反而会带来：

- 同域名冲突；
- 子域名要额外做 DNS 解析和证书；
- 前端 `apiBase` 得改成绝对地址，变成跨域，还要配 CORS；
- 多一个 vhost 要维护。

正确做法：**在现有 WordPress 站点的 `server { }` 里加一个 `location`。**

#### 方式 A：手动改配置文件（推荐，最可控）

1. 宝塔 → 网站 → 找到 WordPress 站点 → 设置 → 配置文件；
2. 在 `server { }` 内、PHP 的 `include enable-php-*.conf;` 之前，粘贴第 9 节的
   `location /ai-api/ { ... }`；
3. 保存。宝塔会检查语法并重载 Nginx；报错会弹出来；
4. 验证：

```bash
curl -s https://你的域名/ai-api/health
```

#### 方式 B：用宝塔的“反向代理”界面

不同宝塔版本字段名略有差异，核心步骤是：

1. 网站 → 站点 → 设置 → 反向代理 → 添加反向代理；
2. 代理名称：`l2d-ai-api`；
3. 目标 URL：`http://127.0.0.1:18080`；
4. 发送域名：`$host`；
5. 代理目录：填 `/ai-api/`（有的版本叫“代理目录/路径”）；
6. **缓存：关闭**；
7. 保存后打开“配置文件”，确认最终生成的内容里有：

```nginx
proxy_pass http://127.0.0.1:18080/api/;   # 末尾必须有 /
proxy_buffering off;                      # 没有就手动补
proxy_read_timeout 300s;
```

> 如果面板只允许代理 `/`，**不要**在主站点上使用它——那会把整个 WordPress
> 也转发给 Python 服务。这种情况回到方式 A。

#### 验证返回码的含义

| `/ai-api/health` 返回 | 含义 |
| --- | --- |
| `200` + JSON | 反代和后端都正常 |
| `404` | Nginx 没有匹配到 `location`（配置没保存/放错 server 块） |
| `502` | Nginx 找不到后端（服务没启动、端口不对） |
| `504` | 后端超时（SSE 超时设得太短或后端卡住） |

#### 什么时候才需要新建站点？

只有当你决定让后端使用**独立子域名**（例如 `ai.example.com`）时才需要。
那时要额外做 DNS、证书、修改插件 `apiBase` 为绝对地址，并在后端配置 CORS。
除非有特别理由，**同域 `/ai-api/` 更简单，也更容易保证登录 Cookie 和 nonce 正常**。

---

## 10. 部署 WordPress 插件（手动）

### 10.1 本地构建

在本地仓库根目录：

```bash
cd frontend
pnpm install
pnpm build
```

产物会写进：

```text
wordpress-plugin/wp-live2d-ai/assets/app/
├─ app.js
├─ app.css
└─ live2d/            ← Cubism Core 和模型
```

模型必须先放进 `frontend/public/live2d/models/` 再构建，否则服务器上不会有模型。
`assets/app/` 不纳入 Git，所以**必须在本地构建后上传**，不能只靠服务器 `git pull`。

### 10.2 上传

把整个 `wp-live2d-ai` 文件夹上传到：

```text
/www/wwwroot/<站点目录>/wp-content/plugins/wp-live2d-ai/
```

推荐用宝塔文件管理上传 zip 后解压（普通 SSH 用户通常没有插件目录的写权限）。
上传后修正属主：

```bash
chown -R www:www /www/wwwroot/<站点目录>/wp-content/plugins/wp-live2d-ai
```

注意 `-R` 不能省：`assets/` 子目录也要一起改。如果外层是 `www:www`、里面
`assets/` 却是 `root:root`，WordPress 能读、但升级或在线编辑插件时会因为
没有写权限而失败。

**不要**上传以下内容：`node_modules/`、`.git/`、本地 `.env`。

### 10.3 启用与设置

1. WordPress 后台 → 插件 → 启用「Live2D AI 小助手」；
2. 设置 → Live2D 小助手；
3. AI 服务地址保持 `/ai-api`；
4. 确认「登录凭证」区域显示 JWT 密钥**已配置**。

插件版本号 `0.2.0` 会作为静态资源查询参数，升级后浏览器不会继续用旧缓存。
以后改了前端但没升版本号，可能出现“代码更新了页面没变”，把 `VERSION` 加一即可。

### 10.4 `wp-config.php` 加密钥

按第 6 节添加 `L2D_AI_JWT_SECRET`。保存后不需要重启 PHP；
如果是 OPcache 且配置了 `validate_timestamps=0`，才需要重载 PHP。

### 10.5 REST 接口检查

WordPress 的 token 接口依赖 REST API 可用。先试“漂亮”地址：

```bash
curl -i https://你的域名/wp-json/
```

如果返回 404，再试“参数”形式：

```bash
curl -i "https://你的域名/?rest_route=/"
curl -i "https://你的域名/index.php/wp-json/"
```

三种入口只要有一个返回 JSON，REST 就是正常的。**`/wp-json/` 404 本身不代表 REST 坏了**，
它通常说明固定链接用的是“朴素”模式，而 `rest_url()` 会自动选择可用的形式，
插件不需要你手动改。

如果三种都 404：

- 后台 → 设置 → 固定链接，改成“文章名”后保存（需要 Nginx 伪静态支持）；
- 检查安全插件/WAF 是否禁用了 REST API；
- 检查是否把 `/wp-json/` 误配置成了静态目录或重定向。

---

## 11. 验证清单

按顺序做，前一步不过就不要往下走。

### 11.1 后端本机

```bash
curl -s http://127.0.0.1:18080/api/health
systemctl is-active l2d-ai-backend
ss -lntp | grep 18080        # 应只看到 127.0.0.1:18080
```

### 11.2 经过 Nginx

```bash
curl -s https://你的域名/ai-api/health
```

返回与 11.1 相同。若 502，看 Nginx 错误日志和 `systemctl status`。

### 11.3 token 接口

未登录时应被拒绝：

```bash
# 固定链接是“文章名”时
curl -i -X POST https://你的域名/wp-json/l2d-ai/v1/token

# 固定链接是“朴素”时（上一条 404 就用这条）
curl -i -X POST "https://你的域名/?rest_route=/l2d-ai/v1/token"
```

期望 `401` 和 JSON 错误（`l2d_ai_not_logged_in`）。
如果返回 200，说明请求意外带上了登录 Cookie，或缓存/代理行为异常。

### 11.4 浏览器

1. 未登录打开博客，点聊天按钮 → 应看到“登录后就能聊天啦”和登录/注册按钮；
2. 登录后刷新 → 标题下出现昵称，管理员带“管理员”标记；
3. 打开开发者工具 → Network：
   - `POST /wp-json/l2d-ai/v1/token` 返回 200，响应里有 `token`、`expires_at`；
   - `POST /ai-api/chat` 的请求头带 `Authorization: Bearer ...`；
   - 响应类型是 `text/event-stream`，文字逐段出现；
4. 退出登录 → 登录门重新出现。

### 11.5 未登录请求必须被拒绝

```bash
curl -i -X POST https://你的域名/ai-api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

期望 `401`。如果仍然返回 `200 text/event-stream`，说明后端还是旧版本、服务没重启，
或者 `.env` 里的密钥没被读到。

### 11.6 额度的实际行为

- 普通用户：每天“普通用户每日额度”条，按 `QUOTA_TIMEZONE` 的零点重置；
- 普通用户还受“全站每日总额度”限制（0 = 不限），两者在同一个事务里占位；
- 管理员：token 里 `admin: true`，不限量，也不占全站额度；
- 一次聊天算 1 条，内部调用多少次工具都不会重复扣；
- 如果模型在输出第一个字之前失败/超时/被取消，个人和全站额度都会自动退回；
- 个人额度用完返回 `429`，提示“今天的聊天额度已经用完啦”；
  全站额度用完同样返回 `429`，但提示“今天全站的聊天额度已经用完了”。
- 打开聊天面板时 `GET /ai-api/quota` 会显示剩余次数；每次回答后前端再用响应头刷新。

想快速验证 429：在 WordPress 后台把“普通用户每日额度”临时改成 `1`，保存后立即生效，
用普通账号连发两条。第二条应返回 429，聊天窗口显示额度用完的提示。
验证完记得改回来。

> 额度计数库是 `backend/data/quota.sqlite3`。删掉它等于把所有人的当天用量清零；
> 它不参与 Git，也不需要和知识库一起重建。

### 11.7 WordPress 后台设置页

插件 `0.3.0` 起，后台“设置 → Live2D 小助手”分成三块：

1. **显示设置**（保存到 WordPress options）
   - AI 服务地址；
   - 形象大小：小 / 中 / 大 / 自定义；
   - 自定义宽高（160–420 px、260–720 px）。
   这一类只影响浏览器，保存后访客刷新页面即可看到。

2. **聊天额度与运行参数**（保存到后端 `runtime_settings.json`）
   - 普通用户每日额度；
   - 全站每日总额度（0 = 不限，只统计普通用户）；
   - 单轮模型调用 / 工具调用上限；
   - 单次回答超时；
   - DeepSeek 模型名。
   保存时 WordPress 服务器会现场签一个 **2 分钟有效的管理员 JWT**，
   再 `PUT /ai-api/admin/settings`；浏览器不会接触这个 token，也不会接触 JWT 密钥。
   后端校验并落盘后立即生效，不需要重启。

3. **只读运行信息**
   - 后端监听端口（由 systemd 决定）、DeepSeek Key 是否配置、mock 模式、
     后端 JWT 密钥是否配置、额度重置时区。
   - 端口和 Key 故意不提供在线修改：改它们需要动 systemd/Nginx 或 `.env`，
     “看起来能改、其实没生效”比只读更危险。

如果“读取后端设置失败”，依次检查：

- “AI 服务地址”是否可以访问（`curl http://127.0.0.1:18080/api/health`）；
- Python 服务是否在运行；
- `wp-config.php` 和后端 `.env` 的 `L2D_AI_JWT_SECRET` 是否完全一致；
- 后端日志里是否有 `401`（密钥不一致）或 `403`（token 里不是管理员）。

---

## 12. 更新、回滚与备份

### 12.1 更新后端

```bash
cd /opt/l2d-ai/WordPressAssistant
git pull
chown -R l2dai:l2dai backend
cd backend
runuser -u l2dai -- env UV_CACHE_DIR=/opt/l2d-ai/.uv-cache /usr/local/bin/uv sync --no-dev
systemctl restart l2d-ai-backend
journalctl -u l2d-ai-backend -n 50 --no-pager
```

### 12.2 更新插件

本地重新 `pnpm build`，重新上传整个文件夹，覆盖后：

```bash
chown -R www:www /www/wwwroot/<站点目录>/wp-content/plugins/wp-live2d-ai
```

### 12.3 备份

需要备份的只有“无法从 Git 重建”的东西：

```bash
tar czf /root/l2d-ai-backup-$(date +%F).tgz \
  /opt/l2d-ai/WordPressAssistant/backend/.env \
  /opt/l2d-ai/WordPressAssistant/backend/data
```

WordPress 侧按宝塔的数据库 + 站点备份策略走。
`backend/data/` 里现在有知识库、额度库，以及后台保存的 `runtime_settings.json`，升级前一起备份。

### 12.4 轮换密钥

1. 生成新值；
2. 同时更新后端 `.env` 和 `wp-config.php`；
3. `systemctl restart l2d-ai-backend`；
4. 旧 token 立即失效，用户刷新页面即可换新；
5. 如果只改了其中一处，所有聊天请求都会返回 401。

---

## 13. 手动设置总表

下面这些**必须在服务器上手动完成**，代码和构建不会替你做：

| # | 设置 | 位置 | 说明 |
| --- | --- | --- | --- |
| 1 | 运行用户 | `l2dai` | 不用 root/www 跑后端 |
| 2 | 目录与属主 | `/opt/l2d-ai` | 后端不能放在网站根目录 |
| 3 | `uv` 安装 | `/usr/local/bin/uv` | systemd/普通用户都能找到 |
| 4 | 后端 `.env` | `backend/.env`，`640` | DeepSeek Key、JWT 密钥、额度默认值、站点地址 |
| 5 | JWT 密钥 | `.env` + `wp-config.php` | 两处必须一致 |
| 6 | 知识库同步 | `backend/data/kb.sqlite3` | 发布文章后要重跑 |
| 7 | systemd 服务 | `/etc/systemd/system/l2d-ai-backend.service` | 开机自启、崩溃重启 |
| 8 | Nginx 反代 | 宝塔站点配置 | `/ai-api/` → 18080，关缓冲 |
| 9 | 防火墙/安全组 | 阿里云 + 宝塔 | **不要**放行 18080 |
| 10 | 插件上传 | `wp-content/plugins/wp-live2d-ai/` | 含 `assets/app/` 构建产物 |
| 11 | 插件属主 | `www:www` | 否则 WordPress 无法更新/读取 |
| 12 | 插件激活 | WordPress 后台 | 启用并检查设置页 |
| 13 | 服务地址 | 插件设置 `/ai-api` | 同域反代，无需 CORS |
| 14 | REST 可用 | 固定链接/安全插件 | token 接口依赖 `/wp-json/` |
| 15 | 缓存排除 | 缓存插件/CDN | `/wp-json/l2d-ai/*`、`/ai-api/*` |
| 16 | 时区 | `timedatectl` | 建议 `Asia/Shanghai` |
| 17 | 备份 | 宝塔 + `/opt/l2d-ai` | 升级前先备份 `.env` 和 `data/` |
| 18 | 后台运营参数 | 设置 → Live2D 小助手 | 首次保存后自动生成 `data/runtime_settings.json`，不要手改 |

---

## 14. 常见问题

| 现象 | 可能原因 | 排查 |
| --- | --- | --- |
| 设置页提示密钥未配置 | `wp-config.php` 没有 `define` 或拼写错误 | 确认常量名是 `L2D_AI_JWT_SECRET`，位置在 stop editing 之前 |
| token 接口 500 | 插件读不到密钥，或密钥短于 32 字符 | 看 WordPress 调试日志；设置页会提示 |
| token 接口 403 | nonce 过期、页面被缓存成访客版、WAF 拦截 | 刷新页面；排除缓存；检查 `X-WP-Nonce` |
| token 接口 404 | 插件未启用、REST 被禁用、固定链接形式 | 试 `?rest_route=/l2d-ai/v1/token`；确认插件已启用 |
| `/wp-json/` 404 但 `?rest_route=/` 200 | 固定链接是“朴素”模式 | 功能正常；想要漂亮地址可改固定链接并配伪静态 |
| 服务 `203/EXEC` | `ExecStart` 路径不对、`.venv` 不存在 | 对照实际目录，执行 `daemon-reload` |
| 服务 `200/CHDIR` | `WorkingDirectory` 路径不对 | 改成实际的 `backend/` 目录 |
| 插件能读不能更新 | 插件目录/`assets` 属主是 root | `chown -R www:www` 插件目录 |
| 登录后仍是登录门 | 页面缓存把访客 HTML 发给了登录用户 | 缓存插件按登录 Cookie 排除 |
| 登录后聊天 401 | 两处 JWT 密钥不一致、token 过期、服务器时间偏差 | 对比密钥长度/内容；`timedatectl` 校时；刷新页面换 token |
| 聊天 503 | 后端 `.env` 没有 `L2D_AI_JWT_SECRET` 或短于 32 字符 | 补齐后重启；看 `/api/health` 的 `auth_configured` |
| 聊天 429（个人） | 当天个人额度用完 | 正常行为；等零点重置，或后台调高“普通用户每日额度” |
| 聊天 429（全站） | 当天全站总额度用完 | 正常行为；等零点重置，或后台调高/清零“全站每日总额度” |
| 设置页“读取后端设置失败” | 服务地址错、后端没开、JWT 密钥不一致 | 依次检查地址、`systemctl status`、两处密钥；点“重新读取” |
| 后台保存后额度没变 | 改的是 `.env`，但运行时 JSON 优先 | 在后台改，或停服删除 `data/runtime_settings.json` 后重启 |
| 回复“查资料次数有点多” | agent 触发了防循环上限 | 正常保护；确认不是模型反复检索，必要时在后台小幅调高上限 |
| 回复一次性出现 | Nginx/CDN 缓冲 | `proxy_buffering off`，CDN 关缓冲 |
| `mock:true` | 后端没读到 `.env` | 检查 `WorkingDirectory`、文件权限、Key |
| 502 Bad Gateway | 后端没启动或端口不对 | `systemctl status`、`ss -lntp`、日志 |
| 插件按钮出现但没形象 | 模型没构建进去或路径 404 | 检查 `assets/app/live2d/models/manifest.json` |
| 控制台 Cubism Core 未定义 | Core 脚本没上传/被缓存 | 检查 `live2d/core/live2dcubismcore.min.js` |
| 语音按钮没反应 | 浏览器不支持或非 HTTPS | 用 Chrome/Edge，确认 HTTPS |
| `permission denied` 改 `.env` | 文件属主是 `l2dai` | 用 root/宝塔终端，或先 `chown` |
| 18080 被占用 | 重复启动（systemd + 手动/宝塔） | `ss -lntp \| grep 18080`，只保留一个 |
| 磁盘满 | 日志/缓存/镜像 | `du -sh /opt/l2d-ai/*`，清理旧日志 |
| `uv sync` 很慢或失败 | 网络/PyPI 镜像 | 确认能访问清华镜像；重试 |

---

## 15. 安全清单

- [ ] 后端以 `l2dai` 运行，不使用 root；
- [ ] 18080 只监听 `127.0.0.1`，安全组未放行；
- [ ] `backend/.env` 权限 `640`，属主 `l2dai`；
- [ ] `wp-config.php` 权限 `640`，属主 `www:www`；
- [ ] DeepSeek Key 和 JWT 密钥没有出现在 Git、聊天记录、日志中；
- [ ] systemd unit 文件属主 `root:root`、权限 `644`（`www` 不能改自己的启动命令）；
- [ ] 插件目录属主 `www:www`，包括 `assets/` 子目录，没有多余的可写权限；
- [ ] token 接口能被未登录请求正确拒绝（401）；
- [ ] 缓存/CDN 排除了 `/wp-json/l2d-ai/*` 和 `/ai-api/*`；
- [ ] 已配置备份，知道怎么恢复 `.env` 和 `data/`（知识库 + 额度库 + 运行时设置）；
- [ ] 未登录 curl `/ai-api/chat` 返回 401；
- [ ] 普通用户额度用完返回 429，管理员账号不受限；
- [ ] 后台设置页能读取后端参数，保存后立即生效；
- [ ] 管理员 Key/端口只读展示，页面源码和前端配置里搜不到任何密钥；
- [ ] 后台的单轮模型/工具调用上限和超时没有被误设成很大的值
      （它们是防止单次请求失控烧 token 的保险丝；`.env` 只提供初始默认值）。

---

## 附录：只读检查命令

把下面几条存成自己的“上线体检”：

```bash
systemctl is-active l2d-ai-backend
systemctl status l2d-ai-backend --no-pager
ss -lntp | grep 18080
curl -s http://127.0.0.1:18080/api/health
curl -s https://你的域名/ai-api/health
curl -i -X POST https://你的域名/wp-json/l2d-ai/v1/token
journalctl -u l2d-ai-backend --since "10 min ago" --no-pager
df -h /
free -h
timedatectl
```

这些命令都不会修改服务器，可以随时执行。
