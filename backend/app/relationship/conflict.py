"""受伤—修复弧：把“这一轮发生了什么”变成阶段和伤疤。

设计要点：

- 转移是确定性的，不用随机数；同样的事件和状态永远得到同样的下一步。
- 时间只做三件事：最短停留防跳变、无修复时恶化、疤痕淡化。
  时间本身不会把受伤直接变成“没事了”。
- 所有时间型转移都是惰性的：下一次读状态时才结算，不新增定时任务。
- 状态机只返回新状态，写库由调用方负责。

阶段：

    normal → hurt → cold → withdrawing → normal_with_scar → normal
                 ↘ repairing ↗
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from app.persona.models import Dynamics
from app.relationship.state import RelationshipState, clamp, parse_iso

if TYPE_CHECKING:
    from app.relationship.appraisal import AppraisalResult

# 这些事件算“正向”：修复时用来累计诚意。tease 视好感可正可负，
# 所以不算确定的修复信号。
POSITIVE_EVENTS: tuple[str, ...] = (
    "apology",
    "care",
    "flirt",
    "shared_interest",
)


def stage_multiplier(stage: str, dynamics: Dynamics) -> float:
    """阶段对回复概率的乘数；未知阶段按正常处理。"""
    return dynamics.conflict.stage_multipliers.get(stage, 1.0)


def repair_points_needed(affection: float, dynamics: Dynamics) -> int:
    """修复需要几次正向量：越亲近越容易哄回来，越冷淡越费劲。"""
    cfg = dynamics.conflict
    if affection >= cfg.repair_high_affection:
        return cfg.repair_points_high_affection
    if affection >= cfg.repair_low_affection:
        return cfg.repair_points_mid_affection
    return cfg.repair_points_low_affection


def stage_age_minutes(state: RelationshipState, now: str) -> float:
    """进入当前阶段多久了；时间戳缺失或损坏时返回 0（不满足停留）。"""
    if not state.stage_since:
        return 0.0
    try:
        seconds = (
            parse_iso(now) - parse_iso(state.stage_since)
        ).total_seconds()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, seconds / 60.0)


def _count_events(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    since: str,
    events: tuple[str, ...],
    confidence_min: float,
) -> int:
    placeholders = ", ".join("?" for _ in events)
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM appraisal_log
         WHERE persona_key = ? AND person_key = ?
           AND status = 'ok' AND confidence >= ?
           AND created_at >= ?
           AND event IN ({placeholders})
        """,
        (persona_key, person_key, confidence_min, since, *events),
    ).fetchone()
    return int(row[0])


def _confidence_min(dynamics: Dynamics | None) -> float:
    if dynamics is None:
        return Dynamics().appraisal.confidence_min
    return dynamics.appraisal.confidence_min


def positive_appraisal_count(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    since: str,
    dynamics: Dynamics | None = None,
) -> int:
    """`since` 之后的高置信正向评估次数。"""
    return _count_events(
        conn,
        persona_key,
        person_key,
        since=since,
        events=POSITIVE_EVENTS,
        confidence_min=_confidence_min(dynamics),
    )


def boundary_count(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    since: str,
    dynamics: Dynamics | None = None,
) -> int:
    """`since` 之后的越界次数。"""
    return _count_events(
        conn,
        persona_key,
        person_key,
        since=since,
        events=("boundary",),
        confidence_min=_confidence_min(dynamics),
    )


def _since_hours(now: str, hours: float) -> str:
    return (parse_iso(now) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _is_severe(result: "AppraisalResult", dynamics: Dynamics) -> bool:
    cfg = dynamics.conflict
    return (
        result.event in ("betrayal", "hostility")
        and result.confidence >= cfg.severe_confidence_min
        and result.intensity >= cfg.severe_intensity_min
    )


def _boundary_hit(
    conn: sqlite3.Connection,
    state: RelationshipState,
    result: "AppraisalResult",
    dynamics: Dynamics,
    now: str,
) -> bool:
    """越界是否触发受伤：短窗口内一次不算，反复越界才算。"""
    if result.event != "boundary":
        return False
    cfg = dynamics.conflict
    current = 1 if result.confidence >= dynamics.appraisal.confidence_min else 0
    within_1h = boundary_count(
        conn, state.persona_key, state.person_key,
        since=_since_hours(now, 1.0), dynamics=dynamics,
    ) + current
    within_24h = boundary_count(
        conn, state.persona_key, state.person_key,
        since=_since_hours(now, 24.0), dynamics=dynamics,
    ) + current
    return (
        within_1h >= cfg.boundary_count_1h
        or within_24h >= cfg.boundary_count_24h
    )


def _apology_ok(
    result: "AppraisalResult", dynamics: Dynamics, *, intensity_min: float
) -> bool:
    cfg = dynamics.conflict
    return (
        result.event == "apology"
        and result.confidence >= cfg.repair_confidence_min
        and result.intensity >= intensity_min
    )


def _positive_count(
    conn: sqlite3.Connection,
    state: RelationshipState,
    result: "AppraisalResult",
    dynamics: Dynamics,
    now: str,
) -> int:
    """进入当前阶段以来的正向次数；把当前这条也算进去。"""
    since = state.stage_since or now
    count = positive_appraisal_count(
        conn, state.persona_key, state.person_key,
        since=since, dynamics=dynamics,
    )
    if (
        result.event in POSITIVE_EVENTS
        and result.confidence >= dynamics.appraisal.confidence_min
    ):
        count += 1
    return count


def _dwell_ok(state: RelationshipState, stage: str, dynamics: Dynamics, now: str) -> bool:
    minutes = dynamics.conflict.min_dwell_minutes.get(stage, 0)
    return stage_age_minutes(state, now) >= float(minutes)


def _enter(
    state: RelationshipState,
    stage: str,
    *,
    now: str,
    scar_level: float | None = None,
) -> RelationshipState:
    return replace(
        state,
        conflict_stage=stage,
        stage_since=now,
        scar_level=clamp(
            state.scar_level if scar_level is None else scar_level, 0.0, 1.0
        ),
    )


def advance_by_event(
    conn: sqlite3.Connection,
    state: RelationshipState,
    *,
    result: "AppraisalResult",
    dynamics: Dynamics,
    now: str | None = None,
) -> RelationshipState:
    """按这一轮的评估事件推进阶段；不写库，由调用方保存。"""
    stamp = now or state.updated_at
    cfg = dynamics.conflict
    stage = state.conflict_stage
    severe = _is_severe(result, dynamics)
    boundary = _boundary_hit(conn, state, result, dynamics, stamp)
    scar = state.scar_level

    def with_scar(extra: float) -> float:
        return clamp(scar + extra, 0.0, 1.0)

    if stage in ("normal", "normal_with_scar"):
        if severe:
            return _enter(state, "hurt", now=stamp, scar_level=with_scar(cfg.scar_on_severe))
        if boundary:
            return _enter(state, "hurt", now=stamp)
        return state

    if stage == "hurt":
        if severe or boundary:
            return _enter(state, "cold", now=stamp)
        dwell = _dwell_ok(state, "hurt", dynamics, stamp)
        if dwell and _apology_ok(
            result, dynamics, intensity_min=cfg.repair_intensity_min
        ):
            return _enter(state, "repairing", now=stamp)
        needed = repair_points_needed(state.affection, dynamics)
        if dwell and _positive_count(conn, state, result, dynamics, stamp) >= needed:
            return _enter(state, "repairing", now=stamp)
        return state

    if stage == "cold":
        if severe:
            return _enter(state, "withdrawing", now=stamp)
        dwell = _dwell_ok(state, "cold", dynamics, stamp)
        if dwell and _apology_ok(
            result, dynamics, intensity_min=cfg.repair_intensity_min
        ):
            return _enter(state, "repairing", now=stamp)
        needed = repair_points_needed(state.affection, dynamics)
        if (
            dwell
            and state.security >= cfg.repair_security_from_cold
            and _positive_count(conn, state, result, dynamics, stamp) >= needed
        ):
            return _enter(state, "repairing", now=stamp)
        return state

    if stage == "withdrawing":
        if severe:
            return _enter(state, "withdrawing", now=stamp, scar_level=with_scar(cfg.scar_on_relapse))
        if _dwell_ok(state, "withdrawing", dynamics, stamp):
            if _apology_ok(
                result, dynamics, intensity_min=cfg.withdraw_apology_intensity_min
            ):
                # 退缩状态下不接受“一句对不起”：道歉之外还要有其他正向互动。
                others = positive_appraisal_count(
                    conn, state.persona_key, state.person_key,
                    since=_since_hours(stamp, 24.0), dynamics=dynamics,
                )
                if others >= 1:
                    return _enter(state, "repairing", now=stamp)
        return state

    if stage == "repairing":
        if severe:
            return _enter(state, "withdrawing", now=stamp, scar_level=with_scar(cfg.scar_on_relapse))
        needed = repair_points_needed(state.affection, dynamics)
        if (
            stage_age_minutes(state, stamp) >= 24 * 60
            and state.security >= cfg.repair_security_normal
            and _positive_count(conn, state, result, dynamics, stamp) >= needed
        ):
            return _enter(state, "normal_with_scar", now=stamp)
        return state

    # 未知阶段（脏数据）回退到 normal，不继续传播。
    return _enter(state, "normal", now=stamp)


def advance_by_time(
    conn: sqlite3.Connection,
    state: RelationshipState,
    *,
    dynamics: Dynamics,
    now: str | None = None,
    previous_updated_at: str | None = None,
) -> RelationshipState:
    """按流逝时间恶化阶段、淡化伤疤。

    只做三件事：最短停留之后的自然恶化、无修复时降级、疤痕淡化。
    不会因为“过了很久”就直接回到 normal——只有 `normal_with_scar`
    在疤痕淡到阈值以下时才回去。
    """
    stamp = now or state.updated_at
    cfg = dynamics.conflict
    source = previous_updated_at or state.updated_at

    scar = state.scar_level
    try:
        elapsed_days = (
            parse_iso(stamp) - parse_iso(source)
        ).total_seconds() / 86_400.0
    except (TypeError, ValueError):
        elapsed_days = 0.0
    if elapsed_days > 0 and scar > 0:
        scar = clamp(
            scar * 0.5 ** (elapsed_days / cfg.scar_halflife_days), 0.0, 1.0
        )
    current = replace(state, scar_level=scar)
    stage = current.conflict_stage
    age_minutes = stage_age_minutes(current, stamp)

    if stage == "hurt":
        if age_minutes >= cfg.hurt_to_cold_hours * 60:
            return _enter(current, "cold", now=stamp)
        if (
            age_minutes >= cfg.hurt_to_cold_low_patience_hours * 60
            and current.patience < cfg.cold_low_patience
        ):
            return _enter(current, "cold", now=stamp)
        return current

    if stage == "cold":
        if (
            age_minutes >= cfg.cold_to_withdrawing_hours * 60
            and current.security < cfg.withdraw_low_security
        ):
            return _enter(current, "withdrawing", now=stamp)
        return current

    if stage == "withdrawing":
        if age_minutes >= cfg.withdrawing_hours_to_scar * 60:
            # 再冷的冷战也要有个头：软化成一个带疤的 normal，而不是永久消失。
            return _enter(
                current,
                "normal_with_scar",
                now=stamp,
                scar_level=max(scar, 0.4),
            )
        return current

    if stage == "repairing":
        recent = positive_appraisal_count(
            conn,
            current.persona_key,
            current.person_key,
            since=_since_hours(stamp, cfg.repairing_to_cold_hours),
            dynamics=dynamics,
        )
        if recent == 0:
            return _enter(current, "cold", now=stamp)
        return current

    if stage == "normal_with_scar":
        if (
            scar < cfg.scar_fade_below
            and age_minutes >= cfg.scar_fade_min_days * 1440
        ):
            return _enter(current, "normal", now=stamp, scar_level=scar)
        return current

    return current
