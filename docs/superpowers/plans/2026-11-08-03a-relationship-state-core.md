# 关系状态核心（3a）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为人设文件加入可校验的 `dynamics` 动态参数，并按 `(persona_key, person_key)` 建立关系状态表，落地 affection、valence、energy、reply_debt、affinity 派生、角色初始值与衰减。

**Architecture:** 人设文件是动态参数的唯一来源；`app/persona/models.py` 负责加载和校验，`app/relationship/state.py` 负责状态读写与纯函数计算。状态存进现有 `memory.sqlite3`，不新建数据库。3a 只做底层，不接 QQ 适配器、不做概率采样。

**Tech Stack:** Python 3.11+、Pydantic v2、SQLite、pytest。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`

## Global Constraints

- 所有时间使用 UTC ISO 字符串，带时区：`datetime.now(timezone.utc).isoformat(timespec="seconds")`。
- 不新增运行时依赖；只用标准库、Pydantic 和现有 SQLite。
- 人设 `dynamics` 是心情/回复/关系/节奏参数的唯一事实来源；代码只提供默认值。
- 关系状态按 `(persona_key, person_key)` 隔离；`persona_key` 来自 `PersonaVersion.state_key`。
- 不修改 `app.memory.db.stats()` 的返回键，避免破坏现有测试和健康检查。
- 3a 不接 QQ 适配器、不做概率采样、不迁移 `events`/`memories` 的 persona 列；这些属于 4a。
- 在 `backend/` 目录下运行测试：`python -m pytest`。
- **不要提交或推送**，除非用户明确要求；每个任务以测试通过为完成标准。

## Review Focus

- 人设文件不写 `dynamics` 或只写一部分时，必须用默认值补齐，不能报错。
- affection 达到或超过角色上限后，正面事件不再增长，但不能把已有值拉下来。
- 同一个人在两个 persona 下必须各有一行状态，互不影响。
- 时间差为零、为负或时间戳损坏时，衰减必须安全返回，不抛异常。
- 未知角色字符串必须回退到 `normal`，不能 `KeyError`。

---

### Task 1: 人设动态参数模型

**Files:**
- Modify: `backend/app/persona/models.py`
- Test: `backend/tests/test_persona_dynamics.py`

**Interfaces:**
- Consumes: 现有 `PersonaVersion`。
- Produces: `Dynamics`、`MoodDynamics`、`EnergyDynamics`、`ReplyDynamics`、`RoleDynamics`、`RelationshipDynamics`、`PacingDynamics`、`SafetyDynamics`、`NamespaceDynamics`；`PersonaVersion.dynamics`、`PersonaVersion.state_key`、`PersonaVersion.memory_key`。

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.persona.models import PersonaVersion


def make_persona(**overrides):
    data = {
        "schema": "l2d.persona/v1",
        "id": "merlin",
        "version": 1,
        "name": "梅林",
        "core": "CORE",
    }
    data.update(overrides)
    return PersonaVersion.model_validate(data)


def test_dynamics_defaults_are_available_without_a_block():
    persona = make_persona()
    assert persona.dynamics.reply.base == 0.95
    assert persona.dynamics.relationship.roles["normal"].initial == 45
    assert persona.state_key == "merlin"
    assert persona.memory_key == "merlin"


def test_partial_dynamics_merges_with_defaults():
    persona = make_persona(
        dynamics={
            "reply": {"base": 0.8},
            "namespace": {"state": "merlin-v2"},
        }
    )
    assert persona.dynamics.reply.base == 0.8
    assert persona.dynamics.reply.affinity_curve == 0.7
    assert persona.dynamics.relationship.affection_unit == 1.0
    assert persona.state_key == "merlin-v2"
    assert persona.memory_key == "merlin"


def test_hourly_curve_must_have_24_entries():
    with pytest.raises(ValidationError):
        make_persona(dynamics={"energy": {"hourly_curve": [0.5] * 23}})


def test_all_four_roles_are_required_when_roles_are_overridden():
    with pytest.raises(ValidationError):
        make_persona(
            dynamics={
                "relationship": {
                    "roles": {
                        "owner": {"initial": 85, "cap": 100, "growth": 1.0}
                    }
                }
            }
        )


def test_unknown_dynamics_field_is_rejected():
    with pytest.raises(ValidationError):
        make_persona(dynamics={"reply": {"unknown": 1}})
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_persona_dynamics.py -v`
Expected: FAIL，`dynamics` 被 `extra="forbid"` 拒绝，或 `state_key`/`memory_key` 不存在。

- [ ] **Step 3: 实现模型**

在 `backend/app/persona/models.py` 顶部加 `import re`，在 `PERSONA_SCHEMA` 附近加命名空间正则：

```python
_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
```

新增以下模型（全部 `model_config = ConfigDict(extra="forbid", allow_inf_nan=False)`，除 `SafetyDynamics` 外都允许 `allow_inf_nan=False`）：

```python
class MoodDynamics(BaseModel):
    baseline_valence: float = Field(default=0.1, ge=-1.0, le=1.0)
    valence_half_life_minutes: float = Field(default=90.0, gt=0.0, le=10_080.0)
    valence_delta_cap: float = Field(default=0.1, ge=0.0, le=2.0)
    event_delta_cap: float = Field(default=0.3, ge=0.0, le=2.0)


class EnergyDynamics(BaseModel):
    hourly_curve: list[float] = Field(default_factory=lambda: [
        0.15, 0.10, 0.10, 0.12, 0.20, 0.35,
        0.55, 0.70, 0.85, 0.95, 1.00, 1.00,
        0.95, 0.90, 0.90, 0.95, 1.00, 1.00,
        0.95, 0.90, 0.80, 0.65, 0.45, 0.25,
    ])
    fatigue_half_life_minutes: float = Field(default=90.0, gt=0.0, le=10_080.0)
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
    base: float = Field(default=0.95, ge=0.0, le=1.0)
    floor: float = Field(default=0.02, ge=0.0, le=1.0)
    ceiling: float = Field(default=0.95, ge=0.0, le=1.0)
    debt_increment: float = Field(default=0.1, ge=0.0, le=1.0)
    debt_half_life_hours: float = Field(default=8.0, gt=0.0, le=720.0)
    debt_lambda: float = Field(default=1.5, gt=0.0, le=10.0)
    affinity_min: float = Field(default=0.25, ge=0.0, le=2.0)
    affinity_max: float = Field(default=1.2, ge=0.0, le=2.0)
    affinity_curve: float = Field(default=0.7, gt=0.0, le=5.0)

    @model_validator(mode="after")
    def _ranges(self) -> "ReplyDynamics":
        if self.floor > self.ceiling:
            raise ValueError("floor 不能大于 ceiling")
        if self.affinity_min > self.affinity_max:
            raise ValueError("affinity_min 不能大于 affinity_max")
        return self


class RoleDynamics(BaseModel):
    initial: float = Field(ge=0.0, le=100.0)
    cap: float = Field(ge=0.0, le=100.0)
    growth: float = Field(default=1.0, ge=0.0, le=3.0)

    @model_validator(mode="after")
    def _initial_not_above_cap(self) -> "RoleDynamics":
        if self.initial > self.cap:
            raise ValueError("initial 不能大于 cap")
        return self


class RelationshipDynamics(BaseModel):
    affection_unit: float = Field(default=1.0, ge=0.1, le=5.0)
    initial_missing: float = Field(default=50.0, ge=0.0, le=100.0)
    initial_security: float = Field(default=70.0, ge=0.0, le=100.0)
    initial_patience: float = Field(default=70.0, ge=0.0, le=100.0)
    roles: dict[str, RoleDynamics] = Field(default_factory=lambda: {
        "owner": RoleDynamics(initial=85, cap=100, growth=1.0),
        "exception": RoleDynamics(initial=50, cap=85, growth=0.9),
        "special": RoleDynamics(initial=40, cap=75, growth=0.8),
        "normal": RoleDynamics(initial=45, cap=60, growth=0.6),
    })

    @field_validator("roles")
    @classmethod
    def _all_roles(cls, value: dict[str, RoleDynamics]) -> dict[str, RoleDynamics]:
        missing = {"owner", "exception", "special", "normal"} - set(value)
        if missing:
            raise ValueError(f"缺少角色：{sorted(missing)}")
        return value


class PacingDynamics(BaseModel):
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


class SafetyDynamics(BaseModel):
    quiet_hours: list[str] = Field(default_factory=lambda: ["00:00-07:00"])
    crisis_keywords: list[str] = Field(default_factory=lambda: [
        "自杀", "自残", "救命", "出事了",
    ])

    @field_validator("quiet_hours")
    @classmethod
    def _valid_hours(cls, value: list[str]) -> list[str]:
        pattern = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
        for item in value:
            if not pattern.fullmatch(item):
                raise ValueError(f"quiet_hours 格式不合法：{item!r}")
        return value


class NamespaceDynamics(BaseModel):
    state: str = Field(default="", max_length=64)
    memory: str = Field(default="", max_length=64)

    @field_validator("state", "memory")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        if value and not _NAMESPACE_PATTERN.fullmatch(value):
            raise ValueError(f"namespace 不合法：{value!r}")
        return value


class Dynamics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mood: MoodDynamics = Field(default_factory=MoodDynamics)
    energy: EnergyDynamics = Field(default_factory=EnergyDynamics)
    reply: ReplyDynamics = Field(default_factory=ReplyDynamics)
    relationship: RelationshipDynamics = Field(default_factory=RelationshipDynamics)
    pacing: PacingDynamics = Field(default_factory=PacingDynamics)
    safety: SafetyDynamics = Field(default_factory=SafetyDynamics)
    namespace: NamespaceDynamics = Field(default_factory=NamespaceDynamics)
```

在 `PersonaVersion` 里加字段和属性：

```python
dynamics: Dynamics = Field(default_factory=Dynamics)

@property
def state_key(self) -> str:
    return self.dynamics.namespace.state or self.id

@property
def memory_key(self) -> str:
    return self.dynamics.namespace.memory or self.id
```

`MoodDynamics`、`EnergyDynamics`、`ReplyDynamics`、`RoleDynamics`、`RelationshipDynamics`、`PacingDynamics`、`SafetyDynamics`、`NamespaceDynamics` 都必须写 `model_config = ConfigDict(extra="forbid")`；浮点模型再加 `allow_inf_nan=False`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_persona_dynamics.py tests/test_persona_store.py -v`
Expected: PASS。

---

### Task 2: 关系状态表与 `persons.role` 迁移

**Files:**
- Modify: `backend/app/memory/db.py`
- Test: `backend/tests/test_memory_db.py`

**Interfaces:**
- Consumes: 现有 `init_schema()`、`connect()`。
- Produces: `relationship_state`、`relationship_events` 两张表；旧库的 `persons.role` 列。

- [ ] **Step 1: 写失败测试**

在 `tests/test_memory_db.py` 末尾加：

```python
def test_init_schema_creates_relationship_tables(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"relationship_state", "relationship_events"} <= names

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_state)")
        }
        assert {
            "persona_key", "person_key", "affection", "missing", "security",
            "patience", "valence", "energy", "reply_debt", "updated_at",
        } <= columns

        primary_key = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_state)")
            if row["pk"]
        ]
        assert primary_key == ["persona_key", "person_key"]
    finally:
        conn.close()


def test_init_schema_adds_role_to_legacy_persons_table(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS persons")
        conn.execute(
            """
            CREATE TABLE persons (
                person_key TEXT PRIMARY KEY,
                display_name TEXT,
                wp_user_id INTEGER,
                qq_uin TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                note TEXT
            )
            """
        )
        conn.commit()

        db.init_schema(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(persons)")
        }
        assert "role" in columns
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_memory_db.py -v`
Expected: FAIL，`relationship_state` 不存在或 `persons.role` 不存在。

- [ ] **Step 3: 实现建表与迁移**

在 `_SCHEMA` 的 `persons` 表里加一列：

```sql
role TEXT NOT NULL DEFAULT 'normal',
```

在 `_SCHEMA` 末尾加：

```sql
CREATE TABLE IF NOT EXISTS relationship_state (
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
);

CREATE INDEX IF NOT EXISTS idx_relationship_state_person
    ON relationship_state(person_key);

CREATE TABLE IF NOT EXISTS relationship_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    d_affection REAL NOT NULL DEFAULT 0,
    d_missing REAL NOT NULL DEFAULT 0,
    d_security REAL NOT NULL DEFAULT 0,
    d_patience REAL NOT NULL DEFAULT 0,
    d_valence REAL NOT NULL DEFAULT 0,
    d_energy REAL NOT NULL DEFAULT 0,
    d_debt REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1,
    source_event_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relationship_events_person
    ON relationship_events(persona_key, person_key, id);
```

在 `init_schema()` 里、`executescript(_SCHEMA)` 之后加迁移：

```python
def _ensure_person_role_column(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(persons)")
    }
    if "role" not in columns:
        conn.execute(
            "ALTER TABLE persons ADD COLUMN role TEXT NOT NULL DEFAULT 'normal'"
        )
```

然后在 `init_schema()` 中调用 `_ensure_person_role_column(conn)`，再执行 `MEMORIES_VEC.ensure(...)`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_memory_db.py -v`
Expected: PASS，原有 `stats()` 断言不变。

---

### Task 3: affinity 与状态初始化

**Files:**
- Create: `backend/app/relationship/__init__.py`
- Create: `backend/app/relationship/state.py`
- Test: `backend/tests/test_relationship_state.py`

**Interfaces:**
- Consumes: `app.persona.models.Dynamics`、`app.memory.db`。
- Produces:
  - `RelationshipState`（frozen dataclass）。
  - `affinity_for(affection: float, dynamics: Dynamics) -> float`
  - `get_or_create_state(conn, persona_key, person_key, *, role, dynamics, now=None) -> RelationshipState`
  - `save_state(conn, state, *, commit=True) -> None`
  - `role_for(conn, person_key) -> str`
  - `ROLE_NAMES`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.state import (
    affinity_for,
    get_or_create_state,
    save_state,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def dynamics(**overrides):
    data = {
        "schema": "l2d.persona/v1",
        "id": "merlin",
        "version": 1,
        "name": "梅林",
        "core": "CORE",
    }
    if overrides:
        data["dynamics"] = overrides
    return PersonaVersion.model_validate(data).dynamics


def test_affinity_matches_default_curve():
    d = dynamics()
    assert affinity_for(0, d) == pytest.approx(0.25)
    assert affinity_for(30, d) == pytest.approx(0.66, abs=0.01)
    assert affinity_for(45, d) == pytest.approx(0.79, abs=0.01)
    assert affinity_for(85, d) == pytest.approx(1.10, abs=0.01)
    assert affinity_for(100, d) == pytest.approx(1.20)


def test_affinity_uses_custom_curve():
    d = dynamics(reply={"affinity_min": 0.5, "affinity_max": 1.0, "affinity_curve": 1.0})
    assert affinity_for(50, d) == pytest.approx(0.75)


def test_state_initializes_from_role(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="owner", dynamics=d)
    assert state.affection == 85
    assert state.missing == 50
    assert state.security == 70
    assert state.patience == 70
    assert state.valence == pytest.approx(d.mood.baseline_valence)
    assert 0 <= state.energy <= 1
    assert state.reply_debt == 0


def test_unknown_role_falls_back_to_normal(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:2", role="stranger", dynamics=d)
    assert state.affection == 45


def test_persona_state_is_isolated(conn):
    d = dynamics()
    merlin = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    other = get_or_create_state(conn, "other", "wp:1", role="normal", dynamics=d)
    assert merlin.affection == other.affection == 45

    save_state(conn, replace(merlin, affection=99))

    assert get_or_create_state(
        conn, "merlin", "wp:1", role="normal", dynamics=d
    ).affection == 99
    assert get_or_create_state(
        conn, "other", "wp:1", role="normal", dynamics=d
    ).affection == 45
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_relationship_state.py -v`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现状态模块**

`backend/app/relationship/__init__.py` 可以为空。`state.py` 至少包含：

```python
ROLE_NAMES = ("owner", "exception", "special", "normal")

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
    reply_debt: float
    last_interaction_at: str | None
    last_proactive_at: str | None
    last_reply_at: str | None
    attention_confidence: float
    updated_at: str


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def affinity_for(affection: float, dynamics: Dynamics) -> float:
    cfg = dynamics.reply
    ratio = clamp(affection / 100.0, 0.0, 1.0)
    value = cfg.affinity_min + (cfg.affinity_max - cfg.affinity_min) * (ratio ** cfg.affinity_curve)
    return clamp(value, cfg.affinity_min, cfg.affinity_max)
```

实现 `now_iso()`、`parse_iso()`、`energy_at()`、`role_config()`、`role_for()`、`state_from_row()`、`get_state()`、`save_state()`、`get_or_create_state()`：

```python
def role_config(role: str, dynamics: Dynamics) -> RoleDynamics:
    return dynamics.relationship.roles.get(role) or dynamics.relationship.roles["normal"]


def role_for(conn: sqlite3.Connection, person_key: str) -> str:
    row = conn.execute(
        "SELECT role FROM persons WHERE person_key = ?", (person_key,)
    ).fetchone()
    if row is not None and row["role"] in ROLE_NAMES:
        return row["role"]
    return "normal"
```

`get_or_create_state()` 找不到行时按角色初始值创建：`affection=role_config.initial`、`missing/security/patience` 取 `dynamics.relationship.initial_*`、`valence=dynamics.mood.baseline_valence`、`energy=energy_at(now, dynamics.energy.hourly_curve)`、`reply_debt=0`、`attention_confidence=0`。

`save_state()` 用 `INSERT ... ON CONFLICT(persona_key, person_key) DO UPDATE SET ...` 写回全部字段；`commit=False` 时只执行不提交，供后续任务组事务。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_relationship_state.py -v`
Expected: PASS。

---

### Task 4: 衰减与关系事件

**Files:**
- Modify: `backend/app/relationship/state.py`
- Test: `backend/tests/test_relationship_state.py`

**Interfaces:**
- Produces:
  - `decay_state(state, dynamics, *, now=None) -> RelationshipState`
  - `apply_event(conn, persona_key, person_key, *, role, dynamics, kind, d_affection_units=0.0, d_missing=0.0, d_security=0.0, d_patience=0.0, d_valence=0.0, d_energy=0.0, d_debt=0.0, confidence=1.0, source_event_id=None, note="", touch_interaction=True, now=None) -> RelationshipState`

- [ ] **Step 1: 写失败测试**

在 `tests/test_relationship_state.py` 末尾加：

```python
from app.relationship.state import apply_event, decay_state


def test_affection_uses_unit_and_growth(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=10,
    )
    assert after.affection == pytest.approx(51.0)  # 45 + 10 * 1.0 * 0.6


def test_growth_stops_at_role_cap(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=100,
    )
    assert after.affection == pytest.approx(60.0)


def test_negative_affection_floors_at_zero(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="conflict", d_affection_units=-100,
    )
    assert after.affection == pytest.approx(0.0)


def test_valence_delta_is_capped(conn):
    d = dynamics()
    after = apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="mood", d_valence=1.0,
    )
    assert after.valence == pytest.approx(
        d.mood.baseline_valence + d.mood.valence_delta_cap
    )


def test_valence_decays_by_half_life(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        valence=1.0,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_state(conn, state)

    decayed = decay_state(state, d, now="2026-01-01T01:30:00+00:00")

    expected = d.mood.baseline_valence + (1.0 - d.mood.baseline_valence) * 0.5
    assert decayed.valence == pytest.approx(expected)


def test_reply_debt_decays_by_half_life(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(
        state,
        reply_debt=0.8,
        updated_at="2026-01-01T00:00:00+00:00",
    )

    decayed = decay_state(state, d, now="2026-01-01T08:00:00+00:00")

    assert decayed.reply_debt == pytest.approx(0.4)


def test_decay_is_safe_for_zero_or_invalid_elapsed(conn):
    d = dynamics()
    state = get_or_create_state(conn, "merlin", "wp:1", role="normal", dynamics=d)
    state = replace(state, valence=0.8, updated_at="not-a-time")

    decayed = decay_state(state, d, now="2026-01-01T00:00:00+00:00")

    assert decayed.valence == 0.8
    assert decayed.updated_at == "2026-01-01T00:00:00+00:00"


def test_apply_event_records_relationship_event(conn):
    d = dynamics()
    apply_event(
        conn, "merlin", "wp:1", role="normal", dynamics=d,
        kind="chat", d_affection_units=10, d_valence=0.05, d_debt=0.1,
    )

    row = conn.execute("SELECT * FROM relationship_events").fetchone()

    assert row["persona_key"] == "merlin"
    assert row["kind"] == "chat"
    assert row["d_affection"] == pytest.approx(6.0)
    assert row["d_valence"] == pytest.approx(0.05)
    assert row["d_debt"] == pytest.approx(0.1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_relationship_state.py -v`
Expected: FAIL，`apply_event`/`decay_state` 不存在。

- [ ] **Step 3: 实现衰减与事件**

`decay_state()` 规则：

- 用 `updated_at` 到 `now` 的秒数；解析失败或 `elapsed <= 0` 时只更新 `updated_at`，其余不变。
- valence：`baseline + (valence - baseline) * 0.5 ** (elapsed_minutes / valence_half_life_minutes)`，再 clamp 到 `[-1, 1]`。
- reply_debt：`reply_debt * 0.5 ** (elapsed_hours / debt_half_life_hours)`，下限 0。
- energy 在 3a 不随时间衰减；4a 加会话疲劳后再按曲线重算。

`apply_event()` 规则：

- 先 `get_or_create_state()`，再 `decay_state()` 得到基准状态。
- affection：`raw = d_affection_units * affection_unit`；`raw > 0` 时乘角色 `growth`；`new = clamp(base + raw, 0, 100)`；`raw > 0` 时再 `min(new, role.cap)`。
- missing/security/patience：`clamp(base + delta, 0, 100)`。
- valence：先把 `d_valence` clamp 到 `±valence_delta_cap`，再加到 base 并 clamp 到 `[-1, 1]`。
- energy：先把 `d_energy` clamp 到 `±energy_delta_cap`，再加到 base 并 clamp 到 `[0, 1]`。
- reply_debt：`max(0, base + d_debt)`。
- `touch_interaction=True` 时更新 `last_interaction_at`；始终更新 `updated_at`。
- 写回状态和 `relationship_events`，事件里的 delta 用“写入后 - 衰减后”的实际值，不包含衰减本身。
- 状态与事件在同一个事务里提交：`save_state(..., commit=False)`、插入事件、最后 `conn.commit()`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_relationship_state.py -v`
Expected: PASS。

- [ ] **Step 5: 跑完整测试**

Run: `python -m pytest`
Expected: 现有 308 passed / 1 skipped 加上新增用例，全部通过。

---

### Task 5: 示例人设与文档

**Files:**
- Modify: `backend/personas.example/merlin/1.json`
- Modify: `backend/personas.example/README.md`
- Test: `backend/tests/test_persona_store.py`

**Interfaces:**
- Consumes: Task 1 的 `Dynamics`。
- Produces: 可加载的示例 `dynamics` 块；README 字段说明。

- [ ] **Step 1: 写失败测试**

在 `tests/test_persona_store.py` 末尾加：

```python
def test_public_example_persona_carries_dynamics() -> None:
    example_root = Path(__file__).resolve().parents[1] / "personas.example"

    persona = PersonaStore(example_root, "merlin").load_active()

    assert persona.state_key == "merlin"
    assert persona.memory_key == "merlin"
    assert persona.dynamics.namespace.state == "merlin"
    assert persona.dynamics.relationship.roles["normal"].initial == 45
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python -m pytest tests/test_persona_store.py::test_public_example_persona_carries_dynamics -v`
Expected: FAIL，`namespace.state` 为空或示例文件没有 `dynamics`。

- [ ] **Step 3: 更新示例人设**

在 `backend/personas.example/merlin/1.json` 的 `metadata` 之前加 `dynamics`，至少包含：

```json
"dynamics": {
  "namespace": {"state": "merlin", "memory": "merlin"}
}
```

其余字段可以省略，由 Task 1 的默认值补齐。不要在这里写真实人设内容。

- [ ] **Step 4: 更新 README**

在 `backend/personas.example/README.md` 的字段表里加一行：

```markdown
| `dynamics` | 可选。心情、精力、回复概率、关系成长、节奏、安全词和隔离 namespace 的默认值；不写则用代码默认值 |
```

同时把错位的 `| metadata | ... |` 行移回字段表内（当前它落在说明段落之后，表格会断）。

- [ ] **Step 5: 运行测试，确认通过**

Run: `python -m pytest tests/test_persona_store.py tests/test_persona_dynamics.py -v`
Expected: PASS。

---

## 完成标准

- `python -m pytest` 全绿，新增测试覆盖上述 Review Focus 五点。
- 不修改 QQ 适配器、概率采样、`events`/`memories` 的 persona 列。
- 人设文件仍是动态参数的唯一来源；代码里不出现第二套心情/关系常量。
- 没有提交或推送；等待用户确认后再进入 4a。

## 执行结果（2026-11-08）

- Task 1–5 全部完成，未提交。
- 全量测试：`.venv/Scripts/python.exe -m pytest` → **329 passed, 1 skipped**。
- 新增：`backend/app/relationship/`、`backend/tests/test_relationship_state.py`、`backend/tests/test_persona_dynamics.py`。
- 修改：`backend/app/persona/models.py`、`backend/app/memory/db.py`、示例人设与 README、设计文档。
- 3a 边界保持不变：未接 QQ 适配器、未做概率采样、未给 `events`/`memories` 加 persona 列。
