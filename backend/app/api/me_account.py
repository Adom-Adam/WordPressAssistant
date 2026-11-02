"""账号自己的额度继承。

注销再注册不该重置额度，但“同一个邮箱”这个判断只能在 WordPress 侧做：
后端按设计不存邮箱，也不该为了这件事开始存。所以流程是：

1. WordPress 删除用户时，把“邮箱哈希 → 旧用户 ID”记在自己的 options 里；
2. 新账号第一次换 token / 打开个人资料页时，插件用专用短时 JWT
   把旧用户 ID 放进 `transfer_from` claim；
3. 这里只信签名过的 claim，把旧 ID 的额度并到新 ID，成功后插件删除记录。

旧 ID 不放在请求体里：请求体谁都能改，claim 改不了。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_user
from app.auth import AuthUser
from app.credits import get_credit_store
from app.quota import get_quota_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/me/inherit-quota")
async def inherit_quota(user: AuthUser = Depends(require_user)) -> dict[str, object]:
    """把已注销账号的额度并到当前账号。

    没有 `transfer_from` 的普通 token 一律 403：这个接口只服务于
    “插件刚发现这个邮箱有注销记录”的一次性调用。
    """
    if not user.transfer_from:
        raise HTTPException(status_code=403, detail="没有可继承的账号记录")

    def transfer() -> object:
        # daily_usage 由 QuotaStore 建表；如果这个进程第一次访问就是继承接口，
        # 必须先让它初始化，否则 CreditStore 会看不到这张表。
        get_quota_store()
        return get_credit_store().inherit_identity(user.id, user.transfer_from)

    try:
        result = await asyncio.to_thread(transfer)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:  # noqa: BLE001 - 数据库故障不能伪装成“没有记录”
        logger.exception("额度继承失败 user=%s", user.id)
        raise HTTPException(status_code=503, detail="额度服务暂时不可用") from None

    return {
        "transferred": list(result.transferred),
        "already": list(result.already),
        "conflicts": list(result.conflicts),
    }
