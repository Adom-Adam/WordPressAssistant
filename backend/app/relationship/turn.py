"""一轮对话对关系状态的影响。

把“谁是这个人、这一轮该记成什么”从 QQ 适配器里拆出来：

- `relationship_subject` 把 QQ 号和 WP 绑定映射成同一个 person_key，
  并在两个身份之间取更高的角色；
- `load_signals` 读取并衰减状态，给决策层提供 affinity / valence / energy；
- `record_reply` / `record_silence` 在决策之后写回状态和事件。

适配器只负责捕获异常和 fail-open，不关心这些细节。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from app.memory import identity
from app.persona.models import Dynamics
from app.relationship.conflict import advance_by_time
from app.relationship.state import (
    DEFAULT_TIMEZONE,
    ROLE_NAMES,
    RelationshipState,
    affinity_for,
    apply_event,
    clamp,
    decay_state,
    get_or_create_state,
    now_iso,
    role_for,
    save_state,
)

# 角色高低顺序；已绑定账号取两者中更高的一项。
_ROLE_ORDER = ("normal", "special", "exception", "owner")


@dataclass(frozen=True, slots=True)
class TurnSignals:
    """决策层需要的状态快照。"""

    state: RelationshipState
    affinity: float


def _higher_role(first: str, second: str) -> str:
    if first not in ROLE_NAMES:
        first = "normal"
    if second not in ROLE_NAMES:
        second = "normal"
    return first if _ROLE_ORDER.index(first) >= _ROLE_ORDER.index(second) else second


def relationship_subject(
    conn: sqlite3.Connection,
    qq_uin: int | str,
    bound_user_id: int | None,
) -> tuple[str, str]:
    """返回 `(person_key, role)`。

    已绑定的 QQ 用 `wp:<id>` 作为关系主体，和网页共享同一份关系；
    未绑定时用 `qq:<uin>`。角色在 wp 与 qq 两个 key 之间取更高者，
    避免管理员先在后台按 QQ 号加了名单、绑定后却降级。
    """
    person_key = identity.person_key_qq(qq_uin, bound_user_id)
    role = role_for(conn, person_key)
    if bound_user_id:
        role = _higher_role(role, role_for(conn, f"qq:{str(qq_uin).strip()}"))
    return person_key, role


def load_signals(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> TurnSignals:
    """读取状态、按流逝时间衰减并写回，返回决策所需的信号。"""
    stamp = now or now_iso()
    state = get_or_create_state(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        now=stamp,
        timezone_name=timezone_name,
    )
    previous_updated_at = state.updated_at
    state = decay_state(
        state, dynamics, now=stamp, timezone_name=timezone_name
    )
    # 时间型转移是惰性的：趁每次读状态顺手结算，不新增定时任务。
    # 用衰减后的 patience/security 判定，再写回。
    state = advance_by_time(
        conn,
        state,
        dynamics=dynamics,
        now=stamp,
        previous_updated_at=previous_updated_at,
    )
    save_state(conn, state)
    return TurnSignals(state=state, affinity=affinity_for(state.affection, dynamics))


def record_reply(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    baseline: bool = True,
) -> RelationshipState:
    """一轮正常回复：涨一点好感、心情略好、精力下降、刷新 last_reply_at。

    `baseline=False` 用于本轮已经由模型评估落过关系账的情况：只保留
    疲劳和“回复过”的时间戳，不再叠加通用聊天加成。
    """
    return apply_event(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        kind="chat",
        d_affection_units=(
            dynamics.relationship.chat_affection_units if baseline else 0.0
        ),
        d_valence=dynamics.relationship.chat_valence if baseline else 0.0,
        d_energy=-dynamics.energy.fatigue_per_turn,
        touch_reply=True,
        note="normal reply" if baseline else "reply after appraisal",
        now=now,
        timezone_name=timezone_name,
    )


def record_attention(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    confidence: float,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> RelationshipState:
    """记录“这个人此刻正在跟我说话”。

    和好感、心情不同，注意力不累加：它回答的是“现在是不是在聊”，
    答案只有是/否，之后按时间自然衰减。主动消息靠它跳过正在聊的人。

    先衰减再写入：如果上次的注意力已经衰减到 0.2，这次直接置为 1.0，
    而不是在旧值上叠加。
    """
    stamp = now or now_iso()
    state = get_or_create_state(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        now=stamp,
        timezone_name=timezone_name,
    )
    state = decay_state(state, dynamics, now=stamp, timezone_name=timezone_name)
    state = replace(
        state,
        attention_confidence=clamp(confidence, 0.0, 1.0),
        updated_at=stamp,
    )
    save_state(conn, state)
    return state


def record_silence(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> RelationshipState:
    """选择不回复：只积累回复债，不涨好感、不刷新 last_reply_at。"""
    return apply_event(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        kind="ignored",
        d_debt=dynamics.reply.debt_increment,
        note="silent decision",
        now=now,
        timezone_name=timezone_name,
    )
