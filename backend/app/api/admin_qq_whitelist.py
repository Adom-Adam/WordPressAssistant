"""后台 QQ 白名单接口。

三种操作对应三种意图，不要合并：
- 加入 / 改角色：这个号按“已登记”对待；
- 移出名单：不再特殊对待，历史数据留着（休眠）；
- 彻底删除：把这个人名下的记忆、事件、关系一起清掉（隐私要求）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.auth.jwt import AuthUser
from app.credits import get_credit_store
from app.memory import db as memory_db
from app.memory import identity, merge
from app.persona.store import get_persona_store
from app.qq_whitelist import add_entry, entry_for, list_entries, remove_entry
from app.relationship.state import role_config, role_for
from app.schemas import QQWhitelistAdd, QQWhitelistUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def _cap(role: str, dynamics: object) -> int | float:
    cap = role_config(role, dynamics).cap
    return int(cap) if float(cap).is_integer() else float(cap)


def _serialize(conn, row, dynamics: object) -> dict[str, object]:
    uin = str(row["qq_uin"])
    # 角色以 `persons` 为准，不是以名单行为准：`role_for` 顺手把脏数据
    # 归一到 normal，和实际判定保持一致。
    role = role_for(conn, f"qq:{uin}")
    binding = get_credit_store().binding_of_qq(uin)
    return {
        "qq_uin": uin,
        "role": role,
        "cap": _cap(role, dynamics),
        "note": str(row["note"] or ""),
        "added_by": str(row["added_by"] or ""),
        "added_at": str(row["added_at"]),
        "bound_user_id": binding.user_id if binding is not None else None,
    }


def _connect():
    conn = memory_db.connect()
    memory_db.init_schema(conn)
    return conn


def _read() -> dict[str, object]:
    conn = _connect()
    try:
        rows = list_entries(conn)
        dynamics = get_persona_store().load_active().dynamics
        entries = [_serialize(conn, row, dynamics) for row in rows]
    finally:
        conn.close()
    return {"entries": entries}


def _one(qq_uin: str) -> dict[str, object]:
    conn = _connect()
    try:
        row = entry_for(conn, qq_uin)
        if row is None:
            raise HTTPException(status_code=404, detail="QQ 不在白名单里")
        dynamics = get_persona_store().load_active().dynamics
        return _serialize(conn, row, dynamics)
    finally:
        conn.close()


def _set_role(conn, qq_uin: str, role: str) -> None:
    person_key = identity.person_key_qq(qq_uin)
    identity.touch_person(conn, person_key, qq_uin=qq_uin)
    conn.execute(
        "UPDATE persons SET role = ? WHERE person_key = ?", (role, person_key)
    )
    conn.commit()


def _add(payload: QQWhitelistAdd, admin_user_id: int) -> None:
    if get_credit_store().binding_of_qq(payload.qq_uin) is not None:
        # 绑定过的号身份来自账号，白名单对它没有意义；允许加进来会让
        # 后台出现两个互相矛盾的角色设置。
        raise HTTPException(status_code=409, detail="这个 QQ 已经绑定账号了")
    conn = _connect()
    try:
        add_entry(
            conn,
            payload.qq_uin,
            note=payload.note,
            added_by=str(admin_user_id),
        )
        _set_role(conn, payload.qq_uin, payload.role)
    finally:
        conn.close()


def _update(qq_uin: str, payload: QQWhitelistUpdate) -> None:
    conn = _connect()
    try:
        if entry_for(conn, qq_uin) is None:
            raise HTTPException(status_code=404, detail="QQ 不在白名单里")
        conn.execute(
            "UPDATE qq_whitelist SET note = COALESCE(?, note) WHERE qq_uin = ?",
            ((payload.note or "").strip() or None, qq_uin),
        )
        _set_role(conn, qq_uin, payload.role)
    finally:
        conn.close()


def _remove(qq_uin: str) -> None:
    conn = _connect()
    try:
        remove_entry(conn, qq_uin)
        conn.commit()
    finally:
        conn.close()


def _purge(qq_uin: str) -> None:
    conn = _connect()
    try:
        merge.delete_person_data(conn, identity.person_key_qq(qq_uin))
        remove_entry(conn, qq_uin)
        conn.commit()
    finally:
        conn.close()


async def _guarded(fn, *args, detail: str):
    """统一把“库不可用”翻译成 503；HTTPException 原样抛出。"""
    try:
        return await asyncio.to_thread(fn, *args)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("QQ 白名单操作失败 fn=%s", getattr(fn, "__name__", fn))
        raise HTTPException(status_code=503, detail=detail) from None


@router.get("/admin/qq-whitelist")
async def list_qq_whitelist(
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """列出白名单，附上当前角色、上限和（异常情况下的）绑定账号。"""
    return await _guarded(_read, detail="白名单暂时读取不到")


@router.post("/admin/qq-whitelist")
async def add_qq_whitelist(
    payload: QQWhitelistAdd,
    admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """加入白名单；已绑定的 QQ 返回 409。"""
    await _guarded(_add, payload, admin.id, detail="白名单暂不可用")
    return await _guarded(_one, payload.qq_uin, detail="白名单暂时读取不到")


@router.put("/admin/qq-whitelist/{qq_uin}")
async def update_qq_whitelist(
    qq_uin: str,
    payload: QQWhitelistUpdate,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    await _guarded(_update, qq_uin, payload, detail="白名单暂不可用")
    return await _guarded(_one, qq_uin, detail="白名单暂时读取不到")


@router.delete("/admin/qq-whitelist/{qq_uin}", status_code=204)
async def remove_qq_whitelist(
    qq_uin: str,
    _admin: AuthUser = Depends(require_admin),
) -> None:
    """移出名单：记忆保留但不再读取。"""
    await _guarded(_remove, qq_uin, detail="白名单暂不可用")


@router.delete("/admin/qq-whitelist/{qq_uin}/data", status_code=204)
async def purge_qq_whitelist(
    qq_uin: str,
    _admin: AuthUser = Depends(require_admin),
) -> None:
    """彻底删除这个 QQ 名下的全部数据，同时移出名单。"""
    await _guarded(_purge, qq_uin, detail="删除失败，数据没有被清理")
