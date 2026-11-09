"""群聊的三个开关：怎么算被叫到、回不回、要不要主动说话。

三件事互相独立，所以存一行而不是塞进全局设置：

- `trigger`：`mention`（@ 或前缀）、`keyword`（仅前缀）、`all`（每条都算）。
- `reply_policy`：`always` 走正常回复，`silent` 只记录不说话。
- `proactive`：这个群是否允许梅林主动开口。

缺行、行损坏、值不认识时一律回退到调用方给的默认策略（由旧
`QQ_TRIGGER` 推导），宁可维持现状也不要让群突然变哑或刷屏。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.relationship.state import now_iso

TRIGGERS = ("mention", "keyword", "all")
REPLY_POLICIES = ("always", "silent")

# 旧的全局 QQ_TRIGGER 取值。`at` 现在等同于“@ 或前缀”。
_LEGACY = {"at": "mention", "prefix": "keyword", "all": "all"}

DEFAULT_PREFIX = "梅林"


@dataclass(frozen=True, slots=True)
class GroupPolicy:
    trigger: str
    reply_policy: str
    proactive: bool


@dataclass(frozen=True, slots=True)
class TriggerMatch:
    """触发匹配结果。

    `content` 是剥掉前缀之后的正文，空串表示没被触发；
    `addressed` 表示这条消息是在点名梅林（@ 或前缀），而不是
    `trigger=all` 顺带收进来的普通闲聊。
    """

    content: str
    addressed: bool


def default_policy(trigger_setting: str) -> GroupPolicy:
    """从旧的全局 `QQ_TRIGGER` 推导默认策略。"""
    trigger = _LEGACY.get((trigger_setting or "").strip().lower(), "mention")
    return GroupPolicy(trigger=trigger, reply_policy="always", proactive=False)


def _validate(policy: GroupPolicy) -> None:
    if policy.trigger not in TRIGGERS:
        raise ValueError(f"未知的群触发方式：{policy.trigger!r}")
    if policy.reply_policy not in REPLY_POLICIES:
        raise ValueError(f"未知的群回复方式：{policy.reply_policy!r}")


def load_policy(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    fallback: GroupPolicy,
) -> GroupPolicy:
    """读一个群的策略；缺行或行损坏时返回 `fallback`。"""
    row = conn.execute(
        "SELECT trigger, reply_policy, proactive FROM qq_group_policies "
        "WHERE group_id = ?",
        (int(group_id),),
    ).fetchone()
    if row is None:
        return fallback
    policy = GroupPolicy(
        trigger=str(row["trigger"]),
        reply_policy=str(row["reply_policy"]),
        proactive=bool(row["proactive"]),
    )
    try:
        _validate(policy)
    except ValueError:
        return fallback
    return policy


def save_policy(
    conn: sqlite3.Connection,
    group_id: int,
    policy: GroupPolicy,
    *,
    now: str | None = None,
) -> GroupPolicy:
    """写入一个群的策略。值不合法时抛 `ValueError`。"""
    _validate(policy)
    stamp = now or now_iso()
    conn.execute(
        """
        INSERT INTO qq_group_policies (group_id, trigger, reply_policy, proactive, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            trigger = excluded.trigger,
            reply_policy = excluded.reply_policy,
            proactive = excluded.proactive,
            updated_at = excluded.updated_at
        """,
        (
            int(group_id),
            policy.trigger,
            policy.reply_policy,
            1 if policy.proactive else 0,
            stamp,
        ),
    )
    conn.commit()
    return policy


def match_trigger(
    text: str,
    at_self: bool,
    *,
    trigger: str,
    prefix: str = DEFAULT_PREFIX,
) -> TriggerMatch:
    """判断一条群消息是否触发了梅林，并给出剥掉前缀后的正文。

    前缀优先于一切：以 `prefix` 开头的消息一定算点名，不管 `trigger`
    怎么配。之后按 `trigger` 分流——`all` 放行但不算点名，`mention`
    还要 @ 到梅林，`keyword` 只认前缀。
    """
    kind = _LEGACY.get(trigger, trigger)
    cleaned = (prefix or "").strip() or DEFAULT_PREFIX
    if text.startswith(cleaned):
        return TriggerMatch(text[len(cleaned):].strip(), True)
    if kind == "all":
        return TriggerMatch(text, False)
    if kind == "mention" and at_self:
        return TriggerMatch(text, True)
    return TriggerMatch("", False)
