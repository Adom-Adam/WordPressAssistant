"""私聊回复决策：这一条消息，梅林想不想回。

设计文档 5.3 把“回不回”拆成几个正交信号：

    willingness  只看这一条消息本身值不值得接（问句、叫名字、复读、刷屏）
    affinity     由 affection 派生的慢变量（对这个人整体愿意投入多少）
    valence      这一阵的心情好坏
    energy       这一阵有没有精力
    reply_debt   被忽略的消息积累的回复意愿

这里只做纯计算，不读数据库、不发消息。随机数由调用方注入，
测试和线上都能复现同一次掷骰子。

为什么要 fail-open：决策层依赖关系库和人设文件，但“梅林不说话”
不应该是故障的表现。调用方（QQ 适配器）捕获异常后强制回复，
把“新功能坏了”和“她选择沉默”区分开。
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.persona.models import Dynamics
from app.relationship.conflict import stage_multiplier
from app.relationship.state import clamp, now_iso

# 问句/请求的信号词。刻意保持保守：只把明显在提问或求助的消息抬高，
# 不试图做语义理解；真正的 judge 以后可以替换 willingness_for。
_QUESTION_WORDS = (
    "怎么",
    "为什么",
    "什么",
    "如何",
    "哪",
    "谁",
    "多少",
    "能不能",
    "可不可以",
    "帮我",
)
_QUESTION_ENDINGS = ("？", "?", "吗", "呢", "吧")


@dataclass(frozen=True, slots=True)
class Decision:
    """一次回复决策的完整证据，直接对应 decision_log 的一行。"""

    willingness: float
    affinity: float
    valence: float
    energy: float
    reply_debt: float
    p0: float
    p: float
    roll: float | None
    reply: bool
    reason: str
    # 4.7 的模型评估证据。默认值让旧调用方不受影响。
    rule_willingness: float = 0.0
    reply_bias: float = 0.0
    appraisal_event: str | None = None
    appraisal_confidence: float = 0.0
    # 3c.1：这一轮所处的冲突阶段，方便事后解释“为什么突然冷淡”。
    conflict_stage: str = "normal"


def _normalize(text: str) -> str:
    """去掉所有空白并转小写，用于“复读/刷屏”比较。"""
    return "".join(str(text).split()).lower()


def willingness_for(
    text: str,
    recent: Sequence[str] = (),
    *,
    name: str = "",
) -> float:
    """按消息内容估计“这一条想不想接”，范围 0.05–1.0。

    这不是情感判断，只是把设计文档列出的可观察信号折算成一个系数：
    问句和叫名字更容易接，复读和刷屏更不想接。
    """
    normalized = _normalize(text)
    score = 0.55

    if normalized.endswith(_QUESTION_ENDINGS) or any(
        word in normalized for word in _QUESTION_WORDS
    ):
        score += 0.25

    if name and name in normalized:
        score += 0.15

    if len(normalized) >= 20:
        score += 0.05

    # 有一个正在进行的对话窗口（本条是在接上一句），愿意接的概率更高。
    if recent:
        score += 0.05

    recent_normalized = [_normalize(item) for item in recent]
    if recent_normalized:
        occurrences = recent_normalized.count(normalized)
        if occurrences >= 1:
            score -= 0.35
        if occurrences >= 2:
            score -= 0.2
        if len(recent_normalized) >= 5:
            score -= 0.15

    return clamp(score, 0.05, 1.0)


def blend_willingness(
    rule: float, reply_bias: float, confidence: float, weight: float
) -> float:
    """在 logit 空间把规则意愿和模型倾向合成。

    规则意愿先夹到 (0,1) 避免 log(0)；模型倾向按置信度加权，所以
    “不确定的评估”不会大幅改变本轮行为。最终仍夹在 0.05–1.0，
    保证任何消息都有一点点被接住的机会。
    """
    rule = clamp(rule, 0.001, 0.999)
    z = math.log(rule / (1.0 - rule)) + weight * confidence * reply_bias
    blended = 1.0 / (1.0 + math.exp(-z))
    return clamp(blended, 0.05, 1.0)


def _relationship_multiplier(value: float, floor: float) -> float:
    """把 patience/security 的 0–100 映射到 `[floor, 1.0]`。"""
    return floor + (1.0 - floor) * clamp(value / 100.0, 0.0, 1.0)


def reply_probability(
    *,
    willingness: float,
    affinity: float,
    valence: float,
    energy: float,
    reply_debt: float,
    dynamics: Dynamics,
    patience: float = 100.0,
    security: float = 100.0,
    stage_multiplier: float = 1.0,
) -> tuple[float, float]:
    """返回 `(p0, p)`：基础概率与叠加回复债后的最终概率。

    `stage_multiplier` 来自冲突阶段：受伤、冷战、退缩时会压低回复意愿，
    但不会压到 0，危机和命令仍然可以强制回复。
    """
    cfg = dynamics.reply
    p0 = clamp(
        cfg.base
        * willingness
        * affinity
        * math.exp(0.8 * valence)
        * (0.55 + 0.45 * energy)
        * _relationship_multiplier(patience, cfg.patience_multiplier_min)
        * _relationship_multiplier(security, cfg.security_multiplier_min)
        * max(0.0, float(stage_multiplier)),
        cfg.floor,
        cfg.ceiling,
    )
    p = p0 + (0.98 - p0) * (1.0 - math.exp(-cfg.debt_lambda * reply_debt))
    return p0, clamp(p, cfg.floor, 0.98)


def decide(
    *,
    text: str,
    recent: Sequence[str],
    affinity: float,
    valence: float,
    energy: float,
    reply_debt: float,
    dynamics: Dynamics,
    rng: random.Random,
    force_reply: bool = False,
    reason: str = "",
    name: str = "",
    patience: float = 100.0,
    security: float = 100.0,
    reply_bias: float = 0.0,
    bias_confidence: float = 0.0,
    appraisal_event: str | None = None,
    stage: str = "normal",
) -> Decision:
    """掷一次骰子。`force_reply=True` 时不消耗随机数。"""
    rule_willingness = willingness_for(text, recent, name=name)
    willingness = blend_willingness(
        rule_willingness,
        reply_bias,
        bias_confidence,
        dynamics.reply.reply_bias_weight,
    )
    p0, p = reply_probability(
        willingness=willingness,
        affinity=affinity,
        valence=valence,
        energy=energy,
        reply_debt=reply_debt,
        dynamics=dynamics,
        patience=patience,
        security=security,
        stage_multiplier=stage_multiplier(stage, dynamics),
    )

    if force_reply:
        return Decision(
            willingness=willingness,
            affinity=affinity,
            valence=valence,
            energy=energy,
            reply_debt=reply_debt,
            p0=p0,
            p=p,
            roll=None,
            reply=True,
            reason=reason or "forced",
            rule_willingness=rule_willingness,
            reply_bias=reply_bias,
            appraisal_event=appraisal_event,
            appraisal_confidence=bias_confidence,
            conflict_stage=stage,
        )

    roll = rng.random()
    return Decision(
        willingness=willingness,
        affinity=affinity,
        valence=valence,
        energy=energy,
        reply_debt=reply_debt,
        p0=p0,
        p=p,
        roll=roll,
        reply=roll < p,
        reason=reason or "probabilistic",
        rule_willingness=rule_willingness,
        reply_bias=reply_bias,
        appraisal_event=appraisal_event,
        appraisal_confidence=bias_confidence,
        conflict_stage=stage,
    )


def record_decision(
    conn: sqlite3.Connection,
    *,
    persona_key: str,
    channel_id: str,
    person_key: str,
    message_id: int | str | None,
    decision: Decision,
    rule_score: float = 0.0,
    now: str | None = None,
) -> int:
    """把一次决策写进 `decision_log`，返回行号。"""
    cursor = conn.execute(
        """
        INSERT INTO decision_log (
            persona_key, channel_id, person_key, message_id, rule_score,
            willingness, valence, energy, affinity, reply_debt,
            p0, p, roll, verdict, reason, reply_bias, appraisal_event,
            appraisal_confidence, conflict_stage, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_key,
            channel_id,
            person_key,
            None if message_id is None else str(message_id),
            float(rule_score),
            decision.willingness,
            decision.valence,
            decision.energy,
            decision.affinity,
            decision.reply_debt,
            decision.p0,
            decision.p,
            decision.roll,
            "reply" if decision.reply else "silent",
            decision.reason,
            decision.reply_bias,
            decision.appraisal_event,
            decision.appraisal_confidence,
            decision.conflict_stage,
            now or now_iso(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)
