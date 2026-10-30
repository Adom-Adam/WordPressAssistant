"""兑换码接口：登录用户把兑换码换成额外额度或会员天数。

====================================================================
为什么要限流
====================================================================
兑换码只有 16 个字符，但可猜的空间远大于“几十个码”。不过防爆破
依然值得做：攻击者拿到登录态后可以不停地 POST，靠服务器日志和
数据库计数器也能看出异常。这里的规则是：

    同一个用户 10 分钟内最多失败 5 次，超过就 429。

为什么按用户而不是按 IP：每个人都是登录用户，用户 ID 来自签名过的
JWT，伪造不了；IP 在移动网络下会共享，容易误伤。

====================================================================
为什么计数器放在内存里
====================================================================
它只是“防手滑和轻度爆破”的减速带，不是安全边界——真正的边界是
兑换码本身足够长、单用户单次、有使用次数上限。内存计数在重启后清零，
最坏情况是攻击者多试几次；换成 Redis/数据库反而给每个请求增加了写入。

多进程部署时每个进程各记各的，实际阈值会变成 N 倍，同样可以接受。
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_user
from app.auth import AuthUser
from app.credits import RedeemError, get_credit_store
from app.schemas import RedeemRequest

logger = logging.getLogger(__name__)

router = APIRouter()

# 失败窗口与阈值。改这里之前先想想：太严会把正常用户锁在门外
# （比如从聊天记录里翻错了码），太松就失去意义。
_WINDOW_SECONDS = 600
_MAX_FAILURES = 5

# user_id -> 失败时间戳列表（time.monotonic()，不受系统改时间影响）
_failures: dict[int, list[float]] = {}


def _record_failure(user_id: int) -> None:
    stamps = [t for t in _failures.get(user_id, []) if time.monotonic() - t < _WINDOW_SECONDS]
    stamps.append(time.monotonic())
    _failures[user_id] = stamps


def _too_many(user_id: int) -> bool:
    stamps = [t for t in _failures.get(user_id, []) if time.monotonic() - t < _WINDOW_SECONDS]
    if not stamps:
        _failures.pop(user_id, None)
    else:
        _failures[user_id] = stamps
    return len(stamps) >= _MAX_FAILURES


@router.post("/redeem")
async def redeem(
    request: RedeemRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, object]:
    """兑换一个码，返回兑换后的身份与余额。

    成功时把该用户的失败计数清空：能兑成功说明他没有在瞎猜，
    继续锁着他没有意义。
    """
    if _too_many(user.id):
        logger.info("兑换失败次数过多 user=%s", user.id)
        raise HTTPException(
            status_code=429,
            detail="试错次数太多了，过十分钟再来试试～",
            headers={"Retry-After": str(_WINDOW_SECONDS)},
        )

    try:
        # sqlite3 是同步库，放线程池里执行，别卡住事件循环。
        result = await asyncio.to_thread(
            get_credit_store().redeem, user.id, request.code
        )
    except RedeemError as exc:
        _record_failure(user.id)
        # 400 而不是 404：码不存在、已用完、过期对用户是同一件事，
        # 详情里的文案已经说清了是哪一种。
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:  # noqa: BLE001 - 数据库故障不该让用户看到堆栈
        logger.exception("兑换失败 user=%s", user.id)
        raise HTTPException(
            status_code=503, detail="兑换服务暂时不可用，请稍后再试"
        ) from None

    _failures.pop(user.id, None)
    logger.info("兑换成功 user=%s kind=%s", user.id, result.kind)

    return {
        "kind": result.kind,
        "message": result.message,
        "tier": result.tier,
        "tier_expires_at": result.tier_expires_at,
        "credit_balance": result.balance,
    }
