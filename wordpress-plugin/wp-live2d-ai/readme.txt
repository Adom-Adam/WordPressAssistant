=== Live2D AI 小助手 ===
Contributors: adom
Requires at least: 6.0
Requires PHP: 7.4
Stable tag: 0.3.1
License: GPLv2 or later

在博客左下角显示 Live2D AI 助手，对话由独立的 Python 服务提供。

== 安装 ==

1. 先在仓库根目录执行 `pnpm --dir frontend build`，生成 `assets/app/` 构建产物。
2. 把整个 `wp-live2d-ai` 文件夹上传到 WordPress 的 `wp-content/plugins/`。
3. 在后台「插件」中启用。
4. 打开「设置 → Live2D 小助手」，确认 AI 服务地址（默认 `/ai-api`）。

== 登录凭证 ==

0.2.0 起，小助手会读取 WordPress 的登录状态，并向 `wp-json/l2d-ai/v1/token`
换取一个 15 分钟有效的 JWT。签名密钥需要手动配置：

1. 执行 `openssl rand -hex 32` 生成随机值。
2. 在 `wp-config.php` 中添加 `define( 'L2D_AI_JWT_SECRET', '生成的值' );`。
3. 把同一个值写进后端 `.env` 的 `L2D_AI_JWT_SECRET`。

两处不一致时，后端会拒绝所有 token。设置页会提示密钥是否已配置，
但永远不会显示密钥内容。

== 反向代理 ==

生产环境建议用 Nginx 把 `/ai-api/` 转发到 Python 服务，避免跨域：

    location /ai-api/ {
        proxy_pass http://127.0.0.1:18080/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;          # SSE 流式输出需要
        proxy_read_timeout 300s;
    }

== 模型 ==

把 Cubism 3/4 模型放到 `frontend/public/live2d/models/<模型名>/`，
构建时会自动复制到插件的 `assets/app/live2d/models/`。

== 更新日志 ==

= 0.3.1 =

- 登录后隐藏 Argon 导航里的“登录 / 注册”链接。
- 登录后在主导航末尾追加带 nonce 的“退出”链接。
