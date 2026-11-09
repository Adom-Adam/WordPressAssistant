"""群聊里被指名时的那一次判断。

和私聊的区别都收在这个文件里，适配器只管调用：

- 只读关系状态，不写：群里说一句的分量和单独找人说话不一样，
  混在一起会把好感刷得很快；
- 上下文只取**这个人**在这个群里说过的话，绝不把私聊内容带进群提示词；
- 概率用私聊的 p0 公式，但抬到 `dynamics.group.reply_floor`：
  群里有别人看着，点名不回很难解释，只有极端状态才真的不回。
"""

from __future__ import annotations

import random
import sqlite3

from app.persona.models import Dynamics
from app.relationship.decision import Decision, decide
from app.relationship.state import DEFAULT_TIMEZONE
from app.relationship.turn import (
    TurnSignals,
    load_signals,
    relationship_subject,
)


def load_group_signals(
    conn: sqlite3.Connection,
    persona_key: str,
    qq_uin: int | str,
    bound_user_id: int | None,
    *,
    dynamics: Dynamics,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> tuple[str, str, TurnSignals]:
    """返回 `(person_key, role, signals)`，和私聊共用同一套身份映射。"""
    person_key, role = relationship_subject(conn, qq_uin, bound_user_id)
    signals = load_signals(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        now=now,
        timezone_name=timezone_name,
    )
    return person_key, role, signals


def recent_speaker_texts(
    conn: sqlite3.Connection,
    person_key: str,
    channel_id: str,
    limit: int = 5,
) -> list[str]:
    """这个人在这个频道里最近说过的话，新→旧。

    `channel_id` 精确匹配是这里唯一的安全边界：群聊回合查的是
    `group:<群号>`，私聊事件存在 `private:<QQ 号>`，两者永不交叉。
    """
    rows = conn.execute(
        """SELECT content FROM events
            WHERE person_key = ? AND channel_id = ? AND source = 'user_message'
            ORDER BY id DESC
            LIMIT ?""",
        (person_key, channel_id, limit),
    ).fetchall()
    return [row["content"] for row in rows]


def decide_group(
    *,
    text: str,
    recent: list[str],
    signals: TurnSignals,
    dynamics: Dynamics,
    rng: random.Random,
    name: str = "",
) -> Decision:
    """被指名的群消息：按关系状态掷骰子，但概率不低于群下限。"""
    return decide(
        text=text,
        recent=recent,
        affinity=signals.affinity,
        valence=signals.state.valence,
        energy=signals.state.energy,
        reply_debt=signals.state.reply_debt,
        dynamics=dynamics,
        rng=rng,
        name=name,
        patience=signals.state.patience,
        security=signals.state.security,
        stage=signals.state.conflict_stage,
        reason="group_addressed",
        probability_floor=dynamics.group.reply_floor,
    )
