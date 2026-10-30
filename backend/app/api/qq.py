"""QQ 通道的 HTTP 入口。

NapCat 通过“反向 HTTP”把群消息推到这里。这个接口不是给浏览器用的，
所以不校验 WordPress JWT，而是校验一个只属于 OneBot 的 token：

- `QQ_EVENT_TOKEN`：NapCat 请求头里带的 token；
- 支持 `Authorization: Bearer <token>` 和 `X-OneBot-Token: <token>` 两种写法，
  不同 OneBot 实现的配置项名称不一样，两种都兼容；
- token 为空且不是 mock 模式时，直接 503，避免接口在公网上无鉴权可用。
"""

from __future__ import annotations

import hmac
import logging
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.channels.qq.adapter import QQAdapter
from app.channels.qq.client import build_onebot_client
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@lru_cache(maxsize=1)
def get_qq_adapter() -> QQAdapter:
    """全局唯一的适配器。

    适配器里保存了内存中的短期对话历史，所以不能在每次请求时新建实例。
    测试如果需要干净状态，调用 `get_qq_adapter.cache_clear()`。
    """
    settings = get_settings()
    return QQAdapter(settings, build_onebot_client(settings))


def _extract_token(request: Request) -> str:
    """从两种常见请求头里取 OneBot token。"""
    direct = request.headers.get("x-onebot-token", "").strip()
    if direct:
        return direct

    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


@router.post("/channels/qq/events")
async def receive_qq_event(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """接收 NapCat 推送的事件，快速返回 204。

    为什么用后台任务：
    NapCat 对事件回调有超时限制；如果我们在请求里等完整个 AI 回复，
    它会认为回调失败并重试，导致重复回复。先返回 204，再异步处理，
    是 OneBot 适配器的常规做法。
    """
    settings = get_settings()
    if not settings.qq_enabled:
        # 关闭时连路由存在与否都不应该暴露；404 比 403 更不引人注意。
        raise HTTPException(status_code=404, detail="QQ channel is disabled")

    expected = settings.qq_event_token.strip()
    if expected:
        supplied = _extract_token(request)
        # compare_digest 是常数时间比较，避免通过响应时间猜 token。
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid OneBot token")
    elif not settings.qq_mock_mode:
        # 生产环境没配 token 就不开放事件入口。
        # 本地 mock 模式允许空 token，方便 Windows 上直接用 curl/脚本联调。
        raise HTTPException(
            status_code=503,
            detail="QQ_EVENT_TOKEN is not configured",
        )

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="event must be a JSON object")

    background_tasks.add_task(get_qq_adapter().handle_event, payload)
    return Response(status_code=204)
