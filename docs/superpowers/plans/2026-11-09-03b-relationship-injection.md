# 3b 关系完整化与状态注入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把好感改成 -100~100 的可负值慢变量，补上 missing / security / patience / attention 的衰减，让耐心和安全感影响回复概率，并把定性关系状态注入私聊提示词；同时支持不改版本文件就能调参的 `dynamics.override.json`。

**Architecture:** `app/relationship/state.py` 仍是纯计算 + SQLite 读写层；`affinity_for()` 用 tanh 把 affection 映射成回复倍率；`decay_state()` 负责按天/半衰期更新慢变量。`app/relationship/context.py` 把状态渲染成只读中文背景，经 `composer → agent → adapter` 注入私聊 system prompt。`PersonaStore.load()` 在版本文件之后深合并可选覆盖层并整份校验。

**Tech Stack:** Python 3.11+、Pydantic v2、SQLite、pytest。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（4.3、4.4、4.5、5.3、9、10.1）

## Global Constraints

- 好感范围 `-100 ~ +100`；非管理员角色初始 `0`，管理员初始 `60`。
- 正面增长乘角色成长倍率，并随接近角色上限递减；负面变化不乘成长倍率。
- `affinity = clamp(mid + amp × tanh(affection / scale), min, max)`，默认 `0.70 / 0.60 / 60 / 0.10 / 1.20`。
- 慢变量按天变化：`missing +6`、`security -1`、`patience +2`；`attention_confidence` 半衰期 24 小时。
- `patience` / `security` 只做乘性下限（`0.80` / `0.85`），不把概率压到 0；危机、命令、额度提示仍然绕过概率。
- 状态提示词是**只读背景**，不是指令；危机关键词永远优先。
- 覆盖层深合并后整份校验；任何错误回退到版本基线，不能让人设加载失败。
- 适配器不缓存人设，覆盖文件保存后下一轮立即生效。
- 在 `backend/` 目录下用项目虚拟环境运行：`.venv/Scripts/python.exe -m pytest`。
- **不要提交或推送**，除非用户明确要求。

## Review Focus

- 好感为负时，正面事件必须全额生效（不因“接近上限”被误伤），负面事件也不能被成长倍率放大。
- 管理员已高于上限的旧数据不能被强行拉回；上限只限制增长。
- 时间差为负、为零或时间戳损坏时，衰减不能抛异常。
- 覆盖层字段名拼错、数值越界、JSON 语法错误都必须整份回退，不能半份生效。
- 低耐心/低安全感只应降低概率，不能把危机回复也压掉。
- 状态提示词不能泄露裸数值，也不能让模型把“只读背景”当成用户指令。

---

### Task 1: 好感范围、角色初始值与成长曲线

**Files:**
- Modify: `backend/app/persona/models.py`
- Modify: `backend/app/relationship/state.py`
- Test: `backend/tests/test_persona_dynamics.py`、`backend/tests/test_relationship_state.py`

**Interfaces:**
- Consumes: `RelationshipDynamics`、`RoleDynamics`、`apply_event()`。
- Produces:
  - `RoleDynamics.initial: float = Field(ge=-100.0, le=100.0)`
  - `RelationshipDynamics.growth_curve: float = 0.8`
  - 默认角色表：`owner 60/100/1.0`、`exception 0/85/0.9`、`special 0/75/0.8`、`normal 0/60/0.6`
  - `apply_event()` 正面增长按 `(1 - max(affection, 0) / cap) ** growth_curve` 递减

- [x] **Step 1: 写失败测试**

```python
def test_roles_start_at_zero_except_owner():
    roles = Dynamics().relationship.roles
    assert roles["owner"].initial == 60
    assert [roles[name].initial for name in ("exception", "special", "normal")] == [0, 0, 0]


def test_positive_growth_diminishes_near_cap(tmp_path):
    # 0 分 10 单位 → 6.0；接近 60 上限时同量增长明显更小
    ...
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py tests/test_persona_dynamics.py -v`
Expected: FAIL，旧初始值与旧幂次曲线不匹配。

- [x] **Step 3: 改模型与状态层**

改默认角色表、`initial` 取值范围，加入 `growth_curve`；`apply_event()` 用 `ratio = clamp(max(affection, 0) / cap, 0, 1)`，正面增长乘 `(1 - ratio) ** growth_curve`，负面不乘。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py tests/test_persona_dynamics.py -v`
Expected: PASS。

---

### Task 2: affinity 改为 tanh 曲线

**Files:**
- Modify: `backend/app/persona/models.py`
- Modify: `backend/app/relationship/state.py`
- Test: `backend/tests/test_relationship_state.py`、`backend/tests/test_persona_dynamics.py`

**Interfaces:**
- Consumes: `ReplyDynamics`。
- Produces:
  - `ReplyDynamics.affinity_mid/amp/scale/min/max`（移除 `affinity_curve`）
  - `affinity_for(affection, dynamics) -> float` 使用 tanh

- [x] **Step 1: 写失败测试**

```python
def test_affinity_uses_tanh_curve(dynamics):
    assert affinity_for(0, dynamics) == pytest.approx(0.70, abs=0.01)
    assert affinity_for(100, dynamics) == pytest.approx(1.20, abs=0.01)
    assert affinity_for(-100, dynamics) == pytest.approx(0.14, abs=0.01)
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py::test_affinity_uses_tanh_curve -v`
Expected: FAIL，旧幂次曲线在 0 分处不是 0.70。

- [x] **Step 3: 实现 tanh 曲线**

`value = mid + amp * math.tanh(affection / scale)`，再 clamp 到 `[min, max]`；删除不再使用的 `affinity_curve` 字段。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py -v`
Expected: PASS。

---

### Task 3: missing / security / patience / attention 衰减

**Files:**
- Modify: `backend/app/persona/models.py`
- Modify: `backend/app/relationship/state.py`
- Test: `backend/tests/test_relationship_state.py`

**Interfaces:**
- Consumes: `RelationshipDynamics`、`decay_state()`。
- Produces:
  - `initial_missing=50`、`initial_security=70`、`initial_patience=70`
  - `missing_gain_per_day=6`、`security_loss_per_day=1`、`patience_recovery_per_day=2`
  - `attention_half_life_hours=24`

- [x] **Step 1: 写失败测试**

```python
def test_one_day_decay_moves_slow_variables(dynamics):
    before = make_state(missing=50, security=70, patience=70, attention_confidence=0.8)
    after = decay_state(before, dynamics, now=one_day_later)

    assert after.missing == pytest.approx(56)
    assert after.security == pytest.approx(69)
    assert after.patience == pytest.approx(72)
    assert after.attention_confidence == pytest.approx(0.4)
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py::test_one_day_decay_moves_slow_variables -v`
Expected: FAIL，旧实现只衰减 valence / reply_debt / fatigue。

- [x] **Step 3: 实现衰减**

在 `decay_state()` 中按 `elapsed_days` 更新 missing / security / patience；attention 用半衰期 `0.5 ** (小时 / half_life)`；全部 clamp 到合法区间。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_state.py -v`
Expected: PASS。

---

### Task 4: 耐心与安全感影响回复概率

**Files:**
- Modify: `backend/app/persona/models.py`
- Modify: `backend/app/relationship/decision.py`
- Test: `backend/tests/test_reply_decision.py`

**Interfaces:**
- Consumes: `reply_probability()`、`decide()`。
- Produces:
  - `ReplyDynamics.patience_multiplier_min = 0.80`
  - `ReplyDynamics.security_multiplier_min = 0.85`
  - `reply_probability(..., patience=100.0, security=100.0)`
  - `decide(..., patience=100.0, security=100.0)`

- [x] **Step 1: 写失败测试**

```python
def test_low_patience_and_security_lower_probability(dynamics):
    high = reply_probability(..., patience=100, security=100)
    low = reply_probability(..., patience=0, security=0)
    assert low[0] == pytest.approx(high[0] * 0.80 * 0.85)
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py::test_low_patience_and_security_lower_probability -v`
Expected: FAIL，函数没有这两个参数。

- [x] **Step 3: 实现乘性下限**

`factor = floor + (1 - floor) * clamp(value / 100, 0, 1)`；`p0` 依次乘 patience / security 因子。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_reply_decision.py -v`
Expected: PASS。

---

### Task 5: 渲染只读关系状态

**Files:**
- Create: `backend/app/relationship/context.py`
- Test: `backend/tests/test_relationship_context.py`

**Interfaces:**
- Consumes: `RelationshipState`。
- Produces: `render_state_context(state: RelationshipState) -> str`

- [x] **Step 1: 写失败测试**

```python
def test_neutral_state_uses_plain_labels():
    text = render_state_context(make_state())
    assert "陌生人" in text
    assert "平静" in text
    assert "只读背景" in text
    assert "危机" in text


def test_low_energy_and_negative_affection_are_described():
    text = render_state_context(make_state(energy=0.1, affection=-50, valence=-0.6, patience=20, security=20))
    assert "精力很低" in text
    assert "反感" in text
    assert "语气会冷" in text
    assert "耐心快耗尽" in text
    assert "缺乏安全感" in text
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_context.py -v`
Expected: FAIL，`app.relationship.context` 不存在。

- [x] **Step 3: 实现渲染**

按 affection / valence / energy / missing / patience / security 给出定性标签；低精力追加“说话更短，更容易想结束对话”，高好感 + 高精力追加“愿意多聊几句”；末尾固定写明危机优先。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relationship_context.py -v`
Expected: PASS。

---

### Task 6: 把状态注入私聊提示词

**Files:**
- Modify: `backend/app/persona/composer.py`
- Modify: `backend/app/agent/core.py`
- Modify: `backend/app/channels/qq/adapter.py`
- Test: `backend/tests/test_prompts.py`、`backend/tests/test_qq_private_chat.py`、`backend/tests/test_qq_adapter.py`

**Interfaces:**
- Consumes: `render_state_context()`。
- Produces:
  - `compose_system_prompt(..., state_context=None)` / `build_system_prompt(..., state_context=None)`
  - `_build_agent(..., state_context=None)` / `stream_reply(..., state_context=None)`
  - `_decide_private()` 返回 `(person_key, role, decision, state)`
  - `_run_chat_turn(..., state_context=None)` / `_reply(..., state_context=None)`

- [x] **Step 1: 写失败测试**

```python
def test_state_context_sits_between_memory_and_page():
    prompt = build_system_prompt(..., memory_context="MEMORY_MARKER", state_context="STATE_MARKER")
    assert prompt.index("MEMORY_MARKER") < prompt.index("STATE_MARKER")
    assert prompt.index("STATE_MARKER") < prompt.index("用户当前所在页面")


@async_test
async def test_private_reply_receives_state_context(tmp_path):
    ...
    assert "当前关系状态" in captured["state_context"]
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py tests/test_qq_private_chat.py -v`
Expected: FAIL，参数不存在。

- [x] **Step 3: 串联参数**

`composer` 在 memory 之后、page 之前插入非空 `state_context`；`agent` 透传；适配器让 `_decide_private()` 返回决策时用的状态快照，渲染后随本轮传给 `_run_chat_turn → _reply → _stream`。群聊不注入状态。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py tests/test_qq_private_chat.py tests/test_qq_adapter.py -v`
Expected: PASS；测试假 stream 需要接受 `state_context` 关键字。

---

### Task 7: `dynamics.override.json` 与适配器缓存失效

**Files:**
- Modify: `backend/app/persona/store.py`
- Modify: `backend/app/channels/qq/adapter.py`
- Test: `backend/tests/test_persona_store.py`、`backend/tests/test_qq_private_chat.py`

**Interfaces:**
- Consumes: `PersonaStore.load_active()`。
- Produces:
  - 深合并 `persona.dynamics.model_dump(mode="json")` 与 `<persona_dir>/dynamics.override.json`
  - 合并后 `Dynamics.model_validate()`；失败整份回退并告警
  - `QQAdapter._persona()` 每轮重新 `load_active()`，移除 `_persona_cache`

- [x] **Step 1: 写失败测试**

```python
def test_dynamics_override_is_deep_merged(tmp_path):
    write_override(tmp_path, "merlin", {"reply": {"base": 0.5}})
    persona = PersonaStore(tmp_path, "merlin").load("merlin", 1)
    assert persona.dynamics.reply.base == 0.5
    assert persona.dynamics.reply.ceiling == 0.95


def test_invalid_dynamics_override_falls_back_to_base(tmp_path):
    write_override(tmp_path, "merlin", {"reply": {"base": 5.0}})
    assert PersonaStore(tmp_path, "merlin").load("merlin", 1).dynamics.reply.base == 0.95
```

- [x] **Step 2: 运行测试，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_store.py -v`
Expected: FAIL，覆盖层被忽略。

- [x] **Step 3: 实现加载与失效**

`_deep_merge()` 递归合并、列表整体替换；覆盖文件不存在直接返回基线；读取/解析/校验失败只告警。适配器每轮重新加载人设（文件很小），删掉永久缓存。

- [x] **Step 4: 运行测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_persona_store.py tests/test_qq_private_chat.py -v`
Expected: PASS。

---

### Task 8: 文档与全量回归

**Files:**
- Modify: `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`
- Modify: `backend/personas.example/README.md`

- [x] **Step 1: 修正设计文档**

4.3 成长公式改为 `ratio = clamp(max(affection,0)/cap, 0, 1)`；5.3 公式加入 patience / security 因子；10.1 JSON 补 `energy_delta_cap`、两个乘性下限、初始值与按天衰减字段；第 9 节标记 3a.1 完成、3b 进行中。

- [x] **Step 2: 补充覆盖层说明**

在人设 README 写清目录位置、深合并语义、整份校验回退、无需重启、删除即恢复默认。

- [x] **Step 3: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `383 passed, 1 skipped`（3b 完成时）。
