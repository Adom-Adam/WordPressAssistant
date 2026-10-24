=== Live2D AI 小助手 ===
Contributors: adom
Requires at least: 6.0
Requires PHP: 7.4
Stable tag: 0.1.0
License: GPLv2 or later

在博客左下角显示 Live2D AI 助手，对话由独立的 Python 服务提供。

== 安装 ==

1. 先在仓库根目录执行 `pnpm --dir frontend build`，生成 `assets/app/` 构建产物。
2. 把整个 `wp-live2d-ai` 文件夹上传到 WordPress 的 `wp-content/plugins/`。
3. 在后台「插件」中启用。
4. 打开「设置 → Live2D 小助手」，确认 AI 服务地址（默认 `/ai-api`）。

== 反向代理 ==

生产环境建议用 Nginx 把 `/ai-api/` 转发到 Python 服务，避免跨域：

    location /ai-api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;          # SSE 流式输出需要
        proxy_read_timeout 300s;
    }

== 模型 ==

把 Cubism 3/4 模型放到 `frontend/public/live2d/models/<模型名>/`，
构建时会自动复制到插件的 `assets/app/live2d/models/`。
