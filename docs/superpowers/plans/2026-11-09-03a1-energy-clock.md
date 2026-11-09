# 3a.1 精力时钟化补丁实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把精力从“初始化一次的静态值”改成 `本地小时曲线 − 短期疲劳`，修复 UTC 小时导致的 8 小时偏移，并把疲劳持久化到 `relationship_state`。

**Architecture:** `fatigue` 成为精力的持久化来源；`energy` 每次读取时按 `QUOTA_TIMEZONE` 对应用户本地小时重算。`decay_state()` 负责按半衰期衰减疲劳并重算精力；`apply_event()` 把 `d_energy` 解释成疲劳的增减，而不是直接改静态精力。QQ 适配器把 `settings.quota_timezone` 传进关系层。

**Tech Stack:** Python 3.11+、Pydantic v2、SQLite、zoneinfo、pytest。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（4.1、4.6、9、10.1）

## Global Constraints

- 时间仍统一存 UTC ISO 字符串；只在取“本地小时”时转换成 `QUOTA_TIMEZONE`。
- 不新增运行时依赖；只用标准库 `zoneinfo`。
- `energy = clamp(hourly_curve[本地小时] − fatigue, 0, 1)`。
- `fatigue_half_life_minutes` 默认 **60**，可由人设覆盖。
- 不做跨天累积：白天聊天量不能拖累深夜。
- `quiet_hours` 本阶段不参与回复概率；它只留给 4b 的主动消息调度。
- 不修改 3b 才做的好感范围/affinity 曲线；3a.1 只动精力。
- 在 `backend/` 目录下用项目虚拟环境运行：`.venv/Scripts/python.exe -m pytest`。
- **不要提交或推送**，除非用户明确要求。

## Review Focus

- UTC 00:00 必须按北京时间 08:00 取曲线，不能再用 UTC 小时。
- 时间差为负、为零或时间戳损坏时，衰减不能抛异常。
- 旧库没有 `fatigue` 列时必须自动补列；新库建表时已经带列。
- 疲劳衰减后精力要能随白天时钟恢复，不是只降不升。
- 凌晨曲线低、疲劳高时精力必须落在 0，不能为负。

---

### Task 1: 疲劳半衰期默认值

**Files:**
- Modify: `backend/app/persona/models.py:50-72`
- Test: `backend/tests/test_persona_dynamics.py`

**Interfaces:**
- Consumes: `EnergyDynamics`。
- Produces: `EnergyDynamics.fatigue_half_life_minutes` 默认 `60.0`。

- [x] **Step 1: 写失败测试**

```python
def test_fatigue_half_life_defaults_to_sixty_minutes():
    persona = make_persona()
    assert persona.dynamics.energy.fatigue_half_life_minutes == 60.0
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py::test_fatigue_half_life_defaults_to_sixty_minutes -v`
Expected: FAIL，实际值为 `90.0`。

- [x] **Step 3: 改默认值**

把 `EnergyDynamics.fatigue_half_life_minutes` 的默认值从 `90.0` 改为 `60.0`，并更新类 docstring。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py -v`
Expected: PASS。

---

### Task 2: `relationship_state.fatigue` 字段与迁移

**Files:**
- Modify: `backend/app/memory/db.py:81-100`、`backend/app/memory/db.py:160-180`
- Modify: `backend/app/relationship/state.py:74-170`
- Test: `backend/tests/test_memory_db.py`、`backend/tests/test_relationship_state.py`

**Interfaces:**
- Consumes: 现有 `relationship_state` 表。
- Produces:
  - `relationship_state.fatigue REAL NOT NULL DEFAULT 0`
  - `RelationshipState.fatigue: float`
  - `save_state()` / `state_from_row()` / `get_or_create_state()` 读写 `fatigue`

- [x] **Step 1: 写失败测试**

```python
def test_init_schema_creates_relationship_tables(tmp_path, monkeypatch):
    # 在现有断言集合中加入 fatigue
    ...
    assert "fatigue" in columns


def test_init_schema_adds_fatigue_to_legacy_relationship_state(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS relationship_state")
        conn.execute(
            """
            CREATE TABLE relationship_state (
                persona_key TEXT NOT NULL,
                person_key TEXT NOT NULL,
                affection REAL NOT NULL DEFAULT 0,
                missing REAL NOT NULL DEFAULT 50,
                security REAL NOT NULL DEFAULT 70,
                patience REAL NOT NULL DEFAULT 70,
                valence REAL NOT NULL DEFAULT 0,
                energy REAL NOT NULL DEFAULT 1,
                reply_debt REAL NOT NULL DEFAULT 0,
                last_interaction_at TEXT,
                last_proactive_at TEXT,
                last_reply_at TEXT,
                attention_confidence REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (persona_key, person_key)
            )
            """
        )
        conn.commit()

        db.init_schema(conn)

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(relationship_state)")}
        assert "fatigue" in columns
    finally:
        conn.close()
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py -v`
Expected: FAIL，`fatigue` 不在列集合中。

- [x] **Step 3: 实现字段与迁移**

- `_SCHEMA` 的 `relationship_state` 在 `energy` 后加 `fatigue REAL NOT NULL DEFAULT 0`。
- 新增 `_ensure_relationship_state_fatigue_column(conn)`，用 `PRAGMA table_info` 检查后 `ALTER TABLE ... ADD COLUMN fatigue REAL NOT NULL DEFAULT 0`。
- `init_schema()` 在补 `persons.role` 后调用它。
- `RelationshipState` 增加 `fatigue: float`。
- `state_from_row()` 读 `row["fatigue"]`。
- `save_state()` INSERT/UPDATE 加 `fatigue`。
- `get_or_create_state()` 初始化 `fatigue=0.0`。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py tests/test_relationship_state.py -v`
Expected: PASS。

---

### Task 3: 本地小时与疲劳衰减

**Files:**
- Modify: `backend/app/relationship/state.py:42-70`、`backend/app/relationship/state.py:208-245`
- Test: `backend/tests/test_relationship_state.py`

**Interfaces:**
- Consumes: `fatigue`、`hourly_curve`、`fatigue_half_life_minutes`。
- Produces:
  - `energy_at(when: str, curve: list[float], timezone_name: str = "Asia/Shanghai") -> float`
  - `decay_state(state, dynamics, *, now=None, timezone_name="Asia/Shanghai") -> RelationshipState`

- [x] **Step 1: 写失败测试**

```python
def test_energy_uses_local_hour_not_utc():
    curve = [0.0] * 8 + [1.0] * 16
    assert energy_at(
        "2026-01-01T00:00:00+00:00", curve, timezone_name="UTC"
    ) == pytest.approx(0.0)
    assert energy_at(
        "2026-01-01T00:00:00+00:00", curve, timezone_name="Asia/Shanghai"
    ) == pytest.approx(1.0)


def test_fatigue_decays_and_energy_recovers():
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        fatigue=0.5,
        energy=0.5,
        updated_at="2026-01-01T10:00:00+00:00",
    )
    decayed = decay_state(
        state,
        d,
        now="2026-01-01T11:00:00+00:00",
        timezone_name="UTC",
    )
    assert decayed.fatigue == pytest.approx(0.25)
    assert decayed.energy == pytest.approx(d.energy.hourly_curve[11] - 0.25)


def test_daytime_chat_does_not_drag_late_night():
    d = dynamics()
    state = get_or_create_state(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        now="2026-01-01T10:00:00+00:00", timezone_name="UTC",
    )
    state = replace(state, fatigue=0.3, updated_at="2026-01-01T10:00:00+00:00")
    late = decay_state(
        state, d, now="2026-01-01T23:00:00+00:00", timezone_name="UTC"
    )
    assert late.fatigue < 0.01
    assert late.energy == pytest.approx(d.energy.hourly_curve[23], abs=0.01)
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py -v`
Expected: FAIL，`energy_at` 不接受 `timezone_name`，`decay_state` 不衰减疲劳。

- [x] **Step 3: 实现时钟与衰减**

- `state.py` 顶部加 `from zoneinfo import ZoneInfo, ZoneInfoNotFoundError`。
- `energy_at()` 用 `parse_iso(when).astimezone(ZoneInfo(timezone_name)).hour`；非法时区回退 UTC。
- `decay_state()`：
  - 解析失败仍只推进 `updated_at`，不改状态；
  - `fatigue_factor = 0.5 ** ((elapsed_seconds / 60) / fatigue_half_life_minutes)`；
  - `fatigue = clamp(state.fatigue * fatigue_factor, 0, 1)`；
  - `energy = clamp(energy_at(stamp, curve, timezone_name) - fatigue, 0, 1)`；
  - 时间差为负或为零时疲劳不衰减，但精力仍按当前小时重算。
- 保留 valence / reply_debt 原有衰减。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py tests/test_relationship_turn.py -v`
Expected: PASS。

---

### Task 4: 事件、回复与适配器接入

**Files:**
- Modify: `backend/app/relationship/state.py:296-380`
- Modify: `backend/app/relationship/turn.py:70-130`
- Modify: `backend/app/channels/qq/adapter.py:318-365`、`backend/app/channels/qq/adapter.py:457-485`
- Test: `backend/tests/test_relationship_state.py`、`backend/tests/test_relationship_turn.py`、`backend/tests/test_qq_private_chat.py`

**Interfaces:**
- Consumes: `fatigue`、`energy_at()`、`decay_state()`。
- Produces:
  - `apply_event(..., timezone_name="Asia/Shanghai")`
  - `load_signals(..., timezone_name="Asia/Shanghai")`
  - `record_reply(..., timezone_name="Asia/Shanghai")`
  - `record_silence(..., timezone_name="Asia/Shanghai")`
  - `decay_state()` 重算精力；`d_energy` 改成疲劳增减

- [x] **Step 1: 写失败测试**

```python
def test_record_reply_adds_fatigue_and_lowers_energy(conn):
    d = dynamics()
    state = record_reply(
        conn, "merlin", "wp:7", role="normal", dynamics=d,
        now="2026-01-01T10:00:00+00:00", timezone_name="UTC",
    )
    assert state.fatigue == pytest.approx(d.energy.fatigue_per_turn)
    assert state.energy == pytest.approx(
        d.energy.hourly_curve[10] - state.fatigue, abs=0.01
    )
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_turn.py::test_record_reply_adds_fatigue_and_lowers_energy -v`
Expected: FAIL，`record_reply` 不接受 `timezone_name`。

- [x] **Step 3: 实现事件与适配器**

- `apply_event()` 增加 `timezone_name`，在衰减后把 `d_energy` 解释为疲劳变化：
  `fatigue = clamp(base.fatigue - energy_delta, 0, 1)`，再 `energy = clamp(energy_at(stamp, curve, timezone_name) - fatigue, 0, 1)`。
- `turn.load_signals()` / `record_reply()` / `record_silence()` 增加 `timezone_name` 透传。
- QQ 适配器调用 `load_signals`、`record_silence`、`record_reply` 时传 `self.settings.quota_timezone`；`_record_reply_safe()` 增加 `timezone_name` 参数。
- 保持现有 `d_energy` 参数名不变，避免 4a 调用点全部改签名。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py tests/test_relationship_turn.py tests/test_qq_private_chat.py tests/test_qq_quota_flow.py -v`
Expected: PASS。

- [x] **Step 5: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `359 passed, 1 skipped`（允许因本阶段新增测试而数量增加）。
