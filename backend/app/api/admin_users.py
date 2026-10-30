"""管理员侧的用户档案、等级、额外额度与兑换码接口。

====================================================================
这里为什么看不到邮箱
====================================================================
用户的邮箱、昵称、注册时间都在 WordPress 的 wp_users 表里；后端
按设计只保存“与聊天相关”的东西（等级、存量、备注）。两边通过
WordPress 用户 ID 对齐，合并发生在 WP 后台页（PHP 那边）——那里本来
就有完整权限。后端接口不返回邮箱，即使 token 泄露也拿不到身份信息。

====================================================================
为什么列表是“后端有记录的档案”
====================================================================
后端不知道 WordPress 注册了多少人，也不该为查一个用户去连数据库。
所以 GET /api/admin/users 只返回“有人动过”的用户（升过级、发过额度、
写过备注）。后台页拿到 WordPress 用户列表后，用这份快照去合并；
快照里没有的人按默认值（user / 0 条）渲染，不需要额外请求。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.auth import AuthUser
from app.credits import get_credit_store
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store
from app.schemas import CodeCreate, CreditGrant, UserPatch

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/admin/users")
async def list_users(
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """返回后端有记录的用户档案，以及当前的额度上限。"""
    credits = get_credit_store()
    runtime = get_runtime_store().load()

    try:
        user_ids = await asyncio.to_thread(credits.known_user_ids)
        snapshot = await asyncio.to_thread(credits.profile_snapshot, user_ids)
        daily = await asyncio.to_thread(get_quota_store().usage_snapshot, user_ids)
    except Exception:  # noqa: BLE001 - 数据库故障时给后台页一个明确错误
        logger.exception("读取用户档案失败")
        raise HTTPException(status_code=503, detail="用户数据暂时读取不到") from None

    users = [
        {
            "user_id": user_id,
            "tier": profile.tier,
            "tier_expires_at": profile.tier_expires_at,
            "credit": profile.credit,
            "daily_used": daily.get(user_id, 0),
            "note": profile.note,
            "qq_uin": profile.qq_uin,
        }
        for user_id, profile in snapshot.items()
    ]
    users.sort(key=lambda item: item["user_id"])

    return {
        "users": users,
        "day": get_quota_store().today(),
        "limits": {
            "user": runtime.daily_quota,
            "member": runtime.member_daily_quota,
        },
    }


@router.patch("/admin/users/{user_id}")
async def patch_user(
    user_id: int,
    patch: UserPatch,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """调整等级或备注。

    会员必须给天数：等级是有期限的，没期限就无法自动降级，
    也说不清“续期”从哪天算起。
    """
    credits = get_credit_store()

    if patch.tier == "member" and patch.duration_days is None:
        raise HTTPException(status_code=422, detail="会员必须给出天数")

    tier: str | None = None
    expires: str | None = None
    if patch.tier is not None:
        try:
            expires = await asyncio.to_thread(
                credits.set_tier, user_id, patch.tier, patch.duration_days
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        tier = patch.tier

    if patch.note is not None:
        try:
            await asyncio.to_thread(credits.set_note, user_id, patch.note)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    profile = await asyncio.to_thread(credits.profile_snapshot, [user_id])
    current = profile[user_id]

    return {
        "user_id": user_id,
        "tier": tier or current.tier,
        "tier_expires_at": expires if patch.tier is not None else current.tier_expires_at,
        "credit": current.credit,
        "note": current.note,
    }


@router.post("/admin/users/{user_id}/credits")
async def grant_credits(
    user_id: int,
    grant: CreditGrant,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """发放或扣减额外额度。"""
    if grant.delta == 0:
        raise HTTPException(status_code=422, detail="发放数量不能是 0")

    try:
        balance = await asyncio.to_thread(
            get_credit_store().grant_credits,
            user_id,
            grant.delta,
            grant.reason,
        )
    except ValueError as exc:
        # 余额不足、超过单用户上限都属于“输入不对”，不是服务故障。
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:  # noqa: BLE001
        logger.exception("发放额外额度失败 user=%s", user_id)
        raise HTTPException(status_code=503, detail="额度服务暂时不可用") from None

    logger.info(
        "管理员发放额外额度 user=%s delta=%s balance=%s",
        user_id,
        grant.delta,
        balance,
    )
    return {"user_id": user_id, "credit": balance}


@router.post("/admin/codes")
async def create_codes(
    payload: CodeCreate,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """批量生成兑换码，返回明文（只在这里出现一次）。"""
    try:
        codes = await asyncio.to_thread(
            get_credit_store().create_codes,
            kind=payload.kind,
            value=payload.value,
            count=payload.count,
            max_uses=payload.max_uses,
            expires_at=payload.expires_at,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:  # noqa: BLE001
        logger.exception("生成兑换码失败")
        raise HTTPException(status_code=503, detail="兑换码服务暂时不可用") from None

    logger.info("管理员生成兑换码 kind=%s count=%s", payload.kind, len(codes))
    return {"codes": codes}


@router.get("/admin/codes")
async def list_codes(
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """最近 50 个兑换码及其使用情况。"""
    try:
        codes = await asyncio.to_thread(get_credit_store().recent_codes, 50)
    except Exception:  # noqa: BLE001
        logger.exception("读取兑换码失败")
        raise HTTPException(status_code=503, detail="兑换码服务暂时不可用") from None
    return {"codes": codes}
