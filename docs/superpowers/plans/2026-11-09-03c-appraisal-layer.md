# 3c 每轮私聊模型评估实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每个防抖合并后的私聊回合，在掷骰子前同步调用一次轻量模型评估，输出事件类别、强度、置信度和 `reply_bias`；规则概率与 `reply_bias` 用 logit 合成，关系变化在掷骰子后落账，并有独立预算和完整审计。

**Architecture:** 新增 `app/relationship/appraisal.py`（提示词、解析、映射、`Appraiser`）和 `app/relationship/appraisal_budget.py`（本地日、原子预算）。适配器在 `_private_round` 里先读状态、再评估、最后决策；`record_decision` / `relationship_events` / `appraisal_log` 记录证据。事件上限和容忍曲线进人设 `dynamics.appraisal`；全站/每人预算和超时进运行时设置，由 WordPress 后台修改。

**Tech Stack:** Python 3.11+、Pydantic v2、SQLite、LangChain `ChatDeepSeek`、asyncio、pytest、WordPress PHP 设置页。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（4.7、5.3、9、10.1、10.3）

**Scope note:** 这份计划实现 4.7 的模型评估层、`relationship_events(kind="appraisal")` 事件落账和 `appraisal_log` 审计。4.4 的冲突弧 `normal → hurt → cold → withdrawing → repairing → normal_with_scar` 是独立状态机，见 [`2026-11-09-03c1-conflict-arc.md`](2026-11-09-03c1-conflict-arc.md)，不塞进本次评估层。

## Global Constraints

- 评估在**掷骰子前**调用；概率沉默也评估（危机消息除外，危机直接强回、不评估）。`qq_private_policy="silent"` 是用户显式关闭私聊回复，不评估。
- 默认超时 **1.2 秒**；超时、报错、解析失败、超预算一律 fail-open，不改变本轮回复行为。
- 模型只输出：`event` / `intensity` / `confidence` / `reply_bias` / `reason`；不能输出绝对值、概率或“回/不回”。
- `confidence < confidence_min`（默认 `0.5`）不落事件账，`reply_bias` 仍按置信度加权影响本轮；此时保留基础聊天加成作为回退。
- 每人每天模型带来的好感变化：正向最多 `+3`，负向最多 `-3`，正负分开计算，按本地日重置。
- 置信评估成功时，以模型事件为准，`chat_affection_units` / `chat_valence` 不再叠加；评估失败、低置信或关闭时才回退到基础加成。
- 预算独立于用户聊天额度：全站默认 `1000`/天、每人默认 `60`/天；`0` 表示不限，`appraisal_enabled=false` 是总开关。
- 不新增运行时依赖；模型复用 `ChatDeepSeek`。
- 数据库变更必须自动迁移旧库，不能要求手工重建。
- 在 `backend/` 目录下用项目虚拟环境运行：`.venv/Scripts/python.exe -m pytest`。
- **不要提交或推送**；WordPress 插件改动也只改仓库文件，部署由用户手动完成。

## Review Focus

- 评估超时或模型返回垃圾时，本轮仍按规则正常回复/沉默，不抛异常、不写半截状态。
- 并发私聊同时评估时，预算不能超发；全站名额被占后用户名额失败必须回滚。
- 每日好感上限必须按“实际落账值”累计，并在本地午夜重置，不能按 UTC 日算。
- 高好感时 `tease` 可以转正，但 `boundary` / `betrayal` / `hostility` 不能被当成玩笑；好感越高，背叛/辱骂伤害越大。
- 评估成功但置信度低时，必须回退到基础加成；不能既没有事件账、又没有基础账，造成漏计。
- `policy="silent"`、危机、预算耗尽时不能发起模型调用；正常概率沉默仍然评估。

---

### Task 1: 人设评估参数

**Files:**
- Modify: `backend/app/persona/models.py`
- Test: `backend/tests/test_persona_dynamics.py`

**Interfaces:**
- Consumes: `Dynamics`、`ReplyDynamics`。
- Produces:
  - `APPRAISAL_EVENTS: tuple[str, ...]` = `("care","apology","flirt","shared_interest","smalltalk","neutral","tease","boundary","betrayal","hostility","spam")`
  - `APPRAISAL_CAP_EVENTS: tuple[str, ...]` = `APPRAISAL_EVENTS` 去掉 `tease`（`tease` 用非对称的标量参数）
  - `AppraisalDynamics`（`confidence_min`、`daily_affection_cap`、`tease_affection_low/high`、`tease_valence_low/high`、`tease_negative_until`、`tease_positive_from`、四张事件上限表）
  - `Dynamics.appraisal: AppraisalDynamics`
  - `ReplyDynamics.reply_bias_weight: float = 1.0`

默认值：

```python
event_caps = {
    "apology": 1.2, "care": 1.0, "flirt": 0.8, "shared_interest": 0.6,
    "smalltalk": 0.15, "neutral": 0.0,
    "boundary": -1.0, "betrayal": -3.0, "hostility": -2.0, "spam": -0.3,
}
event_valence_caps = {
    "apology": 0.04, "care": 0.05, "flirt": 0.06, "shared_interest": 0.04,
    "smalltalk": 0.01, "neutral": 0.0,
    "boundary": -0.06, "betrayal": -0.15, "hostility": -0.12, "spam": -0.03,
}
event_security_caps = {
    "apology": 4.0, "care": 2.0, "flirt": 1.0, "shared_interest": 1.0,
    "smalltalk": 0.0, "neutral": 0.0,
    "boundary": -3.0, "betrayal": -8.0, "hostility": -5.0, "spam": -1.0,
}
event_patience_caps = {
    "apology": 3.0, "care": 2.0, "flirt": 0.0, "shared_interest": 0.0,
    "smalltalk": 0.0, "neutral": 0.0,
    "boundary": -4.0, "betrayal": -10.0, "hostility": -6.0, "spam": -2.0,
}
tease_affection_low = -0.8    # 好感 0 附近：玩笑带刺
tease_affection_high = 0.6    # 好感 >= tease_positive_from：玩笑是亲近
tease_valence_low = -0.04
tease_valence_high = 0.03
```

- [ ] **Step 1: 写失败测试**

```python
def test_appraisal_defaults_match_design():
    appraisal = Dynamics().appraisal
    assert appraisal.confidence_min == 0.5
    assert appraisal.daily_affection_cap == 3.0
    assert appraisal.event_caps["care"] == 1.0
    assert appraisal.event_caps["betrayal"] == -3.0
    assert set(appraisal.event_caps) == set(APPRAISAL_CAP_EVENTS)


def test_unknown_appraisal_event_is_rejected():
    caps = AppraisalDynamics().event_caps
    with pytest.raises(Exception):
        AppraisalDynamics(event_caps={**caps, "gift": 5.0})


def test_tease_high_affection_is_positive():
    appraisal = Dynamics().appraisal
    assert appraisal.tease_affection_low < 0 < appraisal.tease_affection_high
    assert appraisal.tease_valence_low < 0 < appraisal.tease_valence_high


def test_reply_bias_weight_defaults_to_one():
    assert Dynamics().reply.reply_bias_weight == 1.0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py -q`
Expected: FAIL，`Dynamics` 没有 `appraisal`，`ReplyDynamics` 没有 `reply_bias_weight`。

- [ ] **Step 3: 实现模型**

`AppraisalDynamics` 用 `extra="forbid"`、`allow_inf_nan=False`；四张表都用默认工厂，并用 validator 要求键集合恰好等于 `APPRAISAL_CAP_EVENTS`（拼错事件名要报错，不能静默丢掉）。取值范围：`confidence_min 0–1`、`daily_affection_cap 0–20`、`tease_positive_from > tease_negative_until`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py -q`
Expected: PASS。

---

### Task 2: `apply_event` 接受原始好感点数

**Files:**
- Modify: `backend/app/relationship/state.py`
- Test: `backend/tests/test_relationship_state.py`

**Interfaces:**
- Consumes: `apply_event()`。
- Produces: `apply_event(..., d_affection_points: float = 0.0)`；非 0 时优先于 `d_affection_units × affection_unit`，仍然乘角色成长倍率、递减曲线和上限。

- [ ] **Step 1: 写失败测试**

```python
def test_raw_affection_points_apply_growth(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="appraisal", d_affection_points=2.0,
    )
    assert after.affection == pytest.approx(2.0 * 0.6)  # 0 分附近成长倍率 0.6


def test_raw_negative_affection_is_not_multiplied_by_growth(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="appraisal", d_affection_points=-2.0,
    )
    assert after.affection == pytest.approx(-2.0)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py -q`
Expected: FAIL，`apply_event()` 不认识 `d_affection_points`。

- [ ] **Step 3: 实现**

在函数签名加 `d_affection_points`；`raw_affection = d_affection_points if d_affection_points else d_affection_units * affection_unit`，后续成长/递减/上限逻辑不变。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py -q`
Expected: PASS。

---

### Task 3: 评估表与决策日志字段

**Files:**
- Modify: `backend/app/memory/db.py`
- Test: `backend/tests/test_memory_db.py`

**Interfaces:**
- Produces:
  - `appraisal_usage(scope TEXT, day TEXT, used INTEGER, PRIMARY KEY(scope, day))`
  - `appraisal_log(...)`
  - `decision_log` 新列：`reply_bias REAL NOT NULL DEFAULT 0`、`appraisal_event TEXT`、`appraisal_confidence REAL NOT NULL DEFAULT 0`

建表 SQL：

```sql
CREATE TABLE IF NOT EXISTS appraisal_usage (
    scope TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, day)
);

CREATE TABLE IF NOT EXISTS appraisal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    event TEXT,
    intensity REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    reply_bias REAL NOT NULL DEFAULT 0,
    reason TEXT,
    raw_output TEXT,
    d_affection REAL NOT NULL DEFAULT 0,
    d_valence REAL NOT NULL DEFAULT 0,
    d_security REAL NOT NULL DEFAULT 0,
    d_patience REAL NOT NULL DEFAULT 0,
    applied_affection REAL NOT NULL DEFAULT 0,
    applied_valence REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    skipped_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appraisal_log_person
    ON appraisal_log(persona_key, person_key, id);
```

- [ ] **Step 1: 写失败测试**

```python
def test_init_schema_creates_appraisal_tables(...):
    ...
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"appraisal_usage", "appraisal_log"} <= tables


def test_init_schema_adds_appraisal_columns_to_legacy_decision_log(...):
    # 先建旧版 decision_log，再 init_schema
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")}
    assert {"reply_bias", "appraisal_event", "appraisal_confidence"} <= columns
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py -q`
Expected: FAIL，新表/新列不存在。

- [ ] **Step 3: 实现建表与迁移**

把两张新表加进 `_SCHEMA`；新增 `_ensure_decision_log_appraisal_columns(conn)`，用 `PRAGMA table_info` 逐列 `ALTER TABLE ... ADD COLUMN`，并在 `init_schema()` 里调用。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py -q`
Expected: PASS。

---

### Task 4: 本地日与原子预算

**Files:**
- Create: `backend/app/relationship/appraisal_budget.py`
- Test: `backend/tests/test_appraisal_budget.py`

**Interfaces:**
- Consumes: `appraisal_usage` 表、`state.parse_iso`。
- Produces:
  - `local_day(now: str | None, timezone_name: str) -> str`，返回 `YYYY-MM-DD`
  - `day_start_iso(now: str | None, timezone_name: str) -> str`，本地午夜对应的 UTC ISO
  - `reserve_budget(conn, *, person_key: str, day: str, global_limit: int, user_limit: int) -> bool`
  - `used_budget(conn, *, person_key: str, day: str) -> tuple[int, int]`

`reserve_budget` 语义：

```text
global 与 person:<person_key> 两个 scope 各加 1；limit=0 表示不限，但仍计数。
任一 scope 已达上限则返回 False，并把本次已加的另一项减回去。
```

SQLite 原子写法：

```sql
INSERT INTO appraisal_usage(scope, day, used) VALUES (?, ?, 1)
ON CONFLICT(scope, day) DO UPDATE SET used = used + 1
WHERE ? = 0 OR used < ?
```

- [ ] **Step 1: 写失败测试**

```python
def test_local_day_uses_configured_timezone():
    assert local_day("2026-11-09T16:30:00+00:00", "Asia/Shanghai") == "2026-11-10"


def test_day_start_is_local_midnight_in_utc():
    assert day_start_iso("2026-11-09T16:30:00+00:00", "Asia/Shanghai") == \
        "2026-11-09T16:00:00+00:00"


def test_reserve_respects_global_limit(conn):
    day = "2026-11-09"
    assert reserve_budget(conn, person_key="wp:1", day=day, global_limit=1, user_limit=10)
    assert not reserve_budget(conn, person_key="wp:2", day=day, global_limit=1, user_limit=10)


def test_reserve_rolls_back_global_when_user_limit_hits(conn):
    day = "2026-11-09"
    assert reserve_budget(conn, person_key="wp:1", day=day, global_limit=10, user_limit=1)
    assert not reserve_budget(conn, person_key="wp:1", day=day, global_limit=10, user_limit=1)
    assert used_budget(conn, person_key="wp:1", day=day) == (1, 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraisal_budget.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现**

`local_day` / `day_start_iso` 用 `state._zone` 的同一套回退逻辑：非法时区回退 UTC。`reserve_budget` 先全局后个人；个人失败时对全局执行 `used = used - 1` 再返回 False。所有写操作一次 `commit()`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraisal_budget.py -q`
Expected: PASS。

---

### Task 5: 评估输出解析

**Files:**
- Create: `backend/app/relationship/appraisal.py`
- Test: `backend/tests/test_appraisal_parse.py`

**Interfaces:**
- Consumes: `APPRAISAL_EVENTS`。
- Produces:
  - `@dataclass AppraisalResult(event, intensity, confidence, reply_bias, reason)`
  - `parse_appraisal(raw: str) -> AppraisalResult | None`
  - `build_appraisal_prompt(*, persona_name: str, user_text: str, recent: list[str], state_context: str, local_time: str = "", recent_events: list[str] | None = None) -> str`

解析规则：

```text
必须是 JSON 对象；event 必须在 APPRAISAL_EVENTS 中。
intensity/confidence 夹到 0–1，reply_bias 夹到 -1–1；缺失数值按 0 处理。
reason 截断到 80 字符（spec 4.7）。不是对象、JSON 坏了、event 未知 → 返回 None。
```

- [ ] **Step 1: 写失败测试**

```python
def test_parse_valid_appraisal():
    result = parse_appraisal('{"event":"care","intensity":0.8,"confidence":0.9,"reply_bias":0.3,"reason":"关心"}')
    assert result == AppraisalResult("care", 0.8, 0.9, 0.3, "关心")


def test_parse_strips_code_fence():
    raw = '```json\n{"event":"apology","intensity":1,"confidence":1,"reply_bias":0}\n```'
    assert parse_appraisal(raw).event == "apology"


def test_parse_rejects_unknown_event():
    assert parse_appraisal('{"event":"gift","intensity":1,"confidence":1}') is None


def test_parse_clamps_ranges():
    result = parse_appraisal('{"event":"tease","intensity":9,"confidence":-1,"reply_bias":5}')
    assert (result.intensity, result.confidence, result.reply_bias) == (1.0, 0.0, 1.0)


def test_parse_truncates_reason_to_80_chars():
    raw = '{"event":"care","reason":"' + "长" * 200 + '"}'
    assert len(parse_appraisal(raw).reason) == 80
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraisal_parse.py -q`
Expected: FAIL，模块/函数不存在。

- [ ] **Step 3: 实现解析与提示词**

提示词只给事件枚举和字段说明，明确“只输出一个 JSON 对象”“不要写绝对值/概率/回不回”，并附上定性状态（直接复用 `render_state_context(signals.state)` 的输出）、`local_time`（本地 `YYYY-MM-DD HH:MM`）、最近 3 条关系事件理由和最近 4 条对话。`_strip_fence` 与 `app/memory/extract.py` 的私有实现重复时，抽到 `app/llm_json.py` 的 `strip_fence(raw)`，两边共用。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraisal_parse.py tests/test_memory_extract.py -q`
Expected: PASS。

---

### Task 6: 事件映射与落账

**Files:**
- Modify: `backend/app/relationship/appraisal.py`
- Test: `backend/tests/test_appraisal_mapping.py`

**Interfaces:**
- Consumes: `AppraisalResult`、`Dynamics.appraisal`、`apply_event()`、`day_start_iso()`。
- Produces:
  - `@dataclass AppraisalDelta(d_affection, d_valence, d_security, d_patience, applied_affection, applied_valence, skipped_reason)`
  - `map_appraisal(*, result, affection, dynamics, positive_used, negative_used) -> AppraisalDelta`
  - `apply_appraisal(conn, persona_key, person_key, *, role, dynamics, result, now=None, timezone_name=DEFAULT_TIMEZONE) -> AppraisalDelta`
  - `record_appraisal_log(conn, *, persona_key, person_key, message_id, result: AppraisalResult | None, delta: AppraisalDelta | None = None, latency_ms: int = 0, status: str, raw_output: str = "", skipped_reason: str = "", channel_id: str = "qq_private") -> None`

公式（按顺序）：

```text
confidence < confidence_min → 全部 0，skipped_reason="low_confidence"

tease:
  tolerance = clamp((affection - tease_negative_until) /
                    (tease_positive_from - tease_negative_until), 0, 1)
  d_affection = lerp(tease_affection_low, tease_affection_high, tolerance)
                * intensity * confidence
  d_valence   = lerp(tease_valence_low, tease_valence_high, tolerance)
                * intensity * confidence
  d_security = d_patience = 0

betrayal / hostility:
  severity = 0.5 + 0.5 * clamp(affection / 100, 0, 1)
  d_affection = event_caps[event] * intensity * confidence * severity

其他事件:
  d_affection = event_caps[event] * intensity * confidence
  d_valence   = event_valence_caps[event] * intensity * confidence
  d_security  = event_security_caps[event] * intensity * confidence
  d_patience  = event_patience_caps[event] * intensity * confidence

正向好感剩余 = max(0, daily_affection_cap - positive_used)
负向好感剩余 = max(0, daily_affection_cap - negative_used)
d_affection 按方向夹到剩余额度。
```

`apply_appraisal` 先用 `day_start_iso()` 查当天 `relationship_events(kind="appraisal")` 的正/负实际值，再调用 `apply_event(kind="appraisal", d_affection_points=...)`；返回实际 `state.affection - before.affection` 作为 `applied_affection`。零增量时不写 `relationship_events`。

- [ ] **Step 1: 写失败测试**

```python
def test_care_adds_affection(conn, dynamics):
    delta = apply_appraisal(conn, "merlin", "wp:1", role="normal", dynamics=dynamics,
                            result=AppraisalResult("care", 1.0, 1.0, 0.2, "关心"))
    assert delta.applied_affection == pytest.approx(1.0 * 0.6)


def test_confidence_below_threshold_skips_state(conn, dynamics):
    delta = apply_appraisal(conn, ..., result=AppraisalResult("care", 1.0, 0.4, 0.9, "不确定"))
    assert delta.skipped_reason == "low_confidence"
    assert delta.applied_affection == 0


def tease():
    return AppraisalResult("tease", 1.0, 1.0, 0.0, "")


def test_tease_is_negative_at_low_affection(conn, dynamics):
    delta = map_appraisal(result=tease(), affection=0, dynamics=dynamics,
                          positive_used=0, negative_used=0)
    assert delta.d_affection < 0
    assert delta.d_valence < 0


def test_tease_turns_positive_at_high_affection(conn, dynamics):
    delta = map_appraisal(result=tease(), affection=80, dynamics=dynamics,
                          positive_used=0, negative_used=0)
    assert delta.d_affection > 0
    assert delta.d_valence > 0


def test_tease_at_max_affection_does_not_hurt(conn, dynamics):
    # 好感已在角色上限附近：正向点数会被成长曲线吃掉，但不能变成负数
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=dynamics)
    save_state(conn, replace(state, affection=80))
    applied = apply_appraisal(conn, "merlin", "wp:1", role="normal",
                              dynamics=dynamics, result=tease())
    assert applied.applied_affection >= 0


def test_betrayal_hurts_more_at_high_affection(conn, dynamics):
    low = ...   # affection 0   → severity 0.5
    high = ...  # affection 80  → severity 0.9
    assert abs(high.applied_affection) > abs(low.applied_affection)


def test_daily_positive_cap_limits_total(conn, dynamics):
    delta = map_appraisal(
        result=AppraisalResult("care", 1.0, 1.0, 0.0, ""),
        affection=0, dynamics=dynamics, positive_used=2.8, negative_used=0,
    )
    assert delta.d_affection == pytest.approx(0.2)


def test_boundary_reduces_security_and_patience(conn, dynamics):
    ...
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraisal_mapping.py -q`
Expected: FAIL，函数不存在。

- [ ] **Step 3: 实现映射与落账**

严格按上面顺序实现；`tease_positive_from == tease_negative_until` 时用 `1.0 if affection >= from else 0.0` 避免除零。`record_appraisal_log` 写完整一行（原始输出、映射值、实际落账、耗时、状态、回退原因 `skipped_reason`）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraisal_mapping.py -q`
Expected: PASS。

---

### Task 7: 异步评估器

**Files:**
- Modify: `backend/app/agent/model.py`
- Modify: `backend/app/relationship/appraisal.py`
- Test: `backend/tests/test_appraiser.py`

**Interfaces:**
- Consumes: `build_chat_model()`、`build_appraisal_prompt()`、`parse_appraisal()`。
- Produces:
  - `build_chat_model(model_name=None, *, temperature=0.7, max_tokens=800)`
  - `@dataclass AppraisalCall(result: AppraisalResult | None, status: str, raw_output: str, latency_ms: int)`
  - `class Appraiser: def __init__(self, model=None); async def appraise(*, persona_name, user_text, recent, state_context, timeout, local_time="", recent_events=()) -> AppraisalCall`

`status` 取值：`ok` / `timeout` / `error` / `parse_error`。评估模型固定 `temperature=0.0`、`max_tokens=200`，关闭思考。新测试文件顶部放一份与 `test_qq_private_chat.py` 相同的 `async_test` 包装器（项目没有 pytest-asyncio）。

- [ ] **Step 1: 写失败测试**

```python
@async_test
async def test_appraiser_returns_result():
    class FakeModel:
        async def ainvoke(self, prompt):
            return AIMessage(content='{"event":"care","intensity":1,"confidence":1,"reply_bias":0.5}')

    call = await Appraiser(FakeModel()).appraise(
        persona_name="梅林", user_text="你辛苦了", recent=[], state_context="平静", timeout=1.0,
    )
    assert call.status == "ok"
    assert call.result.event == "care"


@async_test
async def test_appraiser_times_out():
    class SlowModel:
        async def ainvoke(self, prompt):
            await asyncio.sleep(1)

    call = await Appraiser(SlowModel()).appraise(..., timeout=0.01)
    assert call.status == "timeout"
    assert call.result is None


@async_test
async def test_appraiser_reports_parse_error():
    # 返回 "我不知道" → status == "parse_error"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraiser.py -q`
Expected: FAIL，`Appraiser` 不存在。

- [ ] **Step 3: 实现**

`build_chat_model` 把 `temperature` / `max_tokens` 加入缓存键（`lru_cache` 支持关键字参数）。`Appraiser.appraise` 用 `asyncio.wait_for`；捕获 `TimeoutError` / `Exception` 后返回对应 status，不向上抛。`_text_of` 与 `extract.py` 共用 `app/llm_json.py` 里的 `text_of(result)`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_appraiser.py tests/test_memory_extract.py -q`
Expected: PASS。

---

### Task 8: 概率与 `reply_bias` 的 logit 合成

**Files:**
- Modify: `backend/app/relationship/decision.py`
- Test: `backend/tests/test_reply_decision.py`

**Interfaces:**
- Consumes: `Dynamics.reply.reply_bias_weight`。
- Produces:
  - `blend_willingness(rule: float, reply_bias: float, confidence: float, weight: float) -> float`
  - `decide(..., reply_bias=0.0, bias_confidence=0.0, appraisal_event=None)`
  - `Decision` 新字段（都带默认值，避免破坏现有构造）：`rule_willingness: float = 0.0`、`reply_bias: float = 0.0`、`appraisal_event: str | None = None`、`appraisal_confidence: float = 0.0`
  - `record_decision()` 写入 `rule_score`、`reply_bias`、`appraisal_event`、`appraisal_confidence`；适配器调用时传 `rule_score=decision.rule_willingness`

公式：

```text
rule = clamp(rule, 0.001, 0.999)   # 避免 log(0)
z = log(rule / (1 - rule)) + weight * confidence * reply_bias
willingness = clamp(sigmoid(z), 0.05, 1.0)
```

`weight` 取 `dynamics.reply.reply_bias_weight`。

- [ ] **Step 1: 写失败测试**

```python
def test_positive_bias_raises_willingness(dynamics):
    base = blend_willingness(0.5, 0.8, 1.0, dynamics.reply.reply_bias_weight)
    assert base > 0.5


def test_negative_bias_lowers_willingness(dynamics):
    assert blend_willingness(0.5, -0.8, 1.0, 1.0) < 0.5


def test_bias_is_scaled_by_confidence(dynamics):
    full = blend_willingness(0.5, 0.8, 1.0, 1.0)
    half = blend_willingness(0.5, 0.8, 0.5, 1.0)
    assert 0.5 < half < full


def test_decision_log_records_appraisal_fields(conn):
    ...
    row = conn.execute("SELECT rule_score, reply_bias, appraisal_event, appraisal_confidence FROM decision_log").fetchone()
    assert row["appraisal_event"] == "care"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -q`
Expected: FAIL，新函数/字段不存在。

- [ ] **Step 3: 实现**

`decide()` 先算 `rule_willingness = willingness_for(...)`，再用 `blend_willingness` 得到最终值；`force_reply` 路径同样记录最终 willingness，但 `reply=True`。`record_decision` 的 INSERT 增加三列；已有 `rule_score` 列承载 spec 4.7 所说的 `p_rule`（合成前的规则意愿），`willingness` 列存合成后的最终值。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -q`
Expected: PASS。

---

### Task 9: 运行时预算与后台接口

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/runtime_settings.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_runtime_settings.py`、`backend/tests/test_admin_api.py`

**Interfaces:**
- Produces（`Settings` 与 `RuntimeSettings` 同名字段）：
  - `appraisal_enabled: bool = True`
  - `appraisal_timeout_seconds: float = 1.2`
  - `appraisal_global_daily_budget: int = 1000`
  - `appraisal_per_user_daily_budget: int = 60`
- 语义：两个预算 `0` 表示不限；`appraisal_enabled=false` 是总开关；超时范围 `0.2–5.0`。

- [ ] **Step 1: 写失败测试**

```python
def test_runtime_settings_appraisal_defaults(...):
    runtime = get_runtime_store().load()
    assert runtime.appraisal_enabled is True
    assert runtime.appraisal_timeout_seconds == 1.2
    assert runtime.appraisal_global_daily_budget == 1000
    assert runtime.appraisal_per_user_daily_budget == 60


def test_runtime_settings_rejects_bad_appraisal_timeout(...):
    with pytest.raises(ValueError):
        store.update({"appraisal_timeout_seconds": 30})


def test_admin_settings_exposes_appraisal_fields(...):
    data = client.get("/api/admin/settings", ...).json()
    assert "appraisal_global_daily_budget" in data["settings"]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runtime_settings.py tests/test_admin_api.py -q`
Expected: FAIL，字段不存在。

- [ ] **Step 3: 实现**

在 `Settings` 加默认值；`RuntimeSettings` / `_defaults_from_settings` / `_INT_RANGES` / `_BOOL_FIELDS` 加对应项；`appraisal_timeout_seconds` 用独立 `_FLOAT_RANGES` 校验；`RuntimeSettingsUpdate` 加四个可选字段（`ge/le` 与后端一致）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runtime_settings.py tests/test_admin_api.py -q`
Expected: PASS。

---

### Task 10: 适配器串起评估与决策

**Files:**
- Modify: `backend/app/channels/qq/adapter.py`
- Modify: `backend/app/relationship/turn.py`
- Modify: `backend/app/api/qq.py`
- Test: `backend/tests/test_qq_private_chat.py`、`backend/tests/test_qq_relationship_flow.py`、`backend/tests/test_qq_adapter.py`

**Interfaces:**
- Consumes: `Appraiser`、`reserve_budget`、`local_day`、`apply_appraisal`、`record_appraisal_log`、`get_runtime_store()`。
- Produces:
  - `QQAdapter.__init__(..., appraiser: Any | None = None)`，保存 `self._appraiser`；`None` 表示不评估
  - `_load_private_signals(self, incoming, binding, persona) -> tuple[str, str, TurnSignals] | None`
  - `async def _appraise_private(self, incoming, texts, *, person_key, signals, persona) -> AppraisalCall | None`
  - `_decide_and_record(self, incoming, texts, *, binding, persona, person_key, role, signals, call) -> tuple[Decision, RelationshipState]`
  - `_run_chat_turn(..., baseline_affection: bool = True)`
  - `_record_reply_safe(..., baseline: bool = True)`
  - `record_reply(..., baseline: bool = True)`：`baseline=False` 时 `d_affection_units` / `d_valence` 传 0，只保留疲劳和 `touch_reply`
  - `get_qq_adapter()` 改为 `QQAdapter(settings, build_onebot_client(settings), appraiser=Appraiser())`

适配器流程：

```text
1. _load_private_signals(): relationship_subject + load_signals（失败则 fail-open 直接回复，不写关系）。
2. 危机词 → 跳过评估，decision 强制回复。
3. policy="silent" 且非危机 → 跳过评估（用户显式关闭私聊回复）。
4. runtime.appraisal_enabled=false，或 self._appraiser is None → 跳过评估。
5. reserve_budget()：失败记 appraisal_log(status="over_budget")，不调模型。
6. Appraiser.appraise()：返回 AppraisalCall（ok/timeout/error/parse_error）。
7. _decide_and_record():
   decide(..., reply_bias=..., bias_confidence=..., appraisal_event=...)；
   record_decision(rule_score=decision.rule_willingness)；
   沉默则 record_silence()；
   置信评估（status="ok" 且 confidence >= confidence_min）→
       apply_appraisal() + record_appraisal_log()。
8. appraisal_ok = call 存在 且 status="ok" 且 confidence >= confidence_min；
   call 超时/解析失败/低置信/被跳过时 appraisal_ok=False。
9. _run_chat_turn(..., baseline_affection=not appraisal_ok)；
   回复成功时 _record_reply_safe(..., baseline=baseline_affection)。
```

`_appraise_private` 的 `recent` 取 `[m.content for m in self._history.get(key, [])[-4:]]`，最后拼上本轮 `"\n".join(texts)` 作为 `user_text`；`local_time` 用 `parse_iso(now).astimezone(_zone(quota_timezone))` 格式化成 `YYYY-MM-DD HH:MM`；`recent_events` 从 `appraisal_log` 取该 person 最近 3 条非空 `reason`。默认 `appraiser=None` 是为了让现有测试和离线运行不触发真实模型调用；生产入口必须显式注入 `Appraiser()`。

- [ ] **Step 1: 写失败测试**

```python
class FakeAppraiser:
    def __init__(self, call):
        self.call = call
        self.calls = 0

    async def appraise(self, **kwargs):
        self.calls += 1
        return self.call


@async_test
async def test_private_round_appraises_before_decision(tmp_path):
    fake = FakeAppraiser(AppraisalCall(parse_appraisal('{"event":"care","intensity":1,"confidence":1,"reply_bias":0.2}'), "ok", "", 5))
    # probabilistic 策略 + fake appraiser；处理后
    assert fake.calls == 1
    row = conn.execute("SELECT appraisal_event FROM decision_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["appraisal_event"] == "care"


@async_test
async def test_appraisal_timeout_falls_back_to_rules(tmp_path):
    # FakeAppraiser(AppraisalCall(None, "timeout", "", 1200))
    # 仍然正常回复；本轮好感只按基础加成变化


@async_test
async def test_appraisal_skipped_when_disabled(tmp_path):
    # runtime.appraisal_enabled=False → fake.calls == 0


@async_test
async def test_appraisal_skipped_when_policy_silent(tmp_path):
    # qq_private_policy="silent" → fake.calls == 0，回 "silent"


@async_test
async def test_silent_round_still_applies_appraisal(tmp_path):
    # 让骰子落空；relationship_events 仍有 kind="appraisal"


@async_test
async def test_successful_appraisal_replaces_chat_affection(tmp_path):
    # care 评估成功；回复后好感只涨事件值，不再叠加 chat_affection_units


@async_test
async def test_low_confidence_keeps_baseline_affection(tmp_path):
    # confidence=0.4 → 没有 appraisal 事件，但基础聊天加成仍然生效


@async_test
async def test_decision_log_stores_appraisal_fields(tmp_path):
    ...


def test_production_adapter_injects_appraiser(monkeypatch):
    ...
    assert get_qq_adapter()._appraiser is not None
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_private_chat.py tests/test_qq_relationship_flow.py -q`
Expected: FAIL，评估没有接入。

- [ ] **Step 3: 实现**

把现有 `_decide_private` 拆成 `_load_private_signals`（同步，放 `asyncio.to_thread`）、`_appraise_private`（异步）、`_decide_and_record`（同步，放 `asyncio.to_thread`）。`_appraise_private` 的最近上下文取 `self._history.get(key, [])[-4:]` 的文本。任何异常仍按原逻辑 fail-open 为直接回复。测试里的 `make_adapter` 增加 `appraiser=` 参数。

`appraisal_ok` 必须满足三个条件：`call is not None`、`call.status == "ok"`、`call.result.confidence >= dynamics.appraisal.confidence_min`。只有它为真时才抑制基础加成；低置信时 `apply_appraisal` 返回 `low_confidence`，基础加成照常。

`_decide_and_record` 返回决策时的 `signals.state` 快照，不返回落账后的状态：这一轮的语气用掷骰子前的状态，评估增量只影响下一轮。

`app/api/qq.py` 的 `get_qq_adapter()` 注入 `Appraiser()`；测试用 monkeypatch 替换 `build_onebot_client` 和 `Appraiser`，避免真实网络和模型调用。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_private_chat.py tests/test_qq_relationship_flow.py tests/test_reply_decision.py -q`
Expected: PASS。

---

### Task 11: WordPress 后台预算字段

**Files:**
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`
- Test: 手动检查（`php -l` + 后台保存一次）

**Interfaces:**
- Consumes: 后端 `/admin/settings` 的新字段。
- Produces: 设置页“关系评估”分区：
  - `appraisal_enabled`（复选框）
  - `appraisal_timeout_seconds`（数字，0.2–5，步长 0.1）
  - `appraisal_global_daily_budget`（数字，0–1000000，0=不限）
  - `appraisal_per_user_daily_budget`（数字，0–1000，0=不限）

- [ ] **Step 1: 改保存逻辑**

在 `save_backend_settings()` 的 `$rules` 加两个整数项；布尔项追加 `$patch['appraisal_enabled'] = ! empty( $_POST['appraisal_enabled'] );`；超时单独用浮点正则和范围校验后写入 `$patch['appraisal_timeout_seconds']`。

- [ ] **Step 2: 改表单**

在“聊天额度与运行参数”表格末尾加一个跨列标题“关系评估”，四个输入读取 `$settings['appraisal_*']`，描述里写清“0=不限”“关闭后不调用模型、关系只按规则变化”。

- [ ] **Step 3: 校验语法**

Run: `php -l wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`
Expected: `No syntax errors detected`。本机没有 PHP 时改为在服务器上执行，并记下结果。

- [ ] **Step 4: 手动核对**

后台改一次超时和预算并保存；确认页面回显新值、`runtime_settings.json` 出现四个键、没有保存失败提示。不要打印服务器路径或密钥。

---

### Task 12: 文档与全量回归

**Files:**
- Modify: `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`
- Modify: `docs/architecture.md`（如涉及运营参数表）
- Modify: `C:\Users\Adam\.claude\projects\e--study-code-LangChain-WordPressAssistant\memory\kb-vector-retrieval-state.md`

- [ ] **Step 1: 更新设计文档**

10.1 把 `tease_*` / `appraisal_daily_affection` / `appraisal_confidence_min` 从 `relationship` 移到新的 `dynamics.appraisal`；补四张事件上限表和 `reply_bias_weight`；注明预算与超时改由运行时设置管理，`0=不限`。第 9 节把 3c 标为本地完成。

- [ ] **Step 2: 更新架构文档**

在运营参数表补四个 `appraisal_*` 字段和 `0=不限` 语义。

- [ ] **Step 3: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿，基线为 `383 passed, 1 skipped` 加新增用例。

- [ ] **Step 4: 记录进度**

更新记忆文件：3c 本地完成、未提交、未部署；下一步 4b（群策略、待续话题、主动调度）。
