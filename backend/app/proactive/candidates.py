"""谁会让她想起，以及她想起的时候在想什么。

主动消息最容易做坏的地方是“无缘无故想起来”——一条没有由头的消息，
比不发更像骚扰。所以这里拆成两步：

1. `score_target` 给每个认识的人打一个基础分：关系越近、越久没联系、
   最近聊得越好、越有没说完的话，分数越高；
2. `generate_thoughts` 把这个分数变成几条具体的念头，每条念头带着
   自己的由头（跟进那件事、想念、担心、想分享、只是想陪一会儿）。

念头分数达不到阈值就不发。允许“想起来了但没什么好说”，也是分寸。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from app.persona.models import Dynamics
from app.proactive.pending import open_topic
from app.relationship.conflict import stage_multiplier
from app.relationship.state import (
    DEFAULT_TIMEZONE,
    RelationshipState,
    affinity_for,
    clamp,
    decay_state,
    now_iso,
    parse_iso,
    state_from_row,
)

# 念头的优先级：同分时按这个顺序取，保证结果稳定、可解释。
KIND_ORDER = ("event_followup", "missing", "care", "spontaneous", "companionship")

WEIGHTS = {
    "closeness": 0.25,
    "missing": 0.20,
    "gap": 0.15,
    "quality": 0.10,
    "unfinished": 0.15,
    "energy": 0.05,
    "attention": 0.05,
    "repetition": -0.05,
}

SCORE_CEILING = 0.95
# 各种念头的由头加成。
_THOUGHT_BONUS = {
    "event_followup": 0.10,
    "missing": 0.05,
    "care": 0.05,
    "spontaneous": 0.05,
    "companionship": 0.0,
}
# 同一种念头连着出现过，就往下压一点：总是“想你了”会很假。
_REPEAT_PENALTY = 0.03
_REPEAT_MAX = 0.09
_MAX_THOUGHTS = 4
# 多久没聊过天算“满分空档”。
_GAP_FULL_HOURS = 72.0
# 只看最近这么多天还有动静的人，避免把整个历史用户表扫一遍。
CANDIDATE_WINDOW_DAYS = 30


@dataclass(frozen=True, slots=True)
class Thought:
    """一条具体的念头，`kind` 用于去重，`score` 用于和阈值比较。"""

    kind: str
    score: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个可以主动联系的人，连同他的状态快照。

    带上 `state` 是为了让调度器做发送前检查（安静时段、最近是否活跃）
    时不用再查一次库。
    """

    persona_key: str
    person_key: str
    qq_uin: str
    # `wp:` 身份是博客账号；白名单里的 `qq:` 身份没有账号，这里就是 None。
    wp_user_id: int | None
    state: RelationshipState
    base_score: float
    thoughts: tuple[Thought, ...]


def _clamp01(value: float) -> float:
    return clamp(value, 0.0, 1.0)


def score_target(
    *,
    affinity: float,
    missing: float,
    gap_hours: float,
    quality: float,
    unfinished: float,
    energy: float,
    attention: float,
    repetition: float,
) -> float:
    """基础分：只看“关系本身”有没有到该想起的程度。"""
    score = (
        WEIGHTS["closeness"] * _clamp01(affinity / 1.2)
        + WEIGHTS["missing"] * _clamp01(missing / 100.0)
        + WEIGHTS["gap"] * _clamp01(gap_hours / _GAP_FULL_HOURS)
        + WEIGHTS["quality"] * _clamp01(quality)
        + WEIGHTS["unfinished"] * _clamp01(unfinished)
        + WEIGHTS["energy"] * _clamp01(energy)
        + WEIGHTS["attention"] * _clamp01(attention)
        + WEIGHTS["repetition"] * _clamp01(repetition)
    )
    return clamp(score, 0.0, SCORE_CEILING)


def recent_quality(
    conn: sqlite3.Connection, persona_key: str, person_key: str
) -> float:
    """最近三次成功评估的情绪走向：正向 1.0、中性 0.5、负向 0.0。

    没有记录时给 0.5（不知道就当一般），避免冷启动把所有人压到最低。
    """
    rows = conn.execute(
        """SELECT d_affection FROM appraisal_log
            WHERE persona_key = ? AND person_key = ? AND status = 'ok'
            ORDER BY id DESC LIMIT 3""",
        (persona_key, person_key),
    ).fetchall()
    if not rows:
        return 0.5

    values = []
    for row in rows:
        delta = float(row["d_affection"])
        values.append(1.0 if delta > 0 else 0.0 if delta < 0 else 0.5)
    return sum(values) / len(values)


def generate_thoughts(
    base: float,
    *,
    has_pending: bool,
    reply_debt: float,
    missing: float,
    valence: float,
    recent_kinds: tuple[str, ...] = (),
) -> tuple[Thought, ...]:
    """把基础分变成具体念头，按分数从高到低、同分按 `KIND_ORDER` 排。"""
    eligible: list[str] = []
    if has_pending or reply_debt >= 0.1:
        eligible.append("event_followup")
    if missing >= 50:
        eligible.append("missing")
    if valence <= -0.2:
        eligible.append("care")
    if valence >= 0.2:
        eligible.append("spontaneous")
    eligible.append("companionship")

    thoughts = []
    for kind in eligible:
        penalty = min(_REPEAT_PENALTY * recent_kinds.count(kind), _REPEAT_MAX)
        score = clamp(base + _THOUGHT_BONUS[kind] - penalty, 0.0, 1.0)
        thoughts.append(Thought(kind=kind, score=score))

    thoughts.sort(key=lambda item: (-item.score, KIND_ORDER.index(item.kind)))
    return tuple(thoughts[:_MAX_THOUGHTS])


def pick_thought(
    thoughts: tuple[Thought, ...], speak_threshold: float
) -> Thought | None:
    """第一条达到阈值的念头；都不到就这轮不发。"""
    for thought in thoughts:
        if thought.score >= speak_threshold:
            return thought
    return None


def _gap_hours(state: RelationshipState, stamp: str) -> float:
    if not state.last_interaction_at:
        return _GAP_FULL_HOURS
    try:
        elapsed = (parse_iso(stamp) - parse_iso(state.last_interaction_at)).total_seconds()
    except (TypeError, ValueError):
        return _GAP_FULL_HOURS
    return max(0.0, elapsed / 3600.0)


def _recent_kinds(
    conn: sqlite3.Connection, persona_key: str, person_key: str, limit: int = 3
) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT kind FROM proactive_log
            WHERE persona_key = ? AND target_key = ?
            ORDER BY id DESC LIMIT ?""",
        (persona_key, person_key, limit),
    ).fetchall()
    return tuple(row["kind"] for row in rows)


def candidates(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    dynamics: Dynamics,
    think_threshold: float,
    speak_threshold: float,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 200,
) -> list[Candidate]:
    """列出这一轮该想的人，按基础分从高到低。

    `speak_threshold` 目前只用于日志/调试；念头是否够高由调度器用
    `pick_thought` 判断，这里只负责把候选和念头准备好。
    """
    stamp = now or now_iso()
    since = _window_start(stamp)
    rows = conn.execute(
        """SELECT rs.*, p.wp_user_id, p.qq_uin
             FROM relationship_state rs
             JOIN persons p ON p.person_key = rs.person_key
            WHERE rs.persona_key = ? AND rs.updated_at >= ?
              AND (
                (p.wp_user_id IS NOT NULL AND rs.person_key LIKE 'wp:%')
                OR (
                  rs.person_key LIKE 'qq:%'
                  AND rs.last_reply_at IS NOT NULL
                  AND p.qq_uin IN (SELECT qq_uin FROM qq_whitelist)
                )
              )
            ORDER BY rs.updated_at DESC
            LIMIT ?""",
        (persona_key, since, limit),
    ).fetchall()

    result: list[Candidate] = []
    for row in rows:
        person_key = row["person_key"]
        qq_uin = str(row["qq_uin"] or "").strip()
        # 没有 QQ 号就发不出去。
        if not qq_uin:
            continue
        # `wp:` 是博客账号；`qq:` 只可能是白名单里的号（SQL 已经筛过）。
        if person_key.startswith("wp:"):
            wp_user_id: int | None = int(row["wp_user_id"])
        elif person_key.startswith("qq:"):
            wp_user_id = None
        else:
            continue

        state = decay_state(
            state_from_row(row), dynamics, now=stamp, timezone_name=timezone_name
        )
        has_pending = (
            open_topic(conn, persona_key, person_key, now=stamp) is not None
        )
        unfinished = clamp(
            state.reply_debt / 0.5 + (0.5 if has_pending else 0.0), 0.0, 1.0
        )
        base = score_target(
            affinity=affinity_for(state.affection, dynamics),
            missing=state.missing,
            gap_hours=_gap_hours(state, stamp),
            quality=recent_quality(conn, persona_key, person_key),
            unfinished=unfinished,
            energy=state.energy,
            attention=state.attention_confidence,
            repetition=_repeat_ratio(
                _recent_kinds(conn, persona_key, person_key)
            ),
        )
        base = clamp(
            base * stage_multiplier(state.conflict_stage, dynamics),
            0.0,
            SCORE_CEILING,
        )

        thoughts: tuple[Thought, ...] = ()
        if base >= think_threshold:
            thoughts = generate_thoughts(
                base,
                has_pending=has_pending,
                reply_debt=state.reply_debt,
                missing=state.missing,
                valence=state.valence,
                recent_kinds=_recent_kinds(conn, persona_key, person_key),
            )

        result.append(
            Candidate(
                persona_key=persona_key,
                person_key=person_key,
                qq_uin=qq_uin,
                wp_user_id=wp_user_id,
                state=state,
                base_score=base,
                thoughts=thoughts,
            )
        )

    result.sort(key=lambda item: item.base_score, reverse=True)
    return result


def _window_start(stamp: str) -> str:
    """候选人窗口的起点。时间戳坏掉时退回“全都算”。"""
    try:
        return (parse_iso(stamp) - timedelta(days=CANDIDATE_WINDOW_DAYS)).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        return ""


def _repeat_ratio(kinds: tuple[str, ...]) -> float:
    """最近三条里同一种念头占了多大比例，喂给 `score_target` 的重复惩罚。"""
    if not kinds:
        return 0.0
    most = max(kinds.count(kind) for kind in set(kinds))
    return most / len(kinds)
