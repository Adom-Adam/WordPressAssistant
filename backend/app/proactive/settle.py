"""主动消息的“被忽略”结算。

发出去之后要有个结论。这里的关键不是判断，而是**不确定时不扣分**：
对方可能在忙、可能没看到、可能只是懒得回这一条。用三重证据折算置信度：

- 24 小时后这个人一句话都没说 → 0（就当没看见）；
- 他在别的频道活跃，只是没回这条 → 0.2；
- 他回到这条消息所在的私聊里说过话 → 已算回应，不扣分。

只有置信度大于 0 才动关系，而且扣分要乘置信度。宁可漏扣，也不要
因为一次沉默把关系判死。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from app.persona.models import Dynamics
from app.relationship.state import (
    DEFAULT_TIMEZONE,
    apply_event,
    get_state,
    now_iso,
    parse_iso,
    role_for,
)

# 只在私聊里结算：群聊没有“回没回”这回事，大家在聊别的也正常。
_PRIVATE_PREFIX = "private:"
# 在别处活跃、但没回这条时给的置信度。
_ELSEWHERE_CONFIDENCE = 0.2


def derived_attention(
    conn: sqlite3.Connection,
    person_key: str,
    channel_id: str,
    sent_at: str,
) -> float:
    """从“发出之后这个人有没有说话”推一个注意力置信度。"""
    rows = conn.execute(
        """SELECT channel_id FROM events
            WHERE person_key = ? AND source = 'user_message'
              AND occurred_at > ?""",
        (person_key, sent_at),
    ).fetchall()
    if not rows:
        return 0.0
    if any(row["channel_id"] == channel_id for row in rows):
        return 1.0
    return _ELSEWHERE_CONFIDENCE


def settle_ignores(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    dynamics: Dynamics,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, int]:
    """结算已经过了宽限期、还没有回应的私聊主动消息。

    返回 `{"answered": n, "ignored": n}`，方便调度器写日志。
    只处理 `status='sent'` 的行：结算过的不会被再算一次。
    """
    stamp = now or now_iso()
    grace_hours = dynamics.proactive.ignore_grace_hours
    cutoff = (parse_iso(stamp) - timedelta(hours=grace_hours)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        """SELECT id, target_key, channel_id, created_at FROM proactive_log
            WHERE persona_key = ? AND status = 'sent'
              AND channel_id LIKE ? AND created_at < ?
            ORDER BY id""",
        (persona_key, f"{_PRIVATE_PREFIX}%", cutoff),
    ).fetchall()

    answered = 0
    ignored = 0
    try:
        for row in rows:
            person_key = row["target_key"]
            derived = derived_attention(
                conn, person_key, row["channel_id"], row["created_at"]
            )
            if derived >= 1.0:
                _mark(conn, row["id"], "answered")
                answered += 1
                continue

            _mark(conn, row["id"], "ignored")
            ignored += 1

            # 群聊身份（qq:xxx）没有关系状态可扣，标完就结束。
            if not person_key.startswith("wp:"):
                continue

            state = get_state(conn, persona_key, person_key)
            if state is None:
                continue
            confidence = max(state.attention_confidence, derived)
            if confidence <= 0.0:
                continue

            cfg = dynamics.proactive
            apply_event(
                conn,
                persona_key,
                person_key,
                role=role_for(conn, person_key),
                dynamics=dynamics,
                kind="proactive_ignored",
                d_affection_points=-cfg.ignore_affection_points * confidence,
                d_security=-cfg.ignore_security_points * confidence,
                confidence=confidence,
                note="proactive ignored",
                touch_interaction=False,
                now=stamp,
                timezone_name=timezone_name,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"answered": answered, "ignored": ignored}


def _mark(conn: sqlite3.Connection, row_id: int, status: str) -> None:
    conn.execute(
        "UPDATE proactive_log SET status = ? WHERE id = ?",
        (status, int(row_id)),
    )
