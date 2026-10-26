"""额度查询接口。

聊天响应头里其实已经带了剩余次数，但响应头只有“聊过之后”才有。
用户刚打开面板时也需要知道今天还剩几条，所以单独提供一个只读接口。

这个接口**不消耗额度**，内部只做 SELECT。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_user
from app.auth import AuthUser
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/quota")
async def read_quota(user: AuthUser = Depends(require_user)) -> dict[str, object]:
    """返回当前用户的额度状态。

    为什么用 asyncio.to_thread：
    和聊天接口一样，sqlite3 是同步阻塞库；放到线程池里查，
    避免数据库短时锁住时卡住整个事件循环。
    """
    runtime = get_runtime_store().load()

    try:
        status = await asyncio.to_thread(
            get_quota_store().status,
            user.id,
            runtime.daily_quota,
            is_admin=user.is_admin,
            global_limit=runtime.global_daily_quota,
        )
    except Exception:  # noqa: BLE001 - 查询失败不能让前端误以为额度为 0
        logger.exception("查询额度失败 user=%s", user.id)
        raise HTTPException(status_code=503, detail="额度服务暂时不可用") from None

    return {
        "day": status.day,
        "is_admin": status.is_admin,
        "unlimited": status.unlimited,
        "limit": status.limit,
        "used": status.used,
        "remaining": status.remaining,
        "global_limit": status.global_limit,
        "global_used": status.global_used,
        "global_remaining": status.global_remaining,
    }
