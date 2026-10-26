# 从零部署到服务器：前后端移植实操

这篇文档面向第一次把项目搬上服务器的人，按顺序覆盖：

- 服务器上要建哪些目录、用什么账号运行；
- 后端 `.env`、JWT 密钥、知识库怎么配；
- systemd 和 Nginx 怎么写；
- WordPress 插件怎么上传、`wp-config.php` 怎么改；
- 哪些设置必须**手动**在服务器上完成；
- 怎么验证、更新、回滚和排查。

> 适用版本：插件 `0.2.0`。
>
> **当前边界（很重要）**：登录门和 JWT 签发已经完成，但后端还没有校验 token，也没有每日额度。
> 也就是说，现在绕过前端直接请求 `/ai-api/chat` 仍然能聊天。等第二步完成后，
> “必须登录 + 每日额度”才会真正生效。本文会明确标出这一点，避免误以为已经安全。

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
            ├─ backend/.env    ← DeepSeek Key、JWT 密钥
            └─ backend/data/   ← 知识库 SQLite
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
| 仓库代码 | `/opt/l2d-ai/repo` | `l2dai:l2dai` | 不要放进网站根目录 |
| 后端配置 | `/opt/l2d-ai/repo/backend/.env` | `l2dai:l2dai`，权限 `640` | 含 API Key 和 JWT 密钥 |
| 知识库 | `/opt/l2d-ai/repo/backend/data/` | `l2dai:l2dai` | SQLite，可随时重建 |
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
git clone --depth 1 https://github.com/Adom-Adam/WordPressAssistant.git /opt/l2d-ai/repo
chown -R l2dai:l2dai /opt/l2d-ai/repo
```

安装依赖：

```bash
cd /opt/l2d-ai/repo/backend
runuser -u l2dai -- env UV_CACHE_DIR=/opt/l2d-ai/.uv-cache /usr/local/bin/uv sync --no-dev
```

- `uv sync` 会按 `pyproject.toml` / `uv.lock` 建 `.venv`，并自动下载合适的 Python（3.11+）；
- `--no-dev` 不安装 pytest 等开发依赖；
- `pyproject.toml` 里配置了清华 PyPI 镜像，国内下载更快；
- 用 `runuser -u l2dai` 是为了让 `.venv` 和缓存都归运行用户所有。
  有 sudo 时用 `sudo -u l2dai ...` 也可以。

建数据目录：

```bash
install -d -o l2dai -g l2dai /opt/l2d-ai/repo/backend/data
```

> **不要上传本地的 `.venv`、`node_modules`、`data/`**。`.venv` 里是 Windows 的二进制，
> 到 Linux 不能直接用；知识库建议在服务器上重新同步。

---

## 5. 写后端 `.env`

```bash
cd /opt/l2d-ai/repo/backend
install -o l2dai -g l2dai -m 640 .env.example .env
nano .env          # 没有 nano 就用 vi，或在宝塔文件管理里编辑
```

至少要改这些：

| 键 | 示例 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | `sk-...` | 只写服务器；不填会进入 mock 模式 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 以后可在后台配置（第三步） |
| `WP_SITE_URL` | `https://你的域名` | 知识库同步要抓的站点 |
| `L2D_AI_JWT_SECRET` | 64 位十六进制 | 必须和 `wp-config.php` 完全一致 |
| `L2D_AI_JWT_ISSUER` | `l2d-ai-wordpress` | 与插件常量一致 |
| `L2D_AI_JWT_AUDIENCE` | `l2d-ai-backend` | 与插件常量一致 |
| `L2D_AI_JWT_TTL_SECONDS` | `900` | 与插件签发有效期一致 |
| `MOCK_MODE` | `false` | 生产环境应为 false |

`CORS_ORIGINS` 保持默认即可：生产环境走同域 `/ai-api/`，不会触发跨域。
只有后端独立域名时才需要加博客域名。

> `.env` 权限必须是 `640` 或 `600`。后端进程按**当前工作目录**找 `.env`，
> 所以 systemd 的 `WorkingDirectory` 必须是 `backend/`，否则会出现“明明配了 Key 还是 mock”。

---

## 6. 对齐 JWT 共享密钥（手动）

生成一次（在服务器终端执行）：

```bash
openssl rand -hex 32
```

把结果分别写进两个地方：

1. **后端** `/opt/l2d-ai/repo/backend/.env`
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

> 目前后端还没有读取这个密钥；第二步接入校验后，它才会真正生效。
> 但现在就配好，可以避免以后上线时漏配。

---

## 7. 建立知识库索引

先只建文字索引，确认网络和文章抓取正常：

```bash
cd /opt/l2d-ai/repo/backend
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
WorkingDirectory=/opt/l2d-ai/repo/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/l2d-ai/repo/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18080 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
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

启用：

```bash
systemctl daemon-reload
systemctl enable --now l2d-ai-backend
systemctl status l2d-ai-backend --no-pager
curl http://127.0.0.1:18080/api/health
```

健康检查应返回：

```json
{"ok":true,"mock":false,"model":"deepseek-flash"}
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

WordPress 的 token 接口依赖 REST API 可用：

```bash
curl -i https://你的域名/wp-json/
```

如果 404：

- 后台 → 设置 → 固定链接，改成“文章名”后保存；
- 或检查安全插件是否禁用了 REST API；
- 没有伪静态时，插件会用 `?rest_route=/l2d-ai/v1/token` 这种形式，功能仍然可用。

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
curl -i -X POST https://你的域名/wp-json/l2d-ai/v1/token
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

### 11.5 当前阶段的限制

- 直接 `curl` 调 `/ai-api/chat` 仍然能拿到回复，因为后端还没校验 token；
- 每日额度、管理员不限量都还没实现；
- 这些是第二步的工作，完成后上面两条才会改变。

---

## 12. 更新、回滚与备份

### 12.1 更新后端

```bash
cd /opt/l2d-ai/repo
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
  /opt/l2d-ai/repo/backend/.env \
  /opt/l2d-ai/repo/backend/data
```

WordPress 侧按宝塔的数据库 + 站点备份策略走。
升级前先备份，尤其是第二步加入额度数据库之后。

### 12.4 轮换密钥

1. 生成新值；
2. 同时更新后端 `.env` 和 `wp-config.php`；
3. `systemctl restart l2d-ai-backend`；
4. 旧 token 立即失效，用户刷新页面即可换新；
5. 如果只改了其中一处，所有聊天请求会在第二步之后返回 401。

---

## 13. 手动设置总表

下面这些**必须在服务器上手动完成**，代码和构建不会替你做：

| # | 设置 | 位置 | 说明 |
| --- | --- | --- | --- |
| 1 | 运行用户 | `l2dai` | 不用 root/www 跑后端 |
| 2 | 目录与属主 | `/opt/l2d-ai` | 后端不能放在网站根目录 |
| 3 | `uv` 安装 | `/usr/local/bin/uv` | systemd/普通用户都能找到 |
| 4 | 后端 `.env` | `backend/.env`，`640` | DeepSeek Key、JWT 密钥、站点地址 |
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

---

## 14. 常见问题

| 现象 | 可能原因 | 排查 |
| --- | --- | --- |
| 设置页提示密钥未配置 | `wp-config.php` 没有 `define` 或拼写错误 | 确认常量名是 `L2D_AI_JWT_SECRET`，位置在 stop editing 之前 |
| token 接口 500 | 插件读不到密钥，或密钥短于 32 字符 | 看 WordPress 调试日志；设置页会提示 |
| token 接口 403 | nonce 过期、页面被缓存成访客版、WAF 拦截 | 刷新页面；排除缓存；检查 `X-WP-Nonce` |
| 登录后仍是登录门 | 页面缓存把访客 HTML 发给了登录用户 | 缓存插件按登录 Cookie 排除 |
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
- [ ] 插件目录属主 `www:www`，没有多余的可写权限；
- [ ] token 接口能被未登录请求正确拒绝（401）；
- [ ] 缓存/CDN 排除了 `/wp-json/l2d-ai/*` 和 `/ai-api/*`；
- [ ] 已配置备份，知道怎么恢复 `.env` 和 `data/`；
- [ ] 清楚“第二步完成前后端仍不校验 token”这个事实。

> 第二步会加入：后端 JWT 验签、按 WordPress 用户 ID 的每日额度、管理员绕过、
> 失败退款与并发扣减。完成后，本文第 11.5 节的限制才会消失。

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
