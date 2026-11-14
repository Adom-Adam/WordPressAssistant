"""按 (persona_key, person_key) 读写关系状态。

这里只放“慢变量”：好感、思念、安全感、耐心、心情、精力、回复债。
单条消息想不想回（willingness）不在这里算，它由 4a 的回复决策层负责。

为什么按 persona 再分一层：
- 同一个人在不同人设下应该有不同的关系和记忆，默认互不污染；
- 同一个人设升级版本时 persona_key 不变，关系和记忆可以延续；
- 想让人设共用状态时，在人设文件里写同一个 namespace 即可。

时间统一存 UTC ISO 字符串。`decay_state()` 是纯函数：给定旧状态和
当前时间，返回衰减后的新状态；调用方负责写回。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.persona.models import Dynamics, RoleDynamics

DEFAULT_TIMEZONE = "Asia/Shanghai"

ROLE_NAMES = ("owner", "exception", "special", "normal")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """解析 ISO 时间；没有时区时按 UTC 处理。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _zone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def energy_at(
    when: str,
    curve: list[float],
    timezone_name: str = DEFAULT_TIMEZONE,
) -> float:
    """取某个本地整点对应的精力曲线值；疲劳由调用方另行扣除。"""
    hour = parse_iso(when).astimezone(_zone(timezone_name)).hour
    return clamp(curve[hour], 0.0, 1.0)


def affinity_for(affection: float, dynamics: Dynamics) -> float:
    """把 -100–100 的 affection 换算成回复概率里的慢变量倍率。

    用 tanh 跨过 0 点平滑过渡，两端自然饱和；0 分仍保留基础礼貌。
    """
    cfg = dynamics.reply
    value = cfg.affinity_mid + cfg.affinity_amp * math.tanh(
        affection / cfg.affinity_scale
    )
    return clamp(value, cfg.affinity_min, cfg.affinity_max)


def closeness_for(affection: float, dynamics: Dynamics) -> float:
    """把好感换算成 0–1 的亲近度，给主动消息打分用。

    回复概率用的 `affinity_for` 是“愿不愿意投入”的倍率，中心在 1.5；
    主动消息关心的是“这个人离我多近”，0.5 表示刚认识。两者分开，
    调回复概率时不会把陌生人连带算成亲密对象。
    """
    scale = dynamics.reply.affinity_scale
    return clamp(0.5 + 0.5 * math.tanh(affection / scale), 0.0, 1.0)


def role_config(role: str, dynamics: Dynamics) -> RoleDynamics:
    """未知角色回退到 normal，避免脏数据把对话打挂。"""
    return dynamics.relationship.roles.get(role) or dynamics.relationship.roles[
        "normal"
    ]


def role_for(conn: sqlite3.Connection, person_key: str) -> str:
    """从 persons 表读角色；没有记录或角色非法时返回 normal。"""
    row = conn.execute(
        "SELECT role FROM persons WHERE person_key = ?", (person_key,)
    ).fetchone()
    if row is not None and row["role"] in ROLE_NAMES:
        return row["role"]
    return "normal"


@dataclass(frozen=True, slots=True)
class RelationshipState:
    persona_key: str
    person_key: str
    affection: float
    missing: float
    security: float
    patience: float
    valence: float
    energy: float
    fatigue: float
    reply_debt: float
    last_interaction_at: str | None
    last_proactive_at: str | None
    last_reply_at: str | None
    attention_confidence: float
    updated_at: str
    # 3c.1 冲突弧：当前阶段、进入阶段的时间、以及留下的伤疤。
    conflict_stage: str = "normal"
    stage_since: str | None = None
    scar_level: float = 0.0


def state_from_row(row: sqlite3.Row) -> RelationshipState:
    return RelationshipState(
        persona_key=row["persona_key"],
        person_key=row["person_key"],
        affection=float(row["affection"]),
        missing=float(row["missing"]),
        security=float(row["security"]),
        patience=float(row["patience"]),
        valence=float(row["valence"]),
        energy=float(row["energy"]),
        fatigue=float(row["fatigue"]),
        reply_debt=float(row["reply_debt"]),
        last_interaction_at=row["last_interaction_at"],
        last_proactive_at=row["last_proactive_at"],
        last_reply_at=row["last_reply_at"],
        attention_confidence=float(row["attention_confidence"]),
        updated_at=row["updated_at"],
        conflict_stage=row["conflict_stage"] or "normal",
        stage_since=row["stage_since"],
        scar_level=float(row["scar_level"] or 0.0),
    )


def get_state(
    conn: sqlite3.Connection, persona_key: str, person_key: str
) -> RelationshipState | None:
    row = conn.execute(
        """
        SELECT * FROM relationship_state
         WHERE persona_key = ? AND person_key = ?
        """,
        (persona_key, person_key),
    ).fetchone()
    return state_from_row(row) if row is not None else None


def save_state(
    conn: sqlite3.Connection,
    state: RelationshipState,
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO relationship_state (
            persona_key, person_key, affection, missing, security, patience,
            valence, energy, fatigue, reply_debt, last_interaction_at,
            last_proactive_at, last_reply_at, attention_confidence,
            conflict_stage, stage_since, scar_level, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(persona_key, person_key) DO UPDATE SET
            affection = excluded.affection,
            missing = excluded.missing,
            security = excluded.security,
            patience = excluded.patience,
            valence = excluded.valence,
            energy = excluded.energy,
            fatigue = excluded.fatigue,
            reply_debt = excluded.reply_debt,
            last_interaction_at = excluded.last_interaction_at,
            last_proactive_at = excluded.last_proactive_at,
            last_reply_at = excluded.last_reply_at,
            attention_confidence = excluded.attention_confidence,
            conflict_stage = excluded.conflict_stage,
            stage_since = excluded.stage_since,
            scar_level = excluded.scar_level,
            updated_at = excluded.updated_at
        """,
        (
            state.persona_key,
            state.person_key,
            state.affection,
            state.missing,
            state.security,
            state.patience,
            state.valence,
            state.energy,
            state.fatigue,
            state.reply_debt,
            state.last_interaction_at,
            state.last_proactive_at,
            state.last_reply_at,
            state.attention_confidence,
            state.conflict_stage,
            state.stage_since,
            state.scar_level,
            state.updated_at,
        ),
    )
    if commit:
        conn.commit()


def get_or_create_state(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> RelationshipState:
    existing = get_state(conn, persona_key, person_key)
    if existing is not None:
        return existing

    stamp = now or now_iso()
    config = role_config(role, dynamics)
    state = RelationshipState(
        persona_key=persona_key,
        person_key=person_key,
        affection=config.initial,
        missing=dynamics.relationship.initial_missing,
        security=dynamics.relationship.initial_security,
        patience=dynamics.relationship.initial_patience,
        valence=dynamics.mood.baseline_valence,
        energy=energy_at(
            stamp, dynamics.energy.hourly_curve, timezone_name=timezone_name
        ),
        fatigue=0.0,
        reply_debt=0.0,
        last_interaction_at=None,
        last_proactive_at=None,
        last_reply_at=None,
        attention_confidence=0.0,
        updated_at=stamp,
        conflict_stage="normal",
        stage_since=stamp,
        scar_level=0.0,
    )
    save_state(conn, state)
    return state


def decay_state(
    state: RelationshipState,
    dynamics: Dynamics,
    *,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> RelationshipState:
    """按流逝时间衰减慢变量，并重算精力。

    valence 向人格基线回一半；reply_debt 每过一个半衰期减半；
    fatigue 每过一个 ``fatigue_half_life_minutes`` 减半；missing 随时间
    上升、security 缓慢下降、patience 在无冲突时恢复，attention 按半衰期
    衰减。``energy = 本地小时曲线值 - fatigue``。时间戳损坏时不改变状态，
    只把 updated_at 推到当前；时间差为负或为零时慢变量不衰减，但精力
    仍按当前本地小时重算。
    """
    stamp = now or now_iso()
    try:
        elapsed_seconds = (
            parse_iso(stamp) - parse_iso(state.updated_at)
        ).total_seconds()
    except (TypeError, ValueError):
        return replace(state, updated_at=stamp)

    curve = dynamics.energy.hourly_curve

    if elapsed_seconds <= 0:
        return replace(
            state,
            energy=clamp(
                energy_at(stamp, curve, timezone_name=timezone_name)
                - state.fatigue,
                0.0,
                1.0,
            ),
            updated_at=stamp,
        )

    valence_factor = 0.5 ** (
        (elapsed_seconds / 60.0) / dynamics.mood.valence_half_life_minutes
    )
    valence = dynamics.mood.baseline_valence + (
        state.valence - dynamics.mood.baseline_valence
    ) * valence_factor

    debt_factor = 0.5 ** (
        (elapsed_seconds / 3600.0) / dynamics.reply.debt_half_life_hours
    )
    reply_debt = max(0.0, state.reply_debt * debt_factor)

    fatigue_factor = 0.5 ** (
        (elapsed_seconds / 60.0) / dynamics.energy.fatigue_half_life_minutes
    )
    fatigue = clamp(state.fatigue * fatigue_factor, 0.0, 1.0)
    energy = clamp(
        energy_at(stamp, curve, timezone_name=timezone_name) - fatigue,
        0.0,
        1.0,
    )

    elapsed_days = elapsed_seconds / 86_400.0
    relationship = dynamics.relationship
    missing = clamp(
        state.missing + relationship.missing_gain_per_day * elapsed_days,
        0.0,
        100.0,
    )
    security = clamp(
        state.security - relationship.security_loss_per_day * elapsed_days,
        0.0,
        100.0,
    )
    patience = clamp(
        state.patience + relationship.patience_recovery_per_day * elapsed_days,
        0.0,
        100.0,
    )
    attention_factor = 0.5 ** (
        (elapsed_seconds / 3600.0) / relationship.attention_half_life_hours
    )
    attention_confidence = clamp(
        state.attention_confidence * attention_factor, 0.0, 1.0
    )

    return replace(
        state,
        missing=missing,
        security=security,
        patience=patience,
        valence=clamp(valence, -1.0, 1.0),
        energy=energy,
        fatigue=fatigue,
        reply_debt=reply_debt,
        attention_confidence=attention_confidence,
        updated_at=stamp,
    )


def _record_event(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    person_key: str,
    kind: str,
    d_affection: float,
    d_missing: float,
    d_security: float,
    d_patience: float,
    d_valence: float,
    d_energy: float,
    d_debt: float,
    confidence: float,
    source_event_id: int | None,
    note: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO relationship_events (
            persona_key, person_key, kind, d_affection, d_missing, d_security,
            d_patience, d_valence, d_energy, d_debt, confidence,
            source_event_id, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_key,
            person_key,
            kind,
            d_affection,
            d_missing,
            d_security,
            d_patience,
            d_valence,
            d_energy,
            d_debt,
            clamp(confidence, 0.0, 1.0),
            source_event_id,
            note,
            created_at,
        ),
    )


def apply_event(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    kind: str,
    d_affection_units: float = 0.0,
    d_affection_points: float = 0.0,
    d_missing: float = 0.0,
    d_security: float = 0.0,
    d_patience: float = 0.0,
    d_valence: float = 0.0,
    d_energy: float = 0.0,
    d_debt: float = 0.0,
    confidence: float = 1.0,
    source_event_id: int | None = None,
    note: str = "",
    touch_interaction: bool = True,
    touch_reply: bool = False,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> RelationshipState:
    """把一次关系事件写进状态和事件表，返回更新后的状态。

    affection 的正面变化乘角色成长倍率，并随接近上限而递减；负面变化
    不乘成长倍率。范围是 -100–100，不设普通关系下限。
    """
    stamp = now or now_iso()
    before = get_or_create_state(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        now=stamp,
        timezone_name=timezone_name,
    )
    base = decay_state(
        before, dynamics, now=stamp, timezone_name=timezone_name
    )
    config = role_config(role, dynamics)

    # 评估层已经算好原始好感点数时直接使用；否则按“单位 × affection_unit”
    # 换算（聊天、沉默等规则事件走这条路）。
    raw_affection = (
        float(d_affection_points)
        if d_affection_points
        else float(d_affection_units) * dynamics.relationship.affection_unit
    )
    if raw_affection > 0:
        # 越接近角色上限，单次正面事件带来的增长越小；0 分和负分区间
        # 不打折，保证陌生人和受伤关系都还能被修复。
        raw_affection *= config.growth
        if config.cap > 0:
            ratio = clamp(max(base.affection, 0.0) / config.cap, 0.0, 1.0)
            raw_affection *= (1.0 - ratio) ** dynamics.relationship.growth_curve
        else:
            raw_affection = 0.0
    affection = clamp(base.affection + raw_affection, -100.0, 100.0)
    if raw_affection > 0:
        affection = min(affection, config.cap)

    valence_delta = clamp(
        float(d_valence),
        -dynamics.mood.valence_delta_cap,
        dynamics.mood.valence_delta_cap,
    )
    valence = clamp(base.valence + valence_delta, -1.0, 1.0)

    energy_delta = clamp(
        float(d_energy),
        -dynamics.energy.energy_delta_cap,
        dynamics.energy.energy_delta_cap,
    )
    # 负的 d_energy 表示这一轮消耗精力，等价于增加疲劳；正的 d_energy
    # 表示聊得开心、恢复精力，等价于减少疲劳。
    fatigue = clamp(base.fatigue - energy_delta, 0.0, 1.0)
    energy = clamp(
        energy_at(stamp, dynamics.energy.hourly_curve, timezone_name=timezone_name)
        - fatigue,
        0.0,
        1.0,
    )

    missing = clamp(base.missing + float(d_missing), 0.0, 100.0)
    security = clamp(base.security + float(d_security), 0.0, 100.0)
    patience = clamp(base.patience + float(d_patience), 0.0, 100.0)
    reply_debt = max(0.0, base.reply_debt + float(d_debt))

    state = replace(
        base,
        affection=affection,
        missing=missing,
        security=security,
        patience=patience,
        valence=valence,
        energy=energy,
        fatigue=fatigue,
        reply_debt=reply_debt,
        last_interaction_at=(
            stamp if touch_interaction else base.last_interaction_at
        ),
        last_reply_at=(stamp if touch_reply else base.last_reply_at),
        updated_at=stamp,
    )

    save_state(conn, state, commit=False)
    _record_event(
        conn,
        persona_key=persona_key,
        person_key=person_key,
        kind=kind,
        d_affection=state.affection - base.affection,
        d_missing=state.missing - base.missing,
        d_security=state.security - base.security,
        d_patience=state.patience - base.patience,
        d_valence=state.valence - base.valence,
        d_energy=state.energy - base.energy,
        d_debt=state.reply_debt - base.reply_debt,
        confidence=confidence,
        source_event_id=source_event_id,
        note=note,
        created_at=stamp,
    )
    conn.commit()
    return state
