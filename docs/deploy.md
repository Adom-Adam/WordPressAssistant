# 部署到阿里云服务器（宝塔 + Nginx）

目标形态：

```
公网 443 ── Nginx（宝塔站点）
              ├── /                → WordPress（PHP）
              └── /ai-api/         → 127.0.0.1:8000（Python FastAPI）
```

Python 服务只监听 `127.0.0.1`，不额外暴露端口；浏览器始终访问同域 `/ai-api`，无 CORS 问题。

## 0. 准备

- 服务器：Python 3.11+（`python3 --version`）；建议安装 `uv`：
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- 本地：Node 20+ / pnpm，用于构建前端。
- 不需要在服务器上装 Node（推荐本地构建后上传产物）。

## 1. 构建并安装 WordPress 插件

本地仓库根目录：

```bash
cd frontend
pnpm install
pnpm build          # 产物输出到 wordpress-plugin/wp-live2d-ai/assets/app/
```

把 `wordpress-plugin/wp-live2d-ai/` 整个目录上传到：

```
/www/wwwroot/<你的站点>/wp-content/plugins/wp-live2d-ai/
```

（宝塔文件管理里直接上传 zip 再解压也可以。）

然后在 WordPress 后台 **插件 → 启用 Live2D AI 小助手**。

> 模型要先放进 `frontend/public/live2d/models/` 再 build，否则插件里没有模型。
> `assets/app/` 已被 `.gitignore` 忽略，部署时是随构建产物而不是 Git 提交同步。

## 2. 部署 Python 后端

把 `backend/` 上传到服务器，例如 `/www/wwwroot/l2d-ai-backend/`。

```bash
cd /www/wwwroot/l2d-ai-backend
cp .env.example .env
vi .env                      # 填 DEEPSEEK_API_KEY
uv sync                      # 创建 .venv 并安装依赖
```

测试启动：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/api/health
```

应返回 `{"ok":true,"mock":false,...}`。`mock:true` 说明没读到 key。

### systemd 托管

创建 `/etc/systemd/system/l2d-ai.service`：

```ini
[Unit]
Description=Live2D AI assistant backend
After=network.target

[Service]
Type=simple
User=www
WorkingDirectory=/www/wwwroot/l2d-ai-backend
Environment=PATH=/www/wwwroot/l2d-ai-backend/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/www/wwwroot/l2d-ai-backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now l2d-ai
sudo systemctl status l2d-ai
```

> 如果 `uv` 不在默认路径，用 `which uv` 的结果调整 `Environment`。
> 也可以在宝塔「Python 项目管理器」里用同样的启动命令托管，二选一。

## 3. Nginx 反向代理（宝塔）

在宝塔 → 网站 → 你的站点 → 设置 → 配置文件，在 `server { ... }` 内、PHP location 之前加：

```nginx
location /ai-api/ {
    proxy_pass http://127.0.0.1:8000/api/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE 流式输出必须关掉缓冲
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection "";
    chunked_transfer_encoding on;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

保存后 Nginx 会重载。验证：

```bash
curl -N -X POST https://你的域名/ai-api/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

应看到逐条 `data: {...}` 输出。

> 注意 `proxy_pass http://127.0.0.1:8000/api/;` 末尾的 `/`：它把 `/ai-api/chat`
> 重写为 `/api/chat`。少写斜杠会变成 `/ai-api/api/chat`。

## 4. WordPress 侧配置

后台 **设置 → Live2D 小助手**：

- AI 服务地址保持默认 `/ai-api` 即可（同域反代）；
- 如果后端独立域名，才改成 `https://ai.example.com/api`，同时在后端 `.env` 的
  `CORS_ORIGINS` 加上博客域名。

打开博客任意页面，左下角应出现小助手。检查项：

1. 浏览器控制台无 404：`live2d/core/live2dcubismcore.min.js`、`app.js`、`app.css`；
2. Network 里 `/ai-api/chat` 返回 `text/event-stream`；
3. 若回复一次性出现而不是逐字，检查 Nginx 是否关掉了 `proxy_buffering`（宝塔/CDN 都可能加缓冲）。

## 5. 更新流程

```bash
# 本地
cd frontend && pnpm build
# 上传（示例）
scp -r wordpress-plugin/wp-live2d-ai root@服务器:/www/wwwroot/<站点>/wp-content/plugins/
```

后端代码变更：

```bash
cd /www/wwwroot/l2d-ai-backend
git pull   # 或重新上传
uv sync
sudo systemctl restart l2d-ai
```

前端静态资源带 `app.js` 固定文件名，插件用版本号作为查询参数；更新后若浏览器缓存旧文件，
在插件里把 `L2D_AI_Plugin::VERSION` 或 WordPress 版本号递增即可。

## 6. 常见问题

| 现象 | 排查 |
| --- | --- |
| 左下角什么都没有 | 插件未启用；`assets/app/app.js` 没上传；页面被过滤器 `l2d_ai_should_load` 关掉 |
| 有按钮没有形象 | `manifest.json` 为空或模型路径 404；确认模型是 Cubism 3/4、`.model3.json` 存在 |
| 控制台报 Cubism Core 未定义 | Cubism Core 未加载或被缓存；确认 `live2d/core/live2dcubismcore.min.js` 可访问 |
| 回复不是流式 | Nginx/宝塔/CDN 缓冲；关闭 `proxy_buffering` 并设置 `X-Accel-Buffering: no` |
| `mock: true` | 后端 `.env` 没有 `DEEPSEEK_API_KEY`，或 systemd 未加载该文件 |
| 语音按钮无反应 | Web Speech API 仅 Chrome/Edge 等支持；HTTPS 下才可用 |

## 7. 安全清单

- `DEEPSEEK_API_KEY` 只写在服务器 `.env`，权限 `600`，不提交 Git；
- 8000 端口只监听 `127.0.0.1`，安全组不要放行；
- 上线后可以在 Nginx 加限流：`limit_req_zone` + `limit_req`；
- 如后端独立域名，严格配置 `CORS_ORIGINS`，不要用 `*`。
