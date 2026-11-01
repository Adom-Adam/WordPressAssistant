"""用户自己的 QQ 绑定：同步绑定码、查询、解绑。

绑定流程（两端各存一半）
------------------------
1. WordPress 生成 8 位绑定码，明文只存在 WP 的 usermeta 里，用户在
   `wp-admin/profile.php` 能看到；用户点“刷新”就是换一个新的。
2. WP 登录态下把绑定码明文推给这里。后端只存 SHA-256——即使数据库
   泄露，也拿不到能直接用的绑定码。
3. 用户在 QQ 私聊里发 `/绑定 1234-5678`，后端算出哈希去比对。

为什么解绑必须在网页端：QQ 那头没有可靠的身份证明。允许 QQ 侧
解绑/换绑，等于谁先私聊谁就能顶掉别人的绑定；网页端有 WordPress
登录态，是唯一可信的“我是我”。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import require_user
from app.auth import AuthUser
from app.credits import get_credit_store, hash_bind_code, is_valid_bind_code
from app.schemas import BindCodeUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/me/bind")
async def read_bind(user: AuthUser = Depends(require_user)) -> dict[str, object]:
    """当前用户绑定的 QQ；没绑定就是 null。

    个人资料页靠它显示“已绑定 12345”，也让前端知道该显示绑定还是解绑按钮。
    """
    try:
        qq_uin = await asyncio.to_thread(get_credit_store().qq_uin_of, user.id)
    except Exception:  # noqa: BLE001 - 查询失败不能伪装成“未绑定”
        logger.exception("读取 QQ 绑定失败 user=%s", user.id)
        raise HTTPException(status_code=503, detail="绑定信息暂时读取不到") from None
    return {"qq_uin": qq_uin}


@router.put("/me/bind-code", status_code=204)
async def sync_bind_code(
    payload: BindCodeUpdate,
    user: AuthUser = Depends(require_user),
) -> Response:
    """把 WP 侧的绑定码哈希后存下来，并同步管理员身份。

    `is_admin` 只认 JWT 的 admin 声明：WordPress 签发 token 时已经
    查过角色，这里再信任请求体就等于让任何登录用户给自己开管理员。
    附带效果：管理员刚被降权时，个人资料页打开一次才更新；可接受。
    """
    if not is_valid_bind_code(payload.code):
        raise HTTPException(status_code=400, detail="绑定码应为 8 位数字")

    try:
        await asyncio.to_thread(
            get_credit_store().set_bind_code,
            user.id,
            hash_bind_code(payload.code),
            user.is_admin,
        )
    except Exception:  # noqa: BLE001
        # 记 qq_uin/状态，不记绑定码：日志比数据库更常被打包外传。
        logger.exception("同步绑定码失败 user=%s", user.id)
        raise HTTPException(status_code=503, detail="绑定服务暂时不可用") from None

    return Response(status_code=204)


@router.delete("/me/bind", status_code=204)
async def unbind(user: AuthUser = Depends(require_user)) -> Response:
    """解绑自己的 QQ。没绑过也返回 204：结果都是“现在没绑”。"""
    try:
        await asyncio.to_thread(get_credit_store().unbind_user, user.id)
    except Exception:  # noqa: BLE001
        logger.exception("解绑 QQ 失败 user=%s", user.id)
        raise HTTPException(status_code=503, detail="绑定服务暂时不可用") from None

    return Response(status_code=204)
