"""注销账号时删除自己的记忆。

WordPress 账号是唯一身份，QQ 只是可以更换的登录方式：解绑/换绑都不
删除记忆（换了 QQ 还是同一个人）；只有注销账号才删除。

为什么必须由 WordPress 侧调用：后端只知道 `user.id`，不知道用户什么
时候删号。插件在 `wp_delete_user()` 之前调用这里；失败时阻止注销，
避免出现“账号没了、记忆还在”的半注销状态。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import require_user
from app.auth import AuthUser
from app.memory import pipeline

logger = logging.getLogger(__name__)

router = APIRouter()


@router.delete("/me/memory", status_code=204)
async def delete_my_memory(user: AuthUser = Depends(require_user)) -> Response:
    """删除当前账号名下的全部记忆、事件与身份关联。

    不解绑 QQ（那是 `DELETE /me/bind` 的职责），也不退还额度。
    """
    try:
        result = await asyncio.to_thread(pipeline.delete_user_memory, user.id)
    except Exception:  # noqa: BLE001 - 删除失败必须让调用方知道
        logger.exception("删除账号记忆失败 user=%s", user.id)
        raise HTTPException(status_code=503, detail="记忆数据暂时无法删除，请稍后再试") from None

    logger.info(
        "删除账号记忆 user=%s memories=%s events=%s",
        user.id,
        result.get("memories", 0),
        result.get("events", 0),
    )
    return Response(status_code=204)
