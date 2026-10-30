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
from app.credits import get_credit_store
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
        # 管理员单独显示为 "admin"，前端据此隐藏“今日剩余”那一条，
        # 而不是把 unlimited 和 tier 两处判断混在一起。
        if user.is_admin:
            tier, tier_expires_at = "admin", None
            limit = runtime.daily_quota
        else:
            tier, tier_expires_at = await asyncio.to_thread(
                get_credit_store().tier_of, user.id
            )
            limit = (
                runtime.member_daily_quota
                if tier == "member"
                else runtime.daily_quota
            )

        status = await asyncio.to_thread(
            get_quota_store().status,
            user.id,
            limit,
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
        "tier": tier,
        "tier_expires_at": tier_expires_at,
        "credit_balance": status.credit_balance,
    }
