"""FastAPI 应用入口。

启动方式（在 backend/ 目录下）：

    uv run uvicorn app.main:app --reload --port 18080

- `app.main` 是模块路径（app/main.py）；
- `app`    是模块里的 FastAPI 实例；
- `--reload` 只在开发时用，代码改动后自动重启（不要用于生产）。

启动后可访问：
- http://127.0.0.1:18080/docs     自动生成的交互式 API 文档
- http://127.0.0.1:18080/api/health  健康检查
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.api.quota import router as quota_router
from app.config import get_settings
from app.runtime_settings import get_runtime_store

# logging.basicConfig 配置根日志器：
# 之后任何模块里的 logger.info/exception 都会按这个格式输出。
# 生产环境可以换成 JSON 日志或接入日志收集系统。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

settings = get_settings()

# 启动时就检查登录密钥，而不是等第一个用户来聊天才发现。
# 这里只记警告、不让进程退出：健康检查、知识库同步等功能仍然可用；
# 但 /api/chat 会明确返回 503（见 app/api/deps.py 的 require_user）。
if not settings.jwt_configured:
    logging.getLogger(__name__).warning(
        "L2D_AI_JWT_SECRET 未配置或短于 32 字符：所有聊天请求都会返回 503"
    )

app = FastAPI(
    title="Live2D AI Assistant Backend",
    version="0.1.0",
    description="博客 Live2D 小助手的 AI 后端（LangChain + DeepSeek）",
)

# CORS（跨域资源共享）中间件。
# 浏览器里 5173 端口的前端访问 18080 端口的后端属于“跨域”，
# 没有这个中间件，浏览器会拦截响应。
# 生产环境用 Nginx 同域反代 /ai-api 时，其实不会触发跨域；
# 这里的白名单主要服务本地开发，避免使用 "*" 放开所有来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
    # 跨域部署时，浏览器默认读不到自定义响应头；额度提示依赖它们。
    # 同域反代不受影响，这里主要是让“后端独立域名”的部署也能工作。
    expose_headers=[
        "X-Quota-Limit",
        "X-Quota-Remaining",
        "X-Quota-Unlimited",
        "X-Global-Quota-Limit",
        "X-Global-Quota-Remaining",
        "Retry-After",
    ],
)

# 把路由挂到 /api 前缀下：
#   装饰器写的是 @router.post("/chat")
#   最终地址就是 POST /api/chat
app.include_router(chat_router, prefix="/api")
app.include_router(quota_router, prefix="/api")
# 管理接口需要 JWT 里的 admin=true，普通用户访问会得到 403。
app.include_router(admin_router, prefix="/api")


@app.get("/api/health")
async def health() -> dict[str, object]:
    """健康检查。

    返回 `mock` 字段可以让前端/运维一眼看出：
    当前是真实调用 DeepSeek，还是因为没有 Key 进入了假数据模式。
    """
    # 模型名可能被管理员在后台改过，所以以运行时设置里的值为准。
    return {
        "ok": True,
        "mock": settings.mock_enabled,
        "model": get_runtime_store().load().model,
        # 只暴露“有没有配好”，不暴露密钥本身。
        # 部署后用 curl 看一眼就知道聊天接口会不会 503。
        "auth_configured": settings.jwt_configured,
    }


@app.get("/")
async def root() -> dict[str, str]:
    """根路径：告诉访问者这个服务是什么、文档在哪。"""
    return {"service": "l2d-ai-backend", "docs": "/docs", "health": "/api/health"}
