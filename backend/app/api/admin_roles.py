"""后台关系角色名单。

角色回答的是“这个人对我有多重要”，不是“能不能聊天”：owner/exception/
special/normal 只改好感上限和成长速度，改完也不会把已经攒下的好感收回去。

三个约定：
- 管理员自己永远是 owner。站长进后台看到自己上限 60，会以为参数没生效。
- `cap` 永远从当前人设的 `dynamics.relationship.roles` 读，不在代码里写死，
  否则后台显示的上限和实际判定用的上限会慢慢分叉。
- 没有档案的 `wp_user_id` 也能直接设角色：先建一行再改，不用等对方先来聊天。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.auth.jwt import AuthUser
from app.memory import db as memory_db
from app.memory import identity
from app.persona.store import get_persona_store
from app.relationship.state import ROLE_NAMES, role_config
from app.schemas import RoleUpdate

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLE_RANK = {"normal": 0, "special": 1, "exception": 2, "owner": 3}


def _cap(role: str, dynamics: object) -> int | float:
    """角色上限。人设里没配这个角色时 `role_config` 会回退 normal。"""
    cap = role_config(role, dynamics).cap
    return int(cap) if float(cap).is_integer() else float(cap)


def _initial(role: str, dynamics: object) -> int | float:
    """这个角色的起始好感。没有关系档案时，后台显示的就是它。"""
    value = role_config(role, dynamics).initial
    return int(value) if float(value).is_integer() else float(value)


def _serialize(
    row, dynamics: object, *, affection: float | None
) -> dict[str, object]:
    role = str(row["role"] or "normal")
    if role not in ROLE_NAMES:  # 脏数据按 normal 展示，和实际行为一致
        role = "normal"
    return {
        "wp_user_id": int(row["wp_user_id"]),
        "person_key": str(row["person_key"]),
        "display_name": str(row["display_name"] or ""),
        "role": role,
        "cap": _cap(role, dynamics),
        # None 表示“还没有关系状态”，页面上显示角色的初始好感，
        # 不能显示 0——那看起来像好感被清空了。
        "affection": None if affection is None else float(affection),
        "affection_initial": _initial(role, dynamics),
    }


def _promote_admin(conn, admin_user_id: int) -> None:
    """保证调用者自己被标成 owner。

    放在每次读取时做，而不是只在第一次：数据库被手工改过、或者管理员
    换了账号，下一次打开后台就自动纠正。
    """
    admin_key = identity.person_key_web(admin_user_id)
    identity.touch_person(conn, admin_key, wp_user_id=admin_user_id)
    conn.execute(
        "UPDATE persons SET role = 'owner' WHERE person_key = ?", (admin_key,)
    )
    conn.commit()


def _read(query: str, admin_user_id: int) -> dict[str, object]:
    persona = get_persona_store().load_active()
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        _promote_admin(conn, admin_user_id)

        sql = """
            SELECT p.person_key, p.display_name, p.wp_user_id, p.role,
                   p.last_seen_at, rs.affection
              FROM persons p
              LEFT JOIN relationship_state rs
                     ON rs.persona_key = ? AND rs.person_key = p.person_key
             WHERE p.wp_user_id IS NOT NULL
        """
        params: list[object] = [persona.state_key]
        text = (query or "").strip()
        if text:
            # 昵称模糊、ID 精确：站长一般记得其中一种。
            sql += " AND (p.display_name LIKE ? OR CAST(p.wp_user_id AS TEXT) = ?)"
            params.extend([f"%{text}%", text])
        sql += " ORDER BY p.last_seen_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    dynamics = persona.dynamics
    roles = [
        _serialize(row, dynamics, affection=row["affection"]) for row in rows
    ]
    # SQL 已经按最近活跃排好；这里再按角色稳定排序，就是“重要的在前，
    # 同角色里最近来过的在前”。
    roles.sort(key=lambda item: _ROLE_RANK.get(str(item["role"]), 0), reverse=True)
    return {
        "roles": roles,
        "role_caps": {name: _cap(name, dynamics) for name in ROLE_NAMES},
        "role_initials": {name: _initial(name, dynamics) for name in ROLE_NAMES},
    }


def _write(wp_user_id: int, role: str) -> dict[str, object]:
    persona = get_persona_store().load_active()
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        person_key = identity.person_key_web(wp_user_id)
        identity.touch_person(conn, person_key, wp_user_id=wp_user_id)
        conn.execute(
            "UPDATE persons SET role = ? WHERE person_key = ?", (role, person_key)
        )
        conn.commit()
        row = conn.execute(
            """SELECT p.person_key, p.display_name, p.wp_user_id, p.role,
                      rs.affection
                 FROM persons p
                 LEFT JOIN relationship_state rs
                        ON rs.persona_key = ? AND rs.person_key = p.person_key
                WHERE p.person_key = ?""",
            (persona.state_key, person_key),
        ).fetchone()
    finally:
        conn.close()

    return _serialize(row, persona.dynamics, affection=row["affection"])


def _reset(wp_user_id: int) -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            "UPDATE persons SET role = 'normal' WHERE person_key = ?",
            (identity.person_key_web(wp_user_id),),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("/admin/roles")
async def list_roles(
    query: str = "",
    admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """列出所有有 WordPress 账号关系的人，并附上各角色上限。"""
    try:
        return await asyncio.to_thread(_read, query, admin.id)
    except Exception:  # noqa: BLE001 - 记忆库故障时给后台页明确错误
        logger.exception("读取角色名单失败")
        raise HTTPException(status_code=503, detail="角色名单暂时读取不到") from None


@router.put("/admin/roles")
async def set_role(
    payload: RoleUpdate,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """改一个人的角色。只影响之后的好感增长。"""
    try:
        return await asyncio.to_thread(_write, payload.wp_user_id, payload.role)
    except Exception:  # noqa: BLE001 - 写不进去就别假装成功
        logger.exception("写入角色失败 user=%s", payload.wp_user_id)
        raise HTTPException(status_code=503, detail="角色库暂不可用") from None


@router.delete("/admin/roles/{wp_user_id}", status_code=204)
async def reset_role(
    wp_user_id: int,
    _admin: AuthUser = Depends(require_admin),
) -> None:
    """恢复成 normal。没有档案也算成功：后台要的结果已经达成。"""
    try:
        await asyncio.to_thread(_reset, wp_user_id)
    except Exception:  # noqa: BLE001
        logger.exception("重置角色失败 user=%s", wp_user_id)
        raise HTTPException(status_code=503, detail="角色库暂不可用") from None
