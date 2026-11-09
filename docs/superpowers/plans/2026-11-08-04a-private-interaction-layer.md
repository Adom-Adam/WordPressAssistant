# 私聊交互层（4a）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 QQ 私聊从“一问一答”变成“按意愿概率回复”：防抖合并连续消息、生成中被新消息打断、按标点权重分成多条气泡并有打字节奏，每次决定写 `decision_log`。

**Architecture:** 决策与节奏都是纯函数，放在 `app/relationship/decision.py` 和 `app/channels/qq/pacing.py`；关系状态沿用 3a 的 `relationship_state`，每轮回复/沉默各写一条 `relationship_events`。QQ 适配器只做编排：命令/绑定/额度仍即时处理，私聊正文进入一个按会话串行的协调器（`app/channels/qq/conversation.py`）做防抖和打断。群聊在 4a 保持原样，符合设计文档 5.5。

**Tech Stack:** Python 3.11+、Pydantic v2、SQLite、asyncio、pytest（不新增依赖）。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（4.1–4.5、5.2–5.5、8、9）

## Global Constraints

- 所有时间使用 UTC ISO 字符串，带时区。
- 不新增运行时依赖；RNG 必须可注入，测试用固定种子。
- 私聊的节奏/概率参数**只从人设 `dynamics` 读**；`adapter` 里不写死 1.5 秒、0.6 秒、0.25 这类数值。
- 决策层故障必须 **fail-open**：关系库/决策出错时照常回复，绝不因为新功能让梅林变成哑巴。
- 斜杠命令、未绑定引导、额度提示、危机词强制回复，不掷骰子、不受防抖影响。
- 群聊在 4a 不启用概率回复、防抖、打断、多气泡；继续走现有 `trigger` + 同步处理。
- 待续话题（pending follow-up）属于 4b 主动调度，4a 只留 `decision_log` 与关系事件，不建调度器。
- 不修改 `app.memory.db.stats()` 的返回键。
- 在 `backend/` 下用项目 venv 跑测试：`.venv/Scripts/python.exe -m pytest`。
- **不要提交或推送**，除非用户明确要求。

## Review Focus

- 概率沉默后用户再发消息，`reply_debt` 必须上升；连续被忽略时下一次更愿意回。
- 防抖期间连续多条消息只调用一次模型、只占一次额度，合并内容不能丢失或乱序。
- 生成中收到新消息：尚未发气泡 → 取消并合并重答；已发出部分 → 停止剩余气泡，历史里保留半截回复。
- 决策/关系库出错时，私聊仍然正常回复（fail-open），不能返回静默。
- 分气泡不能切断代码块、行内代码、URL；过短的气泡要并回上一段。

---

### Task 1: `decision_log` 表

**Files:**
- Modify: `backend/app/memory/db.py`
- Test: `backend/tests/test_memory_db.py`

**Interfaces:**
- Consumes: 现有 `init_schema()`、`_SCHEMA`。
- Produces: `decision_log` 表（字段见下）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_memory_db.py` 末尾加：

```python
def test_init_schema_creates_decision_log(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")}
        assert {
            "persona_key", "channel_id", "person_key", "message_id",
            "rule_score", "willingness", "valence", "energy", "affinity",
            "reply_debt", "p0", "p", "roll", "verdict", "reason", "created_at",
        } <= columns
        # 健康检查的键不能因为新表变化。
        assert set(db.stats(conn)) == {"events", "memories", "persons"}
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py::test_init_schema_creates_decision_log -v`
Expected: FAIL，`decision_log` 查不到列。

- [ ] **Step 3: 建表**

在 `_SCHEMA` 的 `relationship_events` 索引之后加：

```sql
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    person_key TEXT NOT NULL,
    message_id TEXT,
    rule_score REAL NOT NULL DEFAULT 0,
    willingness REAL NOT NULL DEFAULT 0,
    valence REAL NOT NULL DEFAULT 0,
    energy REAL NOT NULL DEFAULT 1,
    affinity REAL NOT NULL DEFAULT 1,
    reply_debt REAL NOT NULL DEFAULT 0,
    p0 REAL NOT NULL DEFAULT 1,
    p REAL NOT NULL DEFAULT 1,
    roll REAL,
    verdict TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_log_persona
    ON decision_log(persona_key, id);
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_db.py -v`
Expected: PASS。

---

### Task 2: 回复决策引擎

**Files:**
- Create: `backend/app/relationship/decision.py`
- Test: `backend/tests/test_reply_decision.py`

**Interfaces:**
- Consumes: `app.persona.models.Dynamics`、`app.relationship.state.now_iso`。
- Produces:
  - `Decision`（frozen dataclass：willingness / affinity / valence / energy / reply_debt / p0 / p / roll / reply / reason）
  - `willingness_for(text: str, recent: Sequence[str] = (), *, name: str = "") -> float`
  - `reply_probability(*, willingness, affinity, valence, energy, reply_debt, dynamics) -> tuple[float, float]`
  - `decide(*, text, recent, affinity, valence, energy, reply_debt, dynamics, rng, force_reply=False, reason="") -> Decision`
  - `record_decision(conn, *, persona_key, channel_id, person_key, message_id, decision, rule_score=0.0, now=None) -> int`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.decision import (
    decide,
    record_decision,
    reply_probability,
    willingness_for,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(memory_db_path=str(tmp_path / "memory.sqlite3"), embedding_dim=4)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def dynamics(**overrides):
    data = {"schema": "l2d.persona/v1", "id": "merlin", "version": 1, "name": "梅林", "core": "C"}
    if overrides:
        data["dynamics"] = overrides
    return PersonaVersion.model_validate(data).dynamics


def test_question_and_name_raise_willingness():
    plain = willingness_for("今天天气不错")
    question = willingness_for("梅林，你在吗？")
    assert question > plain
    assert 0.05 <= plain < question <= 1.0


def test_repeat_lowers_willingness():
    first = willingness_for("在吗", recent=[])
    repeated = willingness_for("在吗", recent=["在吗", "在吗"])
    assert repeated < first


def test_probability_uses_formula_and_clamps():
    d = dynamics()
    p0, p = reply_probability(
        willingness=1.0, affinity=1.2, valence=0.0, energy=1.0, reply_debt=0, dynamics=d
    )
    assert p0 == pytest.approx(0.95)
    assert p == pytest.approx(p0)

    p0, p = reply_probability(
        willingness=0.0, affinity=0.0, valence=-1.0, energy=0.0, reply_debt=0, dynamics=d
    )
    assert p0 == pytest.approx(d.reply.floor)


def test_reply_debt_raises_probability_but_never_above_098():
    d = dynamics()
    _, low = reply_probability(willingness=0.5, affinity=0.8, valence=0, energy=0.5, reply_debt=0, dynamics=d)
    _, high = reply_probability(willingness=0.5, affinity=0.8, valence=0, energy=0.5, reply_debt=5, dynamics=d)
    assert high > low
    assert high <= 0.98


def test_force_reply_skips_roll():
    d = dynamics()
    decision = decide(
        text="救命", recent=[], affinity=0.25, valence=0, energy=0.1,
        reply_debt=0, dynamics=d, rng=random.Random(0), force_reply=True,
    )
    assert decision.reply is True
    assert decision.roll is None
    assert decision.reason == "forced"


def test_roll_compares_against_probability():
    d = dynamics()
    class AlwaysRoll(random.Random):
        def random(self):
            return 0.0
    decision = decide(
        text="在吗", recent=[], affinity=0.79, valence=0, energy=1,
        reply_debt=0, dynamics=d, rng=AlwaysRoll(0),
    )
    assert decision.reply is True
    assert decision.roll == 0.0


def test_record_decision_writes_row(conn):
    d = dynamics()
    decision = decide(
        text="在吗", recent=[], affinity=0.79, valence=0.1, energy=0.9,
        reply_debt=0, dynamics=d, rng=random.Random(1),
    )
    record_decision(
        conn, persona_key="merlin", channel_id="qq_private", person_key="wp:1",
        message_id=123, decision=decision,
    )
    row = conn.execute("SELECT * FROM decision_log").fetchone()
    assert row["persona_key"] == "merlin"
    assert row["verdict"] in {"reply", "silent"}
    assert row["roll"] == pytest.approx(decision.roll)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -v`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现**

`willingness_for` 以 0.55 为基线：

- 问句（`?`/`？`，或以 `吗/呢/吧` 结尾，或含 `怎么/为什么/什么/哪/谁/多少/能不能/可不可以/帮我`）+0.25；
- 文本里出现 `name`（人设名，非空时）+0.15；
- 长度 ≥ 20 字 +0.05；
- `recent` 非空（是在接上一句）+0.05；
- 与 `recent` 中任一条完全相同（去空白后）−0.35；出现 ≥2 次 −0.2；
- `recent` 条数 ≥ 5（刷屏）−0.15；
- 结果 clamp 到 `[0.05, 1.0]`。

`reply_probability`：

```python
p0 = clamp(
    cfg.base * willingness * affinity * math.exp(0.8 * valence) * (0.55 + 0.45 * energy),
    cfg.floor,
    cfg.ceiling,
)
p = p0 + (0.98 - p0) * (1 - math.exp(-cfg.debt_lambda * reply_debt))
return p0, clamp(p, cfg.floor, 0.98)
```

`decide`：`force_reply=True` 时返回 `reply=True, roll=None, reason="forced"`；否则 `roll=rng.random()`，`reply = roll < p`，`reason` 为传入值或 `"probabilistic"`。

`record_decision` 写入 `decision_log`，`verdict = "reply" if decision.reply else "silent"`，`created_at = now or now_iso()`，`conn.commit()`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -v`
Expected: PASS。

---

### Task 3: 每轮关系信号

**Files:**
- Modify: `backend/app/persona/models.py`
- Modify: `backend/app/relationship/state.py`
- Create: `backend/app/relationship/turn.py`
- Test: `backend/tests/test_persona_dynamics.py`
- Test: `backend/tests/test_relationship_turn.py`

**Interfaces:**
- Consumes: Task 2 的 `Decision`；3a 的 `get_or_create_state` / `decay_state` / `apply_event` / `role_for`；`app.memory.identity.person_key_qq`。
- Produces:
  - `RelationshipDynamics.chat_affection_units`（默认 0.5）、`chat_valence`（默认 0.03）
  - `apply_event(..., touch_reply: bool = False)`：`touch_reply=True` 时把 `last_reply_at` 推到当前时间
  - `relationship_subject(conn, qq_uin, bound_user_id) -> tuple[str, str]`（person_key, role）
  - `load_signals(conn, persona_key, person_key, *, role, dynamics, now=None) -> TurnSignals`
  - `record_reply(conn, persona_key, person_key, *, role, dynamics, now=None) -> RelationshipState`
  - `record_silence(conn, persona_key, person_key, *, role, dynamics, now=None) -> RelationshipState`
  - `TurnSignals.affinity` 由 `affinity_for(state.affection, dynamics)` 派生

- [ ] **Step 1: 写失败测试**

在 `tests/test_persona_dynamics.py` 末尾加：

```python
def test_chat_signal_defaults_are_settable():
    persona = make_persona()
    assert persona.dynamics.relationship.chat_affection_units == 0.5
    assert persona.dynamics.relationship.chat_valence == 0.03

    persona = make_persona(
        dynamics={"relationship": {"chat_affection_units": 2.0, "chat_valence": -0.05}}
    )
    assert persona.dynamics.relationship.chat_affection_units == 2.0
    assert persona.dynamics.relationship.chat_valence == -0.05
```

新建 `tests/test_relationship_turn.py`：

```python
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.state import get_or_create_state, save_state
from app.relationship.turn import (
    load_signals,
    record_reply,
    record_silence,
    relationship_subject,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(memory_db_path=str(tmp_path / "memory.sqlite3"), embedding_dim=4)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    connection.execute(
        "INSERT INTO persons (person_key, first_seen_at, last_seen_at, role) VALUES (?, ?, ?, ?)",
        ("wp:7", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "special"),
    )
    connection.commit()
    yield connection
    connection.close()


def dynamics():
    return PersonaVersion.model_validate(
        {"schema": "l2d.persona/v1", "id": "merlin", "version": 1, "name": "梅林", "core": "C"}
    ).dynamics


def test_bound_qq_uses_wp_key_and_higher_role(conn):
    person_key, role = relationship_subject(conn, 10001, 7)
    assert person_key == "wp:7"
    assert role == "special"


def test_unbound_qq_uses_qq_key_and_normal_role(conn):
    person_key, role = relationship_subject(conn, 10001, None)
    assert person_key == "qq:10001"
    assert role == "normal"


def test_load_signals_derives_affinity(conn):
    d = dynamics()
    signals = load_signals(conn, "merlin", "wp:7", role="normal", dynamics=d)
    assert signals.state.affection == 45
    assert signals.affinity == pytest.approx(0.79, abs=0.01)


def test_record_reply_grows_affection_and_sets_last_reply(conn):
    d = dynamics()
    state = record_reply(conn, "merlin", "wp:7", role="normal", dynamics=d)
    # 45 + 0.5 units * 1.0 unit * 0.6 growth = 45.3
    assert state.affection == pytest.approx(45.3)
    assert state.last_reply_at is not None
    assert state.energy < 1.0


def test_record_silence_adds_debt_without_reply(conn):
    d = dynamics()
    state = record_silence(conn, "merlin", "wp:7", role="normal", dynamics=d)
    assert state.reply_debt == pytest.approx(d.reply.debt_increment)
    assert state.last_reply_at is None


def test_turn_signals_are_persona_isolated(conn):
    d = dynamics()
    record_reply(conn, "merlin", "wp:7", role="normal", dynamics=d)
    other = get_or_create_state(conn, "other", "wp:7", role="normal", dynamics=d)
    assert other.affection == 45
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py tests/test_relationship_turn.py -v`
Expected: FAIL，字段/模块不存在。

- [ ] **Step 3: 实现**

- `RelationshipDynamics` 增加两个带范围校验的字段。
- `apply_event` 增加 `touch_reply=False` 参数，`last_reply_at=stamp if touch_reply else base.last_reply_at`。
- `relationship_subject`：
  - `person_key = identity.person_key_qq(qq_uin, bound_user_id)`；
  - 角色候选：`role_for(person_key)`；若已绑定，再加 `role_for(f"qq:{qq_uin}")`；取 `ROLE_ORDER = ("normal","special","exception","owner")` 中索引最大的。
- `load_signals`：`get_or_create_state` → `decay_state` → `save_state`，返回 `TurnSignals(state, affinity_for(...))`。
- `record_reply`：`apply_event(kind="chat", d_affection_units=chat_affection_units, d_valence=chat_valence, d_energy=-fatigue_per_turn, touch_reply=True)`。
- `record_silence`：`apply_event(kind="ignored", d_debt=debt_increment)`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_dynamics.py tests/test_relationship_turn.py tests/test_relationship_state.py -v`
Expected: PASS。

---

### Task 4: 多气泡切分与打字节奏

**Files:**
- Create: `backend/app/channels/qq/pacing.py`
- Test: `backend/tests/test_qq_pacing.py`

**Interfaces:**
- Produces:
  - `segment_bubbles(text: str, dynamics: Dynamics, *, rng: random.Random) -> list[str]`
  - `bubble_delay(previous: str, dynamics: Dynamics, *, rng: random.Random) -> float`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import random

import pytest

from app.channels.qq.pacing import bubble_delay, segment_bubbles
from app.persona.models import PersonaVersion


def dynamics(**overrides):
    data = {"schema": "l2d.persona/v1", "id": "merlin", "version": 1, "name": "梅林", "core": "C"}
    if overrides:
        data["dynamics"] = overrides
    return PersonaVersion.model_validate(data).dynamics


def test_short_text_stays_one_bubble():
    d = dynamics()
    assert segment_bubbles("我在。", d, rng=random.Random(0)) == ["我在。"]


def test_long_text_splits_on_sentence_boundaries():
    d = dynamics()
    text = "今天风有点大。我出门的时候差点被吹回去。你那边下雨了吗？记得带伞。"
    bubbles = segment_bubbles(text, d, rng=random.Random(3))
    assert len(bubbles) >= 2
    assert "".join(bubbles).replace(" ", "") == text.replace(" ", "")
    assert all(b.strip() for b in bubbles)


def test_never_splits_inside_code_or_url():
    d = dynamics()
    text = "看这个 https://example.com/a/b/c 和 `print(1)` 都别拆。后面还有一句话。再一句。"
    bubbles = segment_bubbles(text, d, rng=random.Random(1))
    joined = "".join(bubbles)
    assert "https://example.com/a/b/c" in joined
    assert "`print(1)`" in joined
    for bubble in bubbles:
        assert bubble.count("`") % 2 == 0


def test_tiny_segments_are_merged():
    d = dynamics()
    text = "好。嗯。就这样吧，明天见。"
    bubbles = segment_bubbles(text, d, rng=random.Random(2))
    assert all(len(b) >= d.pacing.min_bubble_chars for b in bubbles)


def test_delay_is_bounded():
    d = dynamics()
    delay = bubble_delay("今天风有点大。", d, rng=random.Random(0))
    assert d.pacing.min_gap_ms / 1000 <= delay <= 10.0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_pacing.py -v`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现**

- 先保护区间：围栏代码 ```...```、行内代码 `...`、URL `https?://\S+`。
- 候选切点及权重：段落（`\n\n` 后）1.0、换行 0.9、句末 `。！？!?…` 0.85、`；;` 0.45、`：:` 0.3、`，,` 0.18；落在保护区间内的切点丢弃。
- `k = clamp(round(len(text) / target_chars_per_bubble), 1, max_bubbles)`；需要 `k-1` 个切点，按权重无放回抽取（`rng.choices` 抽一个后移除，或按权重排序后加扰动；必须可复现）。
- 选完后按位置切开；任一段 `< min_bubble_chars` 时并入相邻段（优先并到前一段）。
- 去空白、去空段；只剩一段就直接返回。
- `bubble_delay`：`len(previous) / chars_per_second` + 句末额外 `rng.uniform(0.2, 0.4)` + `rng.uniform(min_gap_ms, max_gap_ms) / 1000`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_pacing.py -v`
Expected: PASS。

---

### Task 5: 防抖与打断协调器

**Files:**
- Create: `backend/app/channels/qq/conversation.py`
- Test: `backend/tests/test_qq_conversation.py`

**Interfaces:**
- Produces:
  - `ConversationCoordinator()`：无参数，内部按 key 存状态
  - `async submit(key, text, *, handler, debounce, max_debounce) -> dict`：handler 签名 `async (texts: list[str]) -> dict`
  - `async drain() -> None`：等待所有会话空闲（测试用）

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import asyncio
import functools
import time

from app.channels.qq.conversation import ConversationCoordinator


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


@async_test
async def test_debounce_merges_messages_into_one_round():
    coordinator = ConversationCoordinator()
    seen: list[list[str]] = []

    async def handler(texts):
        seen.append(texts)
        return {"handled": True, "round": len(seen)}

    first = asyncio.create_task(
        coordinator.submit("k", "第一句", handler=handler, debounce=0.05, max_debounce=0.2)
    )
    await asyncio.sleep(0.01)
    second = asyncio.create_task(
        coordinator.submit("k", "第二句", handler=handler, debounce=0.05, max_debounce=0.2)
    )
    results = await asyncio.gather(first, second)

    assert seen == [["第一句", "第二句"]]
    assert results[0] == results[1]


@async_test
async def test_new_message_interrupts_running_round_and_merges():
    coordinator = ConversationCoordinator()
    seen: list[list[str]] = []
    started = asyncio.Event()

    async def handler(texts):
        seen.append(texts)
        if len(seen) == 1:
            started.set()
            await asyncio.sleep(10)  # 会被打断
        return {"handled": True, "round": len(seen)}

    first = asyncio.create_task(
        coordinator.submit("k", "长回复", handler=handler, debounce=0, max_debounce=0)
    )
    await started.wait()
    second = asyncio.create_task(
        coordinator.submit("k", "新消息", handler=handler, debounce=0, max_debounce=0)
    )
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["reason"] == "interrupted"
    assert seen[-1] == ["长回复", "新消息"]
    assert second_result["handled"] is True


@async_test
async def test_drain_waits_for_workers():
    coordinator = ConversationCoordinator()

    async def handler(texts):
        await asyncio.sleep(0.02)
        return {"handled": True}

    await coordinator.submit("k", "x", handler=handler, debounce=0, max_debounce=0)
    await coordinator.drain()
    assert coordinator.pending("k") is False
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_conversation.py -v`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现**

每个 key 一个 `_Conversation`：`queue: list[str]`、`waiters: list[Future]`、`first_at`、`last_at`、`event: asyncio.Event`、`worker: Task|None`、`round_task: Task|None`。

- `submit`：把 text 入队、更新 `last_at` / `first_at`、`event.set()`；若 `round_task` 正在跑就 `cancel()`；没有 worker 就创建；为本次提交建一个 Future 并 await。
- worker 循环：
  1. 若队列空 → 退出并清理；
  2. 防抖等待：`remaining = debounce - (now - last_at)`，若 `now - first_at >= max_debounce` 立即结束；用 `asyncio.wait_for(event.wait(), remaining)`，超时/被唤醒后清 event 再判断；
  3. 取走全部队列文本和 waiters，重置 `first_at`；
  4. `round_task = create_task(handler(texts))`；`await` 它；
  5. 正常完成 → 用结果 resolve 本轮的 waiters；`CancelledError`（被新消息打断）→ 把本轮 texts 重新插回队首，waiters resolve 为 `{"handled": True, "reason": "interrupted"}`，继续循环；若是协调器关闭则原样抛出。
- `pending(key)`：队列非空或 worker 未结束。
- `drain()`：循环 await 所有 worker，直到没有活动会话。

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_conversation.py -v`
Expected: PASS。

---

### Task 6: 接入 QQ 适配器

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/channels/qq/adapter.py`
- Modify: `backend/app/memory/pipeline.py`（新增 `record_qq_ignored`）
- Test: `backend/tests/test_qq_private_chat.py`
- Test: `backend/tests/test_qq_relationship_flow.py`

**Interfaces:**
- Consumes: Task 1–5 全部；`get_persona_store().load_active()`；`pipeline.qq_memory_context` / `record_qq_turn`。
- Produces:
  - `Settings.qq_private_reply_policy`（`probabilistic` / `always` / `silent`，默认 `probabilistic`，非法值回退 `always`）
  - `QQAdapter(..., persona=None, rng=None, coordinator=None)`
  - `pipeline.record_qq_ignored(qq_uin, group_id, binding, text) -> int | None`（只写用户事件，不写助手事件）
  - `QQAdapter.handle_event` 私聊路径返回 `reason`：`"reply"` / `"silent"` / `"interrupted"` / 原有固定原因

- [ ] **Step 1: 写失败测试**

在 `tests/test_qq_private_chat.py` 的 `make_adapter` 里加参数：`policy="always"`，并注入 `persona` 与固定种子 RNG、零延迟节奏：

```python
def make_persona(debounce=0.0, max_debounce=0.0):
    return PersonaVersion.model_validate({
        "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
        "name": "梅林", "core": "C",
        "dynamics": {
            "namespace": {"state": "merlin"},
            "pacing": {
                "debounce_seconds": debounce,
                "debounce_max_seconds": max_debounce,
                "max_gap_ms": 0,
                "min_gap_ms": 0,
                "chars_per_second": 1000,
            },
        },
    })
```

在 `make_adapter` 中：`settings = Settings(..., qq_private_reply_policy=policy)`，`QQAdapter(..., persona=make_persona(), rng=random.Random(0))`。

新建 `tests/test_qq_relationship_flow.py`：

```python
# 复用 test_qq_private_chat 的 fake 结构，关键断言：
# 1. 概率为 0（floor=0、willingness 极低、energy=0）时私聊返回 silent，sender.private 为空，
#    decision_log 有一行 verdict=silent，relationship_state.reply_debt 增加。
# 2. 下一次同一人再发消息时 p 比上一次高（reply_debt 起作用）。
# 3. 生成中收到第二条消息：第一个 handle_event 返回 interrupted，最终 sender 收到包含
#    两条消息的合并回复（用可控 stream 在 yield 前 await 一个 Event）。
# 4. 关系库不可用（monkeypatch db.connect 抛异常）时仍然回复，reason 为 "reply"。
# 5. 危机词（"救命"）在 policy="silent" 下仍然回复。
```

测试用 `dynamics.reply.floor = 0.0`、`ceiling = 0.0` 让概率恒为 0；通过私聊进入决策。

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_relationship_flow.py tests/test_qq_private_chat.py -v`
Expected: FAIL，`qq_private_reply_policy` 不存在或私聊仍固定回复。

- [ ] **Step 3: 实现配置与 pipeline**

- `Settings` 加 `qq_private_reply_policy: str = "probabilistic"` 和 property：

```python
@property
def qq_private_policy(self) -> str:
    value = self.qq_private_reply_policy.strip().lower()
    return value if value in {"always", "probabilistic", "silent"} else "always"
```

- `pipeline.record_qq_ignored`：结构照抄 `record_qq_turn` 的前半段，只写用户事件；失败返回 `None`。

- [ ] **Step 4: 实现适配器编排**

`__init__` 增加 `persona`、`rng`、`coordinator`；`self._dynamics()`：优先注入的人设，否则 `get_persona_store().load_active()`，异常时回退 `Dynamics()` 并 `persona_key = settings.persona_id`。

`handle_event` 私聊路径改为：

1. 命令、未绑定引导、空文本、去重保持同步；这些回复仍走 `_send(..., reason=...)`，不延迟、不受防抖影响。
2. 其余正文交给 `self._coordinator.submit(key, content, handler=self._private_round, debounce=..., max_debounce=...)`，`debounce` 取 `dynamics.pacing`。
3. `_private_round(texts)`（一个合并回合）：
   - `content = "\n".join(texts)`
   - 取绑定与管理员身份；`policy = settings.qq_private_policy`
   - `crisis = any(word in content for word in dynamics.safety.crisis_keywords)`
   - 关系与决策（整段 try/except，异常 → `force_reply=True`）：
     - `conn = db.connect()`；`person_key, role = relationship_subject(...)`
     - `signals = load_signals(...)`
     - `decision = decide(..., force_reply=crisis or policy != "probabilistic")`
     - `record_decision(...)`
   - 若 `not decision.reply`：`record_silence(...)`、`pipeline.record_qq_ignored(...)`、关连接，返回 `{"handled": True, "reason": "silent", "reply": ""}`
   - 回复路径：额度预占（一次性）→ 用尽则发固定文案并返回；记忆检索；`_reply`；成功后 `record_reply(...)`、`record_qq_turn`、`schedule_extraction`；失败退款。
   - 发送用 `_send(..., reason=None)` 触发多气泡与延迟。
   - 若本轮被取消（`CancelledError`）：把已经生成的半截正文写进 `_history`（如果有），再原样抛出。
4. 群聊路径保持现有同步逻辑，只把公共部分抽成 `_run_chat_turn` 供两边复用。

`_send`：

```python
chunks = self._split_text(reply) if reason or incoming.message_type != "private" \
    else segment_bubbles(reply, dynamics, rng=self._rng)
for index, chunk in enumerate(chunks):
    if index:
        await asyncio.sleep(bubble_delay(chunks[index - 1], dynamics, rng=self._rng))
    await send(...)
```

总延迟超过 5.5 秒后不再等待（仍发送剩余气泡）。

- [ ] **Step 5: 更新现有测试**

`test_qq_private_chat.py` 的 `make_adapter` 注入零防抖人设、`policy="always"`、固定 RNG；断言保持不变。若 `test_qq_adapter.py` 的私聊用例受新默认策略影响，也改成显式 `qq_private_reply_policy="always"`。

- [ ] **Step 6: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qq_private_chat.py tests/test_qq_relationship_flow.py tests/test_qq_adapter.py tests/test_qq_quota_flow.py -v`
Expected: PASS。

- [ ] **Step 7: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: 全部通过（现有 329 passed / 1 skipped + 新增用例）。

---

### Task 7: 文档与示例

**Files:**
- Modify: `backend/personas.example/README.md`
- Modify: `docs/superpowers/plans/2026-11-08-04a-private-interaction-layer.md`

- [ ] **Step 1: README 补充**

在 `dynamics` 行之后加说明：`relationship.chat_affection_units` 和 `chat_valence` 是每轮正常回复的关系变化；忽略一条有内容的私聊只增加 `reply.debt_increment` 的回复债。

- [ ] **Step 2: 写入执行结果**

全量测试通过后，在计划末尾追加 `## 执行结果（2026-11-08）`，记录测试命令、通过数、改动文件、未做项（群聊概率回复/待续话题归 4b）。

---

## 完成标准

- 私聊按 `p0`/`p` 公式掷骰子；沉默不发送、不退化成错误回复。
- 防抖合并、生成中打断、多气泡与延迟都由可注入的纯函数/协调器完成并有测试。
- 决策与关系写入失败时私聊仍回复。
- 群聊行为与现有测试保持一致。
- 没有提交或推送。

## 执行结果（2026-11-08）

全部 7 个任务已在本地完成，**未提交、未推送、未部署**。

- 测试命令：`./.venv/Scripts/python.exe -m pytest -q`
- 结果：**359 passed, 1 skipped**（4a 新增 21 个用例，覆盖决策公式、防抖合并、打断、切分、关系回合、端到端私聊流程、额度退还与人设加载失败降级）

### 新增文件

- `backend/app/relationship/decision.py` — 意愿评估、`p0`/`p` 公式、`decide`、`record_decision`
- `backend/app/relationship/turn.py` — 主体解析、状态读取、回复/沉默事件
- `backend/app/channels/qq/pacing.py` — 按标点和字数切气泡、打字延迟
- `backend/app/channels/qq/conversation.py` — 按会话键防抖合并与打断协调器
- `backend/tests/test_reply_decision.py`、`test_relationship_turn.py`、`test_qq_pacing.py`、`test_qq_conversation.py`、`test_qq_relationship_flow.py`

### 修改文件

- `backend/app/memory/db.py` — 新增 `decision_log` 表
- `backend/app/memory/pipeline.py` — `record_qq_ignored`：沉默时只记用户事件
- `backend/app/channels/qq/adapter.py` — 私聊走协调器 + 概率决策；多气泡发送；打断时保留半截上下文；被打断回合退还额度；人设加载失败降级
- `backend/app/persona/models.py` — `chat_affection_units`、`chat_valence`
- `backend/app/config.py`、`backend/.env.example` — `QQ_PRIVATE_REPLY_POLICY`
- `backend/personas.example/README.md` — 每轮对话关系信号说明
- 既有测试适配：`test_memory_db.py`、`test_persona_dynamics.py`、`test_qq_private_chat.py`、`test_qq_quota_flow.py`

### 未做项（按 spec 归入后续阶段）

- 待续话题（pending follow-up）与主动续聊：4b
- 群聊的概率回复与节奏：4b
- 记忆表按 `persona_key` 分列：不在 spec 4a 范围，留待后续
- `decision_log` 的运维查看/清理工具：暂无，表随记忆库一起保留
