"""管理员侧的 QQ 群策略接口。

只能操作 `QQ_ALLOWED_GROUPS` 白名单里的群——策略表本身不设边界，
但后台不该成为“给任意群配策略”的入口，白名单才是唯一来源。

没写过的群返回由 `QQ_TRIGGER` 推导的默认策略（`source='default'`），
写过之后变成 `source='db'`。这样后台第一次打开就能看到当前真实生效
的行为，而不是一片空白。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.auth import AuthUser
from app.channels.qq import group_allowlist
from app.channels.qq.group_policy import (
    GroupPolicy,
    default_policy,
    load_policy,
    save_policy,
)
from app.config import get_settings
from app.memory import db as memory_db
from app.schemas import QQGroupAdd, QQGroupPolicyUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def _serialize(
    group_id: int,
    policy: GroupPolicy,
    *,
    source: str,
) -> dict[str, object]:
    return {
        "group_id": group_id,
        "trigger": policy.trigger,
        "reply_policy": policy.reply_policy,
        "proactive": policy.proactive,
        "source": source,
    }


def _read_all() -> list[dict[str, object]]:
    settings = get_settings()
    fallback = default_policy(settings.qq_trigger)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        stored = {
            int(row["group_id"])
            for row in conn.execute("SELECT group_id FROM qq_group_policies")
        }
        return [
            _serialize(
                group_id,
                load_policy(conn, group_id, fallback=fallback),
                source="db" if group_id in stored else "default",
            )
            for group_id in sorted(group_allowlist.allowed_group_ids(conn))
        ]
    finally:
        conn.close()


def _write(group_id: int, policy: GroupPolicy) -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        # 名单以运行时表为准：后台刚加的群不用重启就能配策略。
        if group_id not in group_allowlist.allowed_group_ids(conn):
            raise HTTPException(status_code=422, detail="群不在白名单里")
        save_policy(conn, group_id, policy)
    finally:
        conn.close()


def _one(group_id: int) -> dict[str, object]:
    """读一个群当前生效的策略，带上它是不是后台写过的。"""
    fallback = default_policy(get_settings().qq_trigger)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        row = conn.execute(
            "SELECT 1 FROM qq_group_policies WHERE group_id = ?", (group_id,)
        ).fetchone()
        return _serialize(
            group_id,
            load_policy(conn, group_id, fallback=fallback),
            source="db" if row is not None else "default",
        )
    finally:
        conn.close()


def _add(group_id: int) -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        group_allowlist.add_group(conn, group_id)
    finally:
        conn.close()


def _remove(group_id: int) -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        group_allowlist.remove_group(conn, group_id)
    finally:
        conn.close()


@router.get("/admin/qq-groups")
async def list_group_policies(
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """列出白名单内每个群当前生效的策略。"""
    try:
        groups = await asyncio.to_thread(_read_all)
    except Exception:  # noqa: BLE001 - 记忆库故障时给后台页明确错误
        logger.exception("读取群策略失败")
        raise HTTPException(status_code=503, detail="策略库暂时读取不到") from None
    return {"groups": groups}


@router.put("/admin/qq-groups/{group_id}")
async def update_group_policy(
    group_id: int,
    payload: QQGroupPolicyUpdate,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """覆盖一个群的策略。群不在白名单里直接拒绝。"""
    policy = GroupPolicy(
        trigger=payload.trigger,
        reply_policy=payload.reply_policy,
        proactive=payload.proactive,
    )
    try:
        await asyncio.to_thread(_write, group_id, policy)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 写不进去就别假装成功
        logger.exception("写入群策略失败 group=%s", group_id)
        raise HTTPException(status_code=503, detail="策略库暂不可用") from None

    return _serialize(group_id, policy, source="db")


@router.post("/admin/qq-groups")
async def add_group(
    payload: QQGroupAdd,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """把一个群加进白名单；已经在名单里也算成功（幂等）。"""
    try:
        await asyncio.to_thread(_add, payload.group_id)
        return await asyncio.to_thread(_one, payload.group_id)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("添加群失败 group=%s", payload.group_id)
        raise HTTPException(status_code=503, detail="策略库暂不可用") from None


@router.delete("/admin/qq-groups/{group_id}", status_code=204)
async def remove_group(
    group_id: int,
    _admin: AuthUser = Depends(require_admin),
) -> None:
    """移出白名单。这个群的策略行保留，重新加回来时配置还在。"""
    try:
        await asyncio.to_thread(_remove, group_id)
    except Exception:  # noqa: BLE001
        logger.exception("移除群失败 group=%s", group_id)
        raise HTTPException(status_code=503, detail="策略库暂不可用") from None
