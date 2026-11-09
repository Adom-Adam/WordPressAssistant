# 3c.1 冲突弧与阶段注入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 spec 4.4 的冲突弧做成“事件驱动 + 最短停留 + 迟滞阈值”的状态机，并把阶段与疤痕定性注入生成提示词，让她的回复概率、语气和修复速度随冲突历史变化。

**Architecture:** 新增 `app/relationship/conflict.py` 承载纯转移函数和窗口计数；`apply_appraisal()` 在事件落账后推进阶段，`load_signals()` 在每次读状态时处理时间型恶化与疤痕淡化；`render_state_context()` 增加阶段行；`reply_probability()` 乘阶段系数；`decision_log` 记录阶段。阶段变化只影响下一轮数值，但当前回合的提示词可以立刻使用新阶段（否则她会在被伤害的那一句里还像没事人）。

**Tech Stack:** Python 3.11+、Pydantic v2、SQLite、pytest。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（4.4、4.7、5.3、9、10.1）

**Scope note:** 本计划只做状态机、概率、提示词和审计。`memories` 表目前没有 `persona_key`，直接写“伤疤记忆”会让同一用户的不同人设互相看见，违反 persona 隔离；因此 `normal_with_scar` 的伤疤先存在 persona 隔离的 `relationship_state` 和 `appraisal_log` 里，记忆接入另开任务。

## Global Constraints

- 状态：`normal` / `hurt` / `cold` / `withdrawing` / `repairing` / `normal_with_scar`；只允许相邻或事件明确允许的转移，不允许 `normal` 直接跳到 `cold`。
- 转移**确定性**，不使用随机数；活人感来自历史、好感、安全感和事件强度的差异，不来自掷骰子。
- 时间只做三件事：最短停留防跳变、无修复时恶化、疤痕淡化；时间本身不把 `hurt` / `cold` / `withdrawing` 直接变成 “没事了”。
- `withdrawing` 无冲突 14 天后也必须软化成 `normal_with_scar`，不允许永久冷战。
- `normal_with_scar` 可以淡回 `normal`，但 `relationship_events` / `appraisal_log` 里的记录不删除。
- 危机、命令、绑定引导、额度提示永远绕过阶段概率；`withdrawing` 也不能压掉危机回复。
- 提示词只给中文定性描述，不出现阶段英文名或裸数值。
- 时间型转移是**惰性**的：下一次 `load_signals()` 或主动调度读状态时才结算，不新增后台定时任务。
- 数据库变更必须自动迁移旧库。
- 在 `backend/` 目录下用项目虚拟环境运行：`.venv/Scripts/python.exe -m pytest`。
- **不要提交或推送**。

## Review Focus

- `normal` 收到一次低置信或低强度的负面事件时不能直接进冲突；阈值必须同时看 `confidence` 和 `intensity`。
- 最短停留必须真的挡住“5 秒内从受伤到原谅”；但 30 分钟后有真诚道歉就应允许进入 `repairing`。
- `repairing` 期间再次背叛必须升级为 `withdrawing`，不能悄悄回到 `normal`。
- 阶段乘数叠加 patience/security 下限后，危机、命令、额度提示仍然强回。
- 提示词不能泄露 `hurt` / `cold` 等英文阶段名或数值；`normal_with_scar` 不能每条消息都翻旧账。

---

### Task 1: 人设冲突参数

**Files:**
- Modify: `backend/app/persona/models.py`
- Test: `backend/tests/test_persona_dynamics.py`

**Interfaces:**
- Consumes: `Dynamics`。
- Produces:
  - `CONFLICT_STAGES: tuple[str, ...]` = `("normal","hurt","cold","withdrawing","repairing","normal_with_scar")`
  - `ConflictDynamics`（阶段乘数、最短停留、阈值、疤痕参数）
  - `Dynamics.conflict: ConflictDynamics`

默认值：

```python
stage_multipliers = {
    "normal": 1.0, "normal_with_scar": 0.98, "hurt": 0.85,
    "cold": 0.70, "withdrawing": 0.50, "repairing": 0.90,
}
min_dwell_minutes = {
    "hurt": 30, "cold": 360, "withdrawing": 1440,
    "repairing": 1440, "normal_with_scar": 0,
}
severe_confidence_min = 0.6
severe_intensity_min = 0.5
boundary_count_1h = 3
boundary_count_24h = 5
hurt_to_cold_hours = 24.0
hurt_to_cold_low_patience_hours = 12.0
cold_low_patience = 50.0
cold_to_withdrawing_hours = 48.0
withdraw_low_security = 40.0
withdrawing_hours_to_scar = 336.0       # 14 天
repairing_to_cold_hours = 72.0
repair_confidence_min = 0.5
repair_intensity_min = 0.5
withdraw_apology_intensity_min = 0.7
repair_security_normal = 60.0
repair_security_from_cold = 40.0
repair_points_high_affection = 1
repair_points_mid_affection = 2
repair_points_low_affection = 3
repair_high_affection = 60.0
repair_low_affection = 0.0
scar_on_severe = 0.3
scar_on_relapse = 0.2
scar_halflife_days = 14.0
scar_fade_below = 0.1
scar_fade_min_days = 7.0
scar_damage_bonus = 1.0
```

- [ ] **Step 1: 写失败测试**

```python
def test_conflict_defaults_match_design():
    conflict = Dynamics().conflict
    assert conflict.stage_multipliers["withdrawing"] == 0.5
    assert conflict.min_dwell_minutes["hurt"] == 30
    assert conflict.withdrawing_hours_to_scar == 336.0
    assert set(conflict.stage_multipliers) == set(CONFLICT_STAGES)


def test_unknown_stage_is_rejected():
    multipliers = Dynamics().conflict.stage_multipliers
    with pytest.raises(Exception):
        ConflictDynamics(stage_multipliers={**multipliers, "angry": 0.1})
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py -q`
Expected: FAIL，`Dynamics` 没有 `conflict`。

- [ ] **Step 3: 实现模型**

`ConflictDynamics` 用 `extra="forbid"`、`allow_inf_nan=False`。`stage_multipliers` 的键必须恰好等于 `CONFLICT_STAGES`，值范围 `0.0–1.5`；`min_dwell_minutes` 的键必须是 `CONFLICT_STAGES` 去掉 `normal`，值 `0–10080`。`withdrawing_hours_to_scar`、`scar_halflife_days` 必须为正。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py -q`
Expected: PASS。

---

### Task 2: 状态与决策日志字段

**Files:**
- Modify: `backend/app/memory/db.py`
- Modify: `backend/app/relationship/state.py`
- Modify: `backend/app/relationship/decision.py`
- Test: `backend/tests/test_memory_db.py`、`backend/tests/test_relationship_state.py`

**Interfaces:**
- Produces:
  - `relationship_state` 新列：`conflict_stage TEXT NOT NULL DEFAULT 'normal'`、`stage_since TEXT`、`scar_level REAL NOT NULL DEFAULT 0`
  - `RelationshipState` 新字段：`conflict_stage: str = "normal"`、`stage_since: str | None = None`、`scar_level: float = 0.0`
  - `decision_log` 新列：`conflict_stage TEXT NOT NULL DEFAULT 'normal'`
  - `Decision.conflict_stage: str = "normal"`

- [ ] **Step 1: 写失败测试**

```python
def test_init_schema_adds_conflict_columns(...):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(relationship_state)")}
    assert {"conflict_stage", "stage_since", "scar_level"} <= columns
    decision_columns = {row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")}
    assert "conflict_stage" in decision_columns


def test_new_state_starts_normal(conn):
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=dynamics())
    assert state.conflict_stage == "normal"
    assert state.scar_level == 0.0
    assert state.stage_since is not None
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py tests/test_relationship_state.py -q`
Expected: FAIL，字段不存在。

- [ ] **Step 3: 实现建表、迁移与读写**

在 `_SCHEMA` 的 `relationship_state` 里加三列、`decision_log` 里加一列；新增 `_ensure_relationship_state_conflict_columns(conn)` 和 `_ensure_decision_log_stage_column(conn)`，用 `PRAGMA table_info` 判断后 `ALTER TABLE ... ADD COLUMN`，在 `init_schema()` 调用。`state_from_row`、`save_state`、`get_or_create_state` 带上三个字段；`get_or_create_state` 初始化 `stage_since=stamp`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py tests/test_relationship_state.py -q`
Expected: PASS。

---

### Task 3: 阶段 helpers

**Files:**
- Create: `backend/app/relationship/conflict.py`
- Test: `backend/tests/test_conflict_arc.py`

**Interfaces:**
- Produces:
  - `stage_multiplier(stage: str, dynamics: Dynamics) -> float`
  - `repair_points_needed(affection: float, dynamics: Dynamics) -> int`
  - `positive_appraisal_count(conn, persona_key, person_key, *, since: str) -> int`
  - `boundary_count(conn, persona_key, person_key, *, since: str) -> int`
  - `stage_age_minutes(state: RelationshipState, now: str) -> float`

窗口计数查询 `appraisal_log`，只统计 `status='ok'` 且 `confidence >= dynamics.appraisal.confidence_min` 的行：

```python
POSITIVE_EVENTS = ("apology", "care", "flirt", "shared_interest")
```

- [ ] **Step 1: 写失败测试**

```python
def test_stage_multiplier_reads_persona_config(dynamics):
    assert stage_multiplier("withdrawing", dynamics) == 0.5
    assert stage_multiplier("unknown", dynamics) == 1.0


def test_repair_points_depend_on_affection(dynamics):
    assert repair_points_needed(80, dynamics) == 1
    assert repair_points_needed(30, dynamics) == 2
    assert repair_points_needed(-10, dynamics) == 3


def test_positive_count_excludes_low_confidence(conn, dynamics):
    insert_appraisal(conn, "care", confidence=0.9, created_at=PREVIOUS)
    insert_appraisal(conn, "care", confidence=0.3, created_at=PREVIOUS)
    assert positive_appraisal_count(
        conn, "merlin", "wp:1", since=PREVIOUS
    ) == 1
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conflict_arc.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现**

`stage_multiplier` 对未知阶段回退 1.0。`repair_points_needed` 按 `affection >= high` / `>= low` 取 1/2/3。两个计数函数都限定 `persona_key`、`person_key`、`status='ok'`、`created_at >= since`，并额外用 `confidence >= dynamics.appraisal.confidence_min` 过滤。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conflict_arc.py -q`
Expected: PASS。

---

### Task 4: 事件驱动的转移

**Files:**
- Modify: `backend/app/relationship/conflict.py`
- Modify: `backend/app/relationship/appraisal.py`
- Test: `backend/tests/test_conflict_arc.py`、`backend/tests/test_appraisal_mapping.py`

**Interfaces:**
- Produces:
  - `advance_by_event(conn, state: RelationshipState, *, result: AppraisalResult, dynamics: Dynamics, now: str | None = None) -> RelationshipState`
  - `AppraisalDelta` 新字段：`stage_after: str = "normal"`、`stage_changed: bool = False`
  - `map_appraisal(..., scar_level: float = 0.0)`

转移规则：

```text
normal / normal_with_scar:
  betrayal|hostility 且 confidence >= severe_confidence_min 且 intensity >= severe_intensity_min
      → hurt；scar_level += scar_on_severe
  boundary 且（1h 内 >= boundary_count_1h 或 24h 内 >= boundary_count_24h）
      → hurt

hurt:
  上述严重事件，或 boundary 窗口再次命中 → cold
  apology（confidence/intensity >= repair_*）且已满最短停留 → repairing
  或 since stage_since 的正向计数 >= repair_points_needed 且已满最短停留 → repairing

cold:
  严重事件 → withdrawing
  apology 且已满最短停留，或正向计数 >= repair_points_needed
      且 security >= repair_security_from_cold、已满最短停留 → repairing

withdrawing:
  严重事件 → 留在 withdrawing，scar_level += scar_on_relapse
  apology 且 intensity >= withdraw_apology_intensity_min，
      且 24h 内至少有 1 个其他正向事件，且已满最短停留 → repairing

repairing:
  严重事件 → withdrawing；scar_level += scar_on_relapse
  stage_age >= 24h 且正向计数 >= repair_points_needed
      且 security >= repair_security_normal → normal_with_scar

spam 永不升级冲突；normal_with_scar 再次严重事件时按 normal 的严重事件处理。
任何阶段 scar_level > 0 时，负面事件的 `d_affection` 额外乘
`1 + scar_level * scar_damage_bonus`。
```

- [ ] **Step 1: 写失败测试**

```python
def test_severe_betrayal_enters_hurt(conn, dynamics):
    state = make_state()
    after = advance_by_event(conn, state, result=appraisal("betrayal", 0.9, 0.9),
                             dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "hurt"
    assert after.scar_level == pytest.approx(0.3)


def test_low_intensity_betrayal_does_not_enter_conflict(conn, dynamics):
    state = make_state()
    after = advance_by_event(conn, state, result=appraisal("betrayal", 0.2, 0.9),
                             dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "normal"


def test_boundary_threshold_enters_hurt(conn, dynamics):
    for offset in (50, 40):
        insert_appraisal(conn, "boundary", created_at=minutes_ago(offset))
    after = advance_by_event(conn, make_state(), result=appraisal("boundary", 0.8, 0.8),
                             dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "hurt"


def test_hurt_apology_enters_repairing_after_dwell(conn, dynamics):
    state = make_state(conflict_stage="hurt", stage_since=minutes_ago(31))
    after = advance_by_event(conn, state, result=appraisal("apology", 0.9, 0.9),
                             dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "repairing"


def test_hurt_apology_before_dwell_does_not_repair(conn, dynamics):
    state = make_state(conflict_stage="hurt", stage_since=minutes_ago(5))
    after = advance_by_event(conn, state, result=appraisal("apology", 0.9, 0.9),
                             dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "hurt"


def test_betrayal_while_repairing_goes_withdrawing(conn, dynamics):
    state = make_state(conflict_stage="repairing", stage_since=hours_ago(2))
    after = advance_by_event(conn, state, result=appraisal("betrayal", 0.9, 0.9),
                             dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "withdrawing"
    assert after.scar_level == pytest.approx(0.2)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conflict_arc.py -q`
Expected: FAIL，`advance_by_event` 不存在。

- [ ] **Step 3: 实现**

按上面的顺序判定，先处理严重事件，再处理修复，最后才是普通正向事件。每次阶段变化都更新 `stage_since=stamp`；`scar_level` clamp 到 `0–1`。`advance_by_event` 只负责返回新状态，由调用方 `save_state`。

窗口计数必须包含当前这条：查询 `appraisal_log` 历史后，如果当前 `result.event == "boundary"` 再加 1；正向计数同理。`withdrawing` 的“其他正向事件”只看历史，不把当前道歉重复算两次。

在 `apply_appraisal()` 里：先读 `state_before`（没有记录就 `get_or_create_state`），把 `scar_level` 传给 `map_appraisal`；`apply_event` 之后调用 `advance_by_event`，有变化则 `save_state`；`AppraisalDelta.stage_after` 取最终阶段，`stage_changed` 表示是否变过。低置信/跳过的分支直接返回 `stage_after=state_before.conflict_stage`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conflict_arc.py tests/test_appraisal_mapping.py -q`
Expected: PASS。

---

### Task 5: 时间型恶化与疤痕淡化

**Files:**
- Modify: `backend/app/relationship/conflict.py`
- Modify: `backend/app/relationship/turn.py`
- Test: `backend/tests/test_conflict_arc.py`、`backend/tests/test_relationship_turn.py`

**Interfaces:**
- Produces: `advance_by_time(conn, state, *, dynamics, now=None, previous_updated_at=None) -> RelationshipState`
- `load_signals()` 在 `decay_state()` 之后调用它，再 `save_state`

规则：

```text
疤痕：scar_level *= 0.5 ** (elapsed_days / scar_halflife_days)，clamp 0–1。

hurt:
  stage_age >= hurt_to_cold_hours → cold
  或 stage_age >= hurt_to_cold_low_patience_hours 且 patience < cold_low_patience → cold

cold:
  stage_age >= cold_to_withdrawing_hours 且 security < withdraw_low_security → withdrawing

withdrawing:
  stage_age >= withdrawing_hours_to_scar → normal_with_scar；scar_level = max(scar_level, 0.4)

repairing:
  最近 repairing_to_cold_hours 内没有新的正向事件 → cold

normal_with_scar:
  scar_level < scar_fade_below 且 stage_age_minutes >= scar_fade_min_days * 1440 → normal
```

- [ ] **Step 1: 写失败测试**

```python
def test_hurt_goes_cold_after_24h(conn, dynamics):
    state = make_state(conflict_stage="hurt", stage_since=hours_ago(25), patience=70)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "cold"


def test_low_patience_goes_cold_after_12h(conn, dynamics):
    state = make_state(conflict_stage="hurt", stage_since=hours_ago(13), patience=30)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "cold"


def test_cold_goes_withdrawing_when_security_low(conn, dynamics):
    state = make_state(conflict_stage="cold", stage_since=hours_ago(49), security=20)
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "withdrawing"


def test_withdrawing_softens_after_14_days(conn, dynamics):
    state = make_state(conflict_stage="withdrawing", stage_since=days_ago(15))
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "normal_with_scar"
    assert after.scar_level >= 0.4


def test_scar_fades_to_normal_after_week(conn, dynamics):
    state = make_state(conflict_stage="normal_with_scar", stage_since=days_ago(8),
                       scar_level=0.05, updated_at=days_ago(8))
    after = advance_by_time(conn, state, dynamics=dynamics, now=NOW)
    assert after.conflict_stage == "normal"
    assert after.scar_level < 0.05
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conflict_arc.py -q`
Expected: FAIL，`advance_by_time` 不存在。

- [ ] **Step 3: 实现**

用 `previous_updated_at or state.updated_at` 和 `now` 算流逝时间；时间戳损坏或 `<= 0` 时只做当前阶段判定，不衰减疤痕。`load_signals()` 在 `decay_state()` 之前记住旧 `updated_at`，`decay_state()` 之后调用 `advance_by_time(..., previous_updated_at=旧值)`，再 `save_state()`。这样 12 小时/48 小时规则能用衰减后的 patience/security 判定。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conflict_arc.py tests/test_relationship_turn.py -q`
Expected: PASS。

---

### Task 6: 阶段影响回复概率与决策日志

**Files:**
- Modify: `backend/app/relationship/decision.py`
- Modify: `backend/app/channels/qq/adapter.py`
- Test: `backend/tests/test_reply_decision.py`

**Interfaces:**
- Produces:
  - `reply_probability(..., stage_multiplier: float = 1.0)`
  - `decide(..., stage: str = "normal")`
  - `Decision.conflict_stage: str = "normal"`
  - `record_decision()` 写入 `conflict_stage`

- [ ] **Step 1: 写失败测试**

```python
def test_stage_multiplier_scales_p0(dynamics):
    normal = reply_probability(..., stage_multiplier=1.0)
    withdrawing = reply_probability(..., stage_multiplier=0.5)
    assert withdrawing[0] == pytest.approx(normal[0] * 0.5, rel=0.01)


def test_decision_log_records_conflict_stage(conn):
    decision = decide(..., stage="cold")
    record_decision(conn, ..., decision=decision)
    row = conn.execute("SELECT conflict_stage FROM decision_log").fetchone()
    assert row["conflict_stage"] == "cold"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -q`
Expected: FAIL，参数/字段不存在。

- [ ] **Step 3: 实现**

`reply_probability()` 在 `p0 = clamp(... * stage_multiplier, floor, ceiling)`；`decide()` 用 `stage_multiplier(stage, dynamics)`，并把 `stage` 写进 `Decision`。适配器 `_decide_and_record()` 传 `stage=signals.state.conflict_stage`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -q`
Expected: PASS。

---

### Task 7: 把阶段与疤痕写进提示词

**Files:**
- Modify: `backend/app/relationship/context.py`
- Test: `backend/tests/test_relationship_context.py`

**Interfaces:**
- Produces: `render_state_context()` 增加阶段行和疤痕行；空阶段不输出。

标签：

```text
hurt             → "心里还有点受伤，语气会收着，回复更短"
cold             → "还在生气，不太想展开话题"
withdrawing      → "有些退缩，不太想主动接话"
repairing        → "在观察对方是不是真心的，愿意听但还没完全放下"
normal_with_scar → "已经缓过来，但那件事还记着；不要主动翻旧账"
normal           → 不输出
```

`normal_with_scar` 且 `scar_level >= 0.3` 时追加一条：“上次的冲突还留着印象，只有对方再犯时才需要提起。”

- [ ] **Step 1: 写失败测试**

```python
def test_withdrawing_context_describes_distance():
    text = render_state_context(make_state(conflict_stage="withdrawing"))
    assert "有些退缩" in text
    assert "withdrawing" not in text


def test_normal_with_scar_mentions_not_dredging_up():
    text = render_state_context(make_state(conflict_stage="normal_with_scar", scar_level=0.5))
    assert "还记着" in text
    assert "不要主动翻旧账" in text
    assert "0.5" not in text


def test_normal_state_has_no_conflict_line():
    assert "受伤" not in render_state_context(make_state())
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_context.py -q`
Expected: FAIL，阶段行不存在。

- [ ] **Step 3: 实现**

在“安全感”之后插入阶段行；`normal` 不插入。保留原有的危机优先段落。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_context.py -q`
Expected: PASS。

---

### Task 8: 适配器使用落账后的阶段生成当前回复

**Files:**
- Modify: `backend/app/channels/qq/adapter.py`
- Test: `backend/tests/test_qq_private_chat.py`、`backend/tests/test_qq_relationship_flow.py`

**Interfaces:**
- Consumes: `AppraisalDelta.stage_after`、`render_state_context()`。
- Produces: `_decide_and_record()` 返回 `(Decision, RelationshipState)`，其中第二个状态用于提示词，`conflict_stage` 取 `delta.stage_after`。

- [ ] **Step 1: 写失败测试**

```python
@async_test
async def test_betrayal_changes_same_round_prompt(tmp_path):
    # fake appraiser 返回 betrayal；即使本轮回复，state_context 也应包含“受伤”
    ...


@async_test
async def test_withdrawing_still_replies_to_crisis(tmp_path):
    # 预置 conflict_stage="withdrawing"；危机消息仍然强制回复，context 含“退缩”
    ...
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_private_chat.py tests/test_qq_relationship_flow.py -q`
Expected: FAIL，提示词仍是旧阶段。

- [ ] **Step 3: 实现**

`_decide_and_record()` 在 `apply_appraisal()` 之后用 `dataclasses.replace(signals.state, conflict_stage=delta.stage_after)` 构造提示词状态；没有 delta 时用 `signals.state`。返回这个状态给 `_private_round()` 渲染 `state_context`。危机分支不评估，但仍按当前阶段渲染（危机强制回复不受阶段乘数影响）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_private_chat.py tests/test_qq_relationship_flow.py -q`
Expected: PASS。

---

### Task 9: 文档与全量回归

**Files:**
- Modify: `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`
- Modify: `docs/superpowers/plans/2026-11-09-03c-appraisal-layer.md`
- Modify: `C:\Users\Adam\.claude\projects\e--study-code-LangChain-WordPressAssistant\memory\kb-vector-retrieval-state.md`

- [ ] **Step 1: 更新设计文档**

第 9 节把 3c.1 标为本地完成；4.4 补上本计划确定的最短停留、恶化时间和疤痕半衰期；注明“伤疤记忆接入”因 `memories` 缺 `persona_key` 而推迟，当前用 `relationship_state` + `appraisal_log` 承载。

- [ ] **Step 2: 更新 3c 计划**

把 3c 计划的 Scope note 改成链接到 `2026-11-09-03c1-conflict-arc.md`，不再写“单独写 3c.1 计划”。

- [ ] **Step 3: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿，基线为 3c 完成后的通过数加新增用例。

- [ ] **Step 4: 记录进度**

更新记忆文件：3c 和 3c.1 本地完成、未提交、未部署；下一步 4b（群策略、待续话题、主动调度）。
