"""每轮私聊的模型评估层。

模型只看“这一轮发生了什么”，输出事件类别、强度、置信度和一个
`reply_bias`；它不能直接改数值、不能决定回不回。数值映射、日上限、
概率合成都在规则层完成——模型跑偏时最多让这一轮的语气偏一点，
不会把关系状态带沟里。

评估是锦上添花：超时、报错、解析失败都 fail-open，本轮照常按规则回复。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Any

from app.llm_json import strip_fence, text_of
from app.persona.models import APPRAISAL_EVENTS, Dynamics
from app.relationship.appraisal_budget import day_start_iso
from app.relationship.state import (
    DEFAULT_TIMEZONE,
    apply_event,
    clamp,
    get_or_create_state,
    now_iso,
    save_state,
)

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 80

# 评估是同步等待的一次轻量调用，必须短。超时时间由运行时设置决定，
# 这里只给一个保底值，防止调用方传入 0 或负数导致 wait_for 立即失败。
_MIN_TIMEOUT_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class AppraisalResult:
    event: str
    intensity: float
    confidence: float
    reply_bias: float
    reason: str = ""


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(low, min(high, number))


def parse_appraisal(raw: str) -> AppraisalResult | None:
    """解析模型输出；不是合法评估时返回 None，绝不猜。"""
    try:
        payload = json.loads(strip_fence(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    event = payload.get("event")
    if event not in APPRAISAL_EVENTS:
        return None

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)

    return AppraisalResult(
        event=event,
        intensity=_clamp(payload.get("intensity"), 0.0, 1.0),
        confidence=_clamp(payload.get("confidence"), 0.0, 1.0),
        reply_bias=_clamp(payload.get("reply_bias"), -1.0, 1.0),
        reason=reason.strip()[:MAX_REASON_CHARS],
    )


APPRAISAL_PROMPT = """你在帮长期陪伴型角色「{persona_name}」判断用户这一轮消息的性质。
只输出一个 JSON 对象，不要输出其他任何文字。

字段：
- event：从下面列表里选一个最贴切的事件
  care（关心/体贴）、apology（道歉/认错）、flirt（暧昧/示好）、
  shared_interest（分享共同兴趣）、smalltalk（闲聊）、neutral（普通中性）、
  tease（开玩笑/逗她）、boundary（越界/不尊重）、
  betrayal（背叛/欺骗/严重失信）、hostility（辱骂/敌意）、spam（刷屏/无意义重复）
- intensity：0.0–1.0，这件事的强度
- confidence：0.0–1.0，你有多确定
- reply_bias：-1.0–1.0，这件事让你更想回（正）还是更不想回（负）
- reason：不超过 30 字的简短理由

规则：
1. 只输出一个 JSON 对象，不要输出数组、解释或 Markdown 代码块；
2. 不要写绝对值、分数、概率，也不要判断回不回——那是另一个系统的职责；
3. 拿不准就用 neutral，并把 confidence 调低；
4. 好感高时玩笑可以是亲近，但越界、欺骗、辱骂不能被当成玩笑。

当前本地时间：{local_time}

{state_context}

最近的关系评估：
{recent_events}

最近对话：
{recent}

用户这轮说的话：
{user_text}
"""


@dataclass(frozen=True, slots=True)
class AppraisalDelta:
    """映射后的关系增量与实际落账值。"""

    d_affection: float = 0.0
    d_valence: float = 0.0
    d_security: float = 0.0
    d_patience: float = 0.0
    applied_affection: float = 0.0
    applied_valence: float = 0.0
    skipped_reason: str = ""
    # 3c.1：落账后的冲突阶段，供同一轮提示词立即使用。
    stage_after: str = "normal"
    stage_changed: bool = False


def _lerp(low: float, high: float, ratio: float) -> float:
    return low + (high - low) * ratio


def map_appraisal(
    *,
    result: AppraisalResult,
    affection: float,
    dynamics: Dynamics,
    positive_used: float,
    negative_used: float,
    scar_level: float = 0.0,
) -> AppraisalDelta:
    """把一次模型评估换算成有界的关系增量。

    顺序很重要：先看置信度门槛，再按事件算原始值，最后按当天剩余额度
    裁剪。裁剪用的是“实际落账值”，避免成长曲线吃掉的部分仍占额度。
    """
    cfg = dynamics.appraisal
    if result.confidence < cfg.confidence_min:
        return AppraisalDelta(skipped_reason="low_confidence")

    weight = result.intensity * result.confidence

    if result.event == "tease":
        span = cfg.tease_positive_from - cfg.tease_negative_until
        if span > 0:
            tolerance = clamp(
                (affection - cfg.tease_negative_until) / span, 0.0, 1.0
            )
        else:
            tolerance = 1.0 if affection >= cfg.tease_positive_from else 0.0
        d_affection = _lerp(
            cfg.tease_affection_low, cfg.tease_affection_high, tolerance
        ) * weight
        d_valence = _lerp(
            cfg.tease_valence_low, cfg.tease_valence_high, tolerance
        ) * weight
        d_security = 0.0
        d_patience = 0.0
    elif result.event in ("betrayal", "hostility"):
        # 好感越高，背叛/辱骂越伤人；低好感时本来就不在意，伤害减半。
        severity = 0.5 + 0.5 * clamp(affection / 100.0, 0.0, 1.0)
        d_affection = cfg.event_caps[result.event] * weight * severity
        d_valence = cfg.event_valence_caps[result.event] * weight * severity
        d_security = cfg.event_security_caps[result.event] * weight * severity
        d_patience = cfg.event_patience_caps[result.event] * weight * severity
    else:
        d_affection = cfg.event_caps[result.event] * weight
        d_valence = cfg.event_valence_caps[result.event] * weight
        d_security = cfg.event_security_caps[result.event] * weight
        d_patience = cfg.event_patience_caps[result.event] * weight

    # 带伤疤的关系对负面事件更敏感：同样的背叛，伤过之后再犯更疼。
    if d_affection < 0 and scar_level > 0:
        d_affection *= 1.0 + clamp(scar_level, 0.0, 1.0) * dynamics.conflict.scar_damage_bonus

    if d_affection > 0:
        remaining = max(0.0, cfg.daily_affection_cap - positive_used)
        d_affection = min(d_affection, remaining)
    elif d_affection < 0:
        remaining = max(0.0, cfg.daily_affection_cap - negative_used)
        d_affection = max(d_affection, -remaining)

    return AppraisalDelta(
        d_affection=d_affection,
        d_valence=d_valence,
        d_security=d_security,
        d_patience=d_patience,
    )


def _daily_usage(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    person_key: str,
    since: str,
) -> tuple[float, float]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN d_affection > 0 THEN d_affection ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN d_affection < 0 THEN -d_affection ELSE 0 END), 0)
          FROM relationship_events
         WHERE persona_key = ? AND person_key = ? AND kind = 'appraisal'
           AND created_at >= ?
        """,
        (persona_key, person_key, since),
    ).fetchone()
    return float(row[0]), float(row[1])


def apply_appraisal(
    conn: sqlite3.Connection,
    persona_key: str,
    person_key: str,
    *,
    role: str,
    dynamics: Dynamics,
    result: AppraisalResult,
    now: str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> AppraisalDelta:
    """映射并落账一次评估；零增量或低置信不写关系事件。"""
    stamp = now or now_iso()
    since = day_start_iso(stamp, timezone_name)
    positive_used, negative_used = _daily_usage(
        conn, persona_key=persona_key, person_key=person_key, since=since
    )
    before = get_or_create_state(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        now=stamp,
        timezone_name=timezone_name,
    )
    delta = map_appraisal(
        result=result,
        affection=before.affection,
        dynamics=dynamics,
        positive_used=positive_used,
        negative_used=negative_used,
        scar_level=before.scar_level,
    )
    if delta.skipped_reason:
        return replace(delta, stage_after=before.conflict_stage)
    if not any(
        (delta.d_affection, delta.d_valence, delta.d_security, delta.d_patience)
    ):
        return replace(delta, stage_after=before.conflict_stage)

    after = apply_event(
        conn,
        persona_key,
        person_key,
        role=role,
        dynamics=dynamics,
        kind="appraisal",
        d_affection_points=delta.d_affection,
        d_valence=delta.d_valence,
        d_security=delta.d_security,
        d_patience=delta.d_patience,
        confidence=result.confidence,
        note=result.reason[:MAX_REASON_CHARS],
        touch_interaction=False,
        now=stamp,
        timezone_name=timezone_name,
    )

    # 事件先落账，再推进冲突阶段；两者用同一个时间戳，避免“先伤后赔”
    # 的边界抖动。
    from app.relationship.conflict import advance_by_event

    advanced = advance_by_event(
        conn, after, result=result, dynamics=dynamics, now=stamp
    )
    if advanced.conflict_stage != after.conflict_stage or advanced.scar_level != after.scar_level:
        save_state(conn, advanced)

    return replace(
        delta,
        applied_affection=after.affection - before.affection,
        applied_valence=after.valence - before.valence,
        stage_after=advanced.conflict_stage,
        stage_changed=advanced.conflict_stage != before.conflict_stage,
    )


def record_appraisal_log(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    person_key: str,
    message_id: str | None,
    result: AppraisalResult | None,
    delta: AppraisalDelta | None = None,
    latency_ms: int = 0,
    status: str,
    raw_output: str = "",
    skipped_reason: str = "",
    channel_id: str = "qq_private",
    now: str | None = None,
) -> None:
    """写一行审计日志；失败原因也留下，方便判断是模型还是规则的问题。"""
    from app.relationship.state import now_iso

    stamp = now or now_iso()
    delta = delta or AppraisalDelta()
    reason = result.reason if result is not None else ""
    conn.execute(
        """
        INSERT INTO appraisal_log (
            persona_key, person_key, channel_id, message_id, event, intensity,
            confidence, reply_bias, reason, raw_output, d_affection, d_valence,
            d_security, d_patience, applied_affection, applied_valence,
            latency_ms, status, skipped_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_key,
            person_key,
            channel_id,
            message_id,
            result.event if result is not None else None,
            result.intensity if result is not None else 0.0,
            result.confidence if result is not None else 0.0,
            result.reply_bias if result is not None else 0.0,
            reason,
            raw_output[:4000],
            delta.d_affection,
            delta.d_valence,
            delta.d_security,
            delta.d_patience,
            delta.applied_affection,
            delta.applied_valence,
            max(0, int(latency_ms)),
            status,
            skipped_reason or delta.skipped_reason,
            stamp,
        ),
    )
    conn.commit()


def build_appraisal_prompt(
    *,
    persona_name: str,
    user_text: str,
    recent: list[str],
    state_context: str,
    local_time: str = "",
    recent_events: list[str] | None = None,
) -> str:
    """拼评估提示词。上下文只给最近几条，控制延迟和成本。"""
    events = [item for item in (recent_events or []) if item]
    return APPRAISAL_PROMPT.format(
        persona_name=persona_name,
        local_time=local_time or "（未提供）",
        state_context=state_context.strip() or "（无）",
        recent_events="\n".join(f"- {item}" for item in events[-3:]) or "（无）",
        recent="\n".join(f"- {item}" for item in recent[-4:]) or "（无）",
        user_text=(user_text or "")[:1500],
    )


@dataclass(frozen=True, slots=True)
class AppraisalCall:
    """一次评估调用的结果与状态；任何失败都不向上抛异常。"""

    result: AppraisalResult | None
    status: str
    raw_output: str = ""
    latency_ms: int = 0


class Appraiser:
    """默认用和聊天同一个模型，但温度 0、只给 200 token。

    这样评估输出稳定、便宜，而且不会因为“创作欲”写出长解释。
    测试可以注入任何带 `ainvoke` 的对象。
    """

    def __init__(self, model: Any = None) -> None:
        self.model = model

    def _model(self) -> Any:
        if self.model is not None:
            return self.model
        from app.agent.model import build_chat_model
        from app.runtime_settings import get_runtime_store

        runtime = get_runtime_store().load()
        return build_chat_model(
            runtime.model, temperature=0.0, max_tokens=200
        )

    async def appraise(
        self,
        *,
        persona_name: str,
        user_text: str,
        recent: list[str],
        state_context: str,
        timeout: float,
        local_time: str = "",
        recent_events: tuple[str, ...] | list[str] = (),
    ) -> AppraisalCall:
        prompt = build_appraisal_prompt(
            persona_name=persona_name,
            user_text=user_text,
            recent=recent,
            state_context=state_context,
            local_time=local_time,
            recent_events=list(recent_events),
        )
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._model().ainvoke(prompt),
                timeout=max(_MIN_TIMEOUT_SECONDS, float(timeout)),
            )
        except TimeoutError:
            return AppraisalCall(
                None, "timeout", "", int((time.monotonic() - started) * 1000)
            )
        except Exception:  # noqa: BLE001 - 评估失败不影响回复
            logger.warning("关系评估调用失败，本轮按规则处理", exc_info=True)
            return AppraisalCall(
                None, "error", "", int((time.monotonic() - started) * 1000)
            )

        raw = text_of(response)
        parsed = parse_appraisal(raw)
        latency_ms = int((time.monotonic() - started) * 1000)
        if parsed is None:
            return AppraisalCall(None, "parse_error", raw, latency_ms)
        return AppraisalCall(parsed, "ok", raw, latency_ms)
