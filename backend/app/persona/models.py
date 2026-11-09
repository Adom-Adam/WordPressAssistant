"""人设文件的数据模型。

这里用 Pydantic 定义“一个人设版本长什么样”。它的作用和 `schemas.py`
里的请求模型一样：在文件被读进程序时立刻校验，而不是等到拼提示词时
才发现少了一个字段。

人设文件示例（真实内容不要提交到 Git）：

    {
      "schema": "l2d.persona/v1",
      "id": "merlin",
      "version": 1,
      "name": "梅林",
      "core": "……她是谁、怎么说话……",
      "background": "……她为什么在这里（所有频道都成立的部分）……",
      "rules": "……所有频道共享的事实与安全边界……",
      "channels": {
        "web": {"overlay": "……网页专属的场景、检索与跳转规则……"},
        "qq_private": {"overlay": "……私聊额外注意事项……"},
        "qq_group": {"overlay": "……群聊额外注意事项……"}
      },
      "metadata": {"source": "current-web-prompt"}
    }

为什么把 `core / background / rules` 分成三段：
- 顺序就是“权重”：越靠前、越长的段落越容易成为模型的默认语气；
- 图书馆身份放在 core 之后，写成“差事/背景”，避免盖过梅林本人；
- 规则只负责事实准确和权限，不参与塑造性格。

`background` 和 `rules` 是**所有频道共享**的，所以只写“在网页和 QQ 里都成立”
的内容。只在网页成立的东西（图书馆场景、search_site、navigate_to_article、
页面跳转按钮）必须放进 `channels.web.overlay`；否则 QQ 也会先读到这些规则，
再靠 QQ 覆盖层“别理前面那段”去压，提示词内部会互相打架。

`channels` 是频道覆盖层。同一份核心人设，网页、QQ 私聊、QQ 群可以用不同的
篇幅、格式和礼仪要求。频道名由调用方传进 `build_system_prompt`：
网页是 `web`，QQ 私聊是 `qq_private`，QQ 群是 `qq_group`；没有覆盖层的
频道只是不加这一段，不会报错。覆盖层只能“追加”，不能删除前面的段落，
所以共享段落里不要放频道专属内容。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 人设文件格式版本。以后字段不兼容时升级这个值，加载器就能明确拒绝旧文件，
# 而不是“少读了几个字段但看起来还能跑”。
PERSONA_SCHEMA = "l2d.persona/v1"

_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# 模型评估可以输出的事件类别。顺序也是提示词里展示的顺序。
APPRAISAL_EVENTS: tuple[str, ...] = (
    "care",
    "apology",
    "flirt",
    "shared_interest",
    "smalltalk",
    "neutral",
    "tease",
    "boundary",
    "betrayal",
    "hostility",
    "spam",
)
# tease 不使用表里的固定上限，而是按好感在两条曲线之间插值，
# 所以四张上限表只需要覆盖其余事件。
APPRAISAL_CAP_EVENTS: tuple[str, ...] = tuple(
    event for event in APPRAISAL_EVENTS if event != "tease"
)

# 冲突弧的阶段。顺序也是“心理距离从近到远”的展示顺序。
CONFLICT_STAGES: tuple[str, ...] = (
    "normal",
    "hurt",
    "cold",
    "withdrawing",
    "repairing",
    "normal_with_scar",
)


class ChannelOverlay(BaseModel):
    """某个频道的附加规则。

    `overlay` 会被追加到核心人设之后、身份/页面上下文之前。
    `max_sentences` 和 `allow_actions` 目前只是给后续频道适配器读取的元数据，
    P1 不会改变网页行为。
    """

    model_config = ConfigDict(extra="forbid")

    overlay: str = Field(default="", max_length=8_000)
    max_sentences: int | None = Field(default=None, ge=1, le=20)
    allow_actions: bool = True

    @field_validator("overlay")
    @classmethod
    def _strip_overlay(cls, value: str) -> str:
        return value.strip()


class MoodDynamics(BaseModel):
    """心情的慢变量参数。valence 向人格基线衰减。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    baseline_valence: float = Field(default=0.1, ge=-1.0, le=1.0)
    valence_half_life_minutes: float = Field(default=90.0, gt=0.0, le=10_080.0)
    valence_delta_cap: float = Field(default=0.1, ge=0.0, le=2.0)
    event_delta_cap: float = Field(default=0.3, ge=0.0, le=2.0)


class EnergyDynamics(BaseModel):
    """精力参数。精力 = 本地时段曲线 − 短期会话疲劳。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    hourly_curve: list[float] = Field(
        default_factory=lambda: [
            0.15, 0.10, 0.10, 0.12, 0.20, 0.35,
            0.55, 0.70, 0.85, 0.95, 1.00, 1.00,
            0.95, 0.90, 0.90, 0.95, 1.00, 1.00,
            0.95, 0.90, 0.80, 0.65, 0.45, 0.25,
        ]
    )
    fatigue_half_life_minutes: float = Field(default=60.0, gt=0.0, le=10_080.0)
    fatigue_per_turn: float = Field(default=0.03, ge=0.0, le=1.0)
    energy_delta_cap: float = Field(default=0.2, ge=0.0, le=1.0)

    @field_validator("hourly_curve")
    @classmethod
    def _valid_curve(cls, value: list[float]) -> list[float]:
        if len(value) != 24:
            raise ValueError("hourly_curve 必须包含 24 个数")
        if any(item < 0.0 or item > 1.0 for item in value):
            raise ValueError("hourly_curve 的每个值必须在 0–1 之间")
        return value


class ReplyDynamics(BaseModel):
    """私聊回复概率与 affinity 曲线。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    base: float = Field(default=0.95, ge=0.0, le=1.0)
    floor: float = Field(default=0.02, ge=0.0, le=1.0)
    ceiling: float = Field(default=0.95, ge=0.0, le=1.0)
    debt_increment: float = Field(default=0.1, ge=0.0, le=1.0)
    debt_half_life_hours: float = Field(default=8.0, gt=0.0, le=720.0)
    debt_lambda: float = Field(default=1.5, gt=0.0, le=10.0)
    affinity_mid: float = Field(default=0.70, ge=0.0, le=2.0)
    affinity_amp: float = Field(default=0.60, ge=0.0, le=2.0)
    affinity_scale: float = Field(default=60.0, gt=0.0, le=1_000.0)
    affinity_min: float = Field(default=0.10, ge=0.0, le=2.0)
    affinity_max: float = Field(default=1.20, ge=0.0, le=2.0)
    # 耐心/安全感低时，回复概率最多被压到这两个下限。
    patience_multiplier_min: float = Field(default=0.80, ge=0.0, le=1.0)
    security_multiplier_min: float = Field(default=0.85, ge=0.0, le=1.0)
    # 模型 reply_bias 在 logit 空间里的权重。0 = 完全忽略模型倾向。
    reply_bias_weight: float = Field(default=1.0, ge=0.0, le=5.0)

    @model_validator(mode="after")
    def _ranges(self) -> "ReplyDynamics":
        if self.floor > self.ceiling:
            raise ValueError("floor 不能大于 ceiling")
        if self.affinity_min > self.affinity_max:
            raise ValueError("affinity_min 不能大于 affinity_max")
        return self


class AppraisalDynamics(BaseModel):
    """每轮私聊模型评估（event / intensity / confidence / reply_bias）的映射参数。

    模型只给“发生了什么、多强、多确定、对回不回有没有倾向”，
    具体改多少分由这里决定。四张上限表按事件名索引，键必须与
    `APPRAISAL_CAP_EVENTS` 完全一致——拼错的事件名会让配置静默失效，
    所以直接拒绝。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    confidence_min: float = Field(default=0.5, ge=0.0, le=1.0)
    daily_affection_cap: float = Field(default=3.0, ge=0.0, le=20.0)
    event_caps: dict[str, float] = Field(
        default_factory=lambda: {
            "apology": 1.2,
            "care": 1.0,
            "flirt": 0.8,
            "shared_interest": 0.6,
            "smalltalk": 0.15,
            "neutral": 0.0,
            "boundary": -1.0,
            "betrayal": -3.0,
            "hostility": -2.0,
            "spam": -0.3,
        }
    )
    event_valence_caps: dict[str, float] = Field(
        default_factory=lambda: {
            "apology": 0.04,
            "care": 0.05,
            "flirt": 0.06,
            "shared_interest": 0.04,
            "smalltalk": 0.01,
            "neutral": 0.0,
            "boundary": -0.06,
            "betrayal": -0.15,
            "hostility": -0.12,
            "spam": -0.03,
        }
    )
    event_security_caps: dict[str, float] = Field(
        default_factory=lambda: {
            "apology": 4.0,
            "care": 2.0,
            "flirt": 1.0,
            "shared_interest": 1.0,
            "smalltalk": 0.0,
            "neutral": 0.0,
            "boundary": -3.0,
            "betrayal": -8.0,
            "hostility": -5.0,
            "spam": -1.0,
        }
    )
    event_patience_caps: dict[str, float] = Field(
        default_factory=lambda: {
            "apology": 3.0,
            "care": 2.0,
            "flirt": 0.0,
            "shared_interest": 0.0,
            "smalltalk": 0.0,
            "neutral": 0.0,
            "boundary": -4.0,
            "betrayal": -10.0,
            "hostility": -6.0,
            "spam": -2.0,
        }
    )
    # 玩笑的容忍曲线：低好感时“玩笑带刺”，高好感时是亲近。
    tease_negative_until: float = Field(default=0.0, ge=-100.0, le=100.0)
    tease_positive_from: float = Field(default=60.0, ge=-100.0, le=100.0)
    tease_affection_low: float = Field(default=-0.8, ge=-10.0, le=10.0)
    tease_affection_high: float = Field(default=0.6, ge=-10.0, le=10.0)
    tease_valence_low: float = Field(default=-0.04, ge=-1.0, le=1.0)
    tease_valence_high: float = Field(default=0.03, ge=-1.0, le=1.0)

    @field_validator(
        "event_caps",
        "event_valence_caps",
        "event_security_caps",
        "event_patience_caps",
    )
    @classmethod
    def _exact_event_keys(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(APPRAISAL_CAP_EVENTS):
            missing = sorted(set(APPRAISAL_CAP_EVENTS) - set(value))
            extra = sorted(set(value) - set(APPRAISAL_CAP_EVENTS))
            raise ValueError(
                f"评估事件上限表的键必须与 APPRAISAL_CAP_EVENTS 一致，"
                f"缺少 {missing}，多出 {extra}"
            )
        return value

    @model_validator(mode="after")
    def _tease_thresholds(self) -> "AppraisalDynamics":
        if self.tease_positive_from <= self.tease_negative_until:
            raise ValueError("tease_positive_from 必须大于 tease_negative_until")
        return self


class ConflictDynamics(BaseModel):
    """受伤—修复弧的参数。

    阶段转移是确定性的：同样的事件、同样的状态，总是得到同样的下一步。
    “活人感”来自历史、好感、安全感和事件强度的差异，而不是随机数。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # 阶段对回复概率的乘数。越退缩越不想接话，但不会到 0。
    stage_multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            "normal": 1.0,
            "normal_with_scar": 0.98,
            "hurt": 0.85,
            "cold": 0.70,
            "withdrawing": 0.50,
            "repairing": 0.90,
        }
    )
    # 最短停留（分钟）：防止“5 秒内从受伤到原谅”。
    min_dwell_minutes: dict[str, int] = Field(
        default_factory=lambda: {
            "hurt": 30,
            "cold": 360,
            "withdrawing": 1440,
            "repairing": 1440,
            "normal_with_scar": 0,
        }
    )
    # 进入冲突必须同时满足置信度和强度，低置信的负面事件不能一票升级。
    severe_confidence_min: float = Field(default=0.6, ge=0.0, le=1.0)
    severe_intensity_min: float = Field(default=0.5, ge=0.0, le=1.0)
    # 越界事件的窗口计数：一次越界不算，短时间内反复越界才算受伤。
    boundary_count_1h: int = Field(default=3, ge=1, le=100)
    boundary_count_24h: int = Field(default=5, ge=1, le=1000)
    # 没有修复时的恶化时间。
    hurt_to_cold_hours: float = Field(default=24.0, gt=0.0, le=10080.0)
    hurt_to_cold_low_patience_hours: float = Field(
        default=12.0, gt=0.0, le=10080.0
    )
    cold_low_patience: float = Field(default=50.0, ge=0.0, le=100.0)
    cold_to_withdrawing_hours: float = Field(default=48.0, gt=0.0, le=10080.0)
    withdraw_low_security: float = Field(default=40.0, ge=0.0, le=100.0)
    withdrawing_hours_to_scar: float = Field(default=336.0, gt=0.0, le=87600.0)
    repairing_to_cold_hours: float = Field(default=72.0, gt=0.0, le=10080.0)
    # 修复门槛：道歉本身要达到足够强度，且往往需要不止一次正向互动。
    repair_confidence_min: float = Field(default=0.5, ge=0.0, le=1.0)
    repair_intensity_min: float = Field(default=0.5, ge=0.0, le=1.0)
    withdraw_apology_intensity_min: float = Field(default=0.7, ge=0.0, le=1.0)
    repair_security_normal: float = Field(default=60.0, ge=0.0, le=100.0)
    repair_security_from_cold: float = Field(default=40.0, ge=0.0, le=100.0)
    repair_points_high_affection: int = Field(default=1, ge=1, le=100)
    repair_points_mid_affection: int = Field(default=2, ge=1, le=100)
    repair_points_low_affection: int = Field(default=3, ge=1, le=100)
    repair_high_affection: float = Field(default=60.0, ge=-100.0, le=100.0)
    repair_low_affection: float = Field(default=0.0, ge=-100.0, le=100.0)
    # 伤疤：严重事件留下一个记号，随时间半衰；再次背叛会加深。
    scar_on_severe: float = Field(default=0.3, ge=0.0, le=1.0)
    scar_on_relapse: float = Field(default=0.2, ge=0.0, le=1.0)
    scar_halflife_days: float = Field(default=14.0, gt=0.0, le=3650.0)
    scar_fade_below: float = Field(default=0.1, ge=0.0, le=1.0)
    scar_fade_min_days: float = Field(default=7.0, ge=0.0, le=3650.0)
    scar_damage_bonus: float = Field(default=1.0, ge=0.0, le=10.0)

    @field_validator("stage_multipliers")
    @classmethod
    def _exact_stages(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(CONFLICT_STAGES):
            missing = sorted(set(CONFLICT_STAGES) - set(value))
            extra = sorted(set(value) - set(CONFLICT_STAGES))
            raise ValueError(
                f"stage_multipliers 的键必须与 CONFLICT_STAGES 一致，"
                f"缺少 {missing}，多出 {extra}"
            )
        for stage, multiplier in value.items():
            if not 0.0 <= float(multiplier) <= 1.5:
                raise ValueError(f"{stage} 的阶段乘数必须在 0–1.5 之间")
        return value

    @field_validator("min_dwell_minutes")
    @classmethod
    def _dwell_stages(cls, value: dict[str, int]) -> dict[str, int]:
        expected = set(CONFLICT_STAGES) - {"normal"}
        if set(value) != expected:
            missing = sorted(expected - set(value))
            extra = sorted(set(value) - expected)
            raise ValueError(
                f"min_dwell_minutes 的键必须覆盖 normal 以外的阶段，"
                f"缺少 {missing}，多出 {extra}"
            )
        for stage, minutes in value.items():
            if not 0 <= int(minutes) <= 10080:
                raise ValueError(f"{stage} 的最短停留必须在 0–10080 分钟之间")
        return value


class RoleDynamics(BaseModel):
    """一个角色的初始好感、增长上限和成长倍率。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    initial: float = Field(ge=-100.0, le=100.0)
    cap: float = Field(ge=0.0, le=100.0)
    growth: float = Field(default=1.0, ge=0.0, le=3.0)

    @model_validator(mode="after")
    def _initial_not_above_cap(self) -> "RoleDynamics":
        if self.initial > self.cap:
            raise ValueError("initial 不能大于 cap")
        return self


class RelationshipDynamics(BaseModel):
    """好感单位、各维度初始值和角色表。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    affection_unit: float = Field(default=1.0, ge=0.1, le=5.0)
    # 一轮正常回复带来的关系变化。忽略一条有内容的私聊不涨好感，
    # 只增加 reply.debt_increment 的回复债。
    chat_affection_units: float = Field(default=0.5, ge=0.0, le=10.0)
    chat_valence: float = Field(default=0.03, ge=-1.0, le=1.0)
    growth_curve: float = Field(default=0.8, gt=0.0, le=5.0)
    initial_missing: float = Field(default=50.0, ge=0.0, le=100.0)
    initial_security: float = Field(default=70.0, ge=0.0, le=100.0)
    initial_patience: float = Field(default=70.0, ge=0.0, le=100.0)
    # 慢变量按“天”变化：思念上升、安全感缓慢下降、耐心在无冲突时恢复。
    missing_gain_per_day: float = Field(default=6.0, ge=0.0, le=100.0)
    security_loss_per_day: float = Field(default=1.0, ge=0.0, le=100.0)
    patience_recovery_per_day: float = Field(default=2.0, ge=0.0, le=100.0)
    attention_half_life_hours: float = Field(default=24.0, gt=0.0, le=8_760.0)
    roles: dict[str, RoleDynamics] = Field(
        default_factory=lambda: {
            "owner": RoleDynamics(initial=60, cap=100, growth=1.0),
            "exception": RoleDynamics(initial=0, cap=85, growth=0.9),
            "special": RoleDynamics(initial=0, cap=75, growth=0.8),
            "normal": RoleDynamics(initial=0, cap=60, growth=0.6),
        }
    )

    @field_validator("roles")
    @classmethod
    def _all_roles(cls, value: dict[str, RoleDynamics]) -> dict[str, RoleDynamics]:
        missing = {"owner", "exception", "special", "normal"} - set(value)
        if missing:
            raise ValueError(f"缺少角色：{sorted(missing)}")
        return value


class PacingDynamics(BaseModel):
    """私聊防抖、多气泡和打字节奏。3a 只加载，4a 使用。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    debounce_seconds: float = Field(default=1.5, ge=0.0, le=30.0)
    debounce_max_seconds: float = Field(default=4.0, ge=0.0, le=60.0)
    target_chars_per_bubble: int = Field(default=32, ge=4, le=200)
    max_bubbles: int = Field(default=5, ge=1, le=10)
    min_bubble_chars: int = Field(default=10, ge=1, le=100)
    chars_per_second: float = Field(default=7.0, gt=0.0, le=50.0)
    min_gap_ms: int = Field(default=600, ge=0, le=30_000)
    max_gap_ms: int = Field(default=2500, ge=0, le=60_000)

    @model_validator(mode="after")
    def _ranges(self) -> "PacingDynamics":
        if self.debounce_max_seconds < self.debounce_seconds:
            raise ValueError("debounce_max_seconds 不能小于 debounce_seconds")
        if self.max_gap_ms < self.min_gap_ms:
            raise ValueError("max_gap_ms 不能小于 min_gap_ms")
        if self.min_bubble_chars > self.target_chars_per_bubble:
            raise ValueError("min_bubble_chars 不能大于 target_chars_per_bubble")
        return self


class ProactiveDynamics(BaseModel):
    """主动消息的行为参数。4b 使用。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # 用户刚在私聊里说过话时不要插主动消息：多少分钟内算“还在聊”。
    recent_active_minutes: float = Field(default=10.0, ge=0.0, le=180.0)
    # 与最近三条主动消息的相似度超过它就算重复，换个念头或不发。
    repeat_similarity: float = Field(default=0.6, gt=0.0, le=1.0)
    # 主动消息发出多久后还没有回应，就结算为“被忽略”。
    ignore_grace_hours: float = Field(default=24.0, ge=1.0, le=168.0)
    # 被忽略一次的扣分，还要乘以注意力置信度。
    ignore_affection_points: float = Field(default=0.2, ge=0.0, le=5.0)
    ignore_security_points: float = Field(default=0.5, ge=0.0, le=20.0)
    # 生成一条主动消息的超时。失败就当这一轮没想过，不影响其它人。
    compose_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)


class GroupDynamics(BaseModel):
    """群聊里被点名时的行为参数。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # 被绑定成员 @ 或前缀点名时的最低回复概率。取得高，是因为群里
    # 有别人看着，点名不回很难解释；只有冲突/低好感/低心情这类极端
    # 状态才真的可能不回。
    reply_floor: float = Field(default=0.90, ge=0.0, le=0.98)


class SafetyDynamics(BaseModel):
    """免打扰时段和危机关键词。"""

    model_config = ConfigDict(extra="forbid")

    quiet_hours: list[str] = Field(default_factory=lambda: ["00:00-07:00"])
    crisis_keywords: list[str] = Field(
        default_factory=lambda: ["自杀", "自残", "救命", "出事了"]
    )

    @field_validator("quiet_hours")
    @classmethod
    def _valid_hours(cls, value: list[str]) -> list[str]:
        pattern = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
        for item in value:
            if not pattern.fullmatch(item):
                raise ValueError(f"quiet_hours 格式不合法：{item!r}")
        return value


class NamespaceDynamics(BaseModel):
    """状态和记忆的隔离键；空字符串表示回退到人设 id。"""

    model_config = ConfigDict(extra="forbid")

    state: str = Field(default="", max_length=64)
    memory: str = Field(default="", max_length=64)

    @field_validator("state", "memory")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        if value and not _NAMESPACE_PATTERN.fullmatch(value):
            raise ValueError(f"namespace 不合法：{value!r}")
        return value


class Dynamics(BaseModel):
    """人设的动态参数。所有影响心情、回复和关系成长的默认值都在这里。"""

    model_config = ConfigDict(extra="forbid")

    mood: MoodDynamics = Field(default_factory=MoodDynamics)
    energy: EnergyDynamics = Field(default_factory=EnergyDynamics)
    reply: ReplyDynamics = Field(default_factory=ReplyDynamics)
    relationship: RelationshipDynamics = Field(default_factory=RelationshipDynamics)
    appraisal: AppraisalDynamics = Field(default_factory=AppraisalDynamics)
    conflict: ConflictDynamics = Field(default_factory=ConflictDynamics)
    pacing: PacingDynamics = Field(default_factory=PacingDynamics)
    proactive: ProactiveDynamics = Field(default_factory=ProactiveDynamics)
    group: GroupDynamics = Field(default_factory=GroupDynamics)
    safety: SafetyDynamics = Field(default_factory=SafetyDynamics)
    namespace: NamespaceDynamics = Field(default_factory=NamespaceDynamics)


class PersonaVersion(BaseModel):
    """一个不可变的人设版本。

    版本文件一旦发布就不应该原地修改——否则“同一版本号”在不同时间
    会产生不同提示词，日志和评测都无法复现。要改内容就新增版本号。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # JSON 里写 "schema"，Python 里叫 schema_version，避免和 Pydantic 的
    # 旧版 `.schema()` 方法重名。
    schema_version: Literal["l2d.persona/v1"] = Field(
        default=PERSONA_SCHEMA,
        alias="schema",
    )

    # 只允许小写字母、数字、点、下划线、连字符，防止用 "../" 跳出人设目录。
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: int = Field(ge=1, le=1_000_000)
    name: str = Field(min_length=1, max_length=120)

    core: str = Field(min_length=1, max_length=20_000)
    background: str = Field(default="", max_length=20_000)
    rules: str = Field(default="", max_length=20_000)

    channels: dict[str, ChannelOverlay] = Field(default_factory=dict)

    # 来源、导入时间、旧版开场对话等说明性信息。
    # 它不参与提示词组装，只用于审计和以后做导入/导出。
    metadata: dict[str, Any] = Field(default_factory=dict)

    # 心情、回复概率、关系成长、节奏和安全行为的人设默认值。
    # 旧版人设文件不写这一段时，全部使用各自的默认值。
    dynamics: Dynamics = Field(default_factory=Dynamics)

    @property
    def state_key(self) -> str:
        """关系状态的隔离键；没写 namespace 时就是人设 id。"""
        return self.dynamics.namespace.state or self.id

    @property
    def memory_key(self) -> str:
        """记忆的隔离键；没写 namespace 时就是人设 id。"""
        return self.dynamics.namespace.memory or self.id

    # 注意：这里故意不 strip core/background/rules。
    # 旧版 prompts.py 的常量末尾带换行，组装时会形成空行；
    # 为了迁移后提示词逐字节一致，必须保留作者写的空白。
    # 是否为空由 composer 用 `part.strip()` 判断，不会产生空段落。

    @field_validator("channels")
    @classmethod
    def _valid_channel_names(
        cls, value: dict[str, ChannelOverlay]
    ) -> dict[str, ChannelOverlay]:
        for name in value:
            if not name or len(name) > 40 or not name.replace("_", "").isalnum():
                raise ValueError(f"频道名不合法：{name!r}")
        return value
