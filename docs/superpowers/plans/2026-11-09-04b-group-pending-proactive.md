# 4b 群策略、指名判断、待续话题与主动调度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让梅林在管理员允许的范围内主动开口：按群配置触发/回复/主动策略；群里被绑定的成员点名时，用她与这个人的私人关系状态决定回不回、带多少熟悉感；把“因累或心情低而没回”的话留成待续话题；用独立预算定时生成并发送主动消息，全部决策可审计。

**Architecture:** 新增 `app/proactive/` 包，按职责拆成待续话题（`pending.py`）、候选评分（`candidates.py`）、发送前检查（`checks.py`）、日志与事件（`store.py`）、消息生成（`message.py`）、忽略结算（`settle.py`）、单次调度（`scheduler.py`）、常驻循环（`runner.py`）。群相关的三件事各自独立：策略存储 `app/channels/qq/group_policy.py`、指名后的关系判断 `app/relationship/group_turn.py`、群语气渲染 `render_group_state_context`。适配器只负责“读策略 → 判断是否指名 → 记录或交给下游”。调度器由 FastAPI `lifespan` 启动，仅在 `QQ_ENABLED=true` 时运行；所有新路径 fail-open。

**Tech Stack:** Python 3.11+、FastAPI、LangChain `ChatDeepSeek`、SQLite、pytest、WordPress PHP 设置页。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（§4.2、§5.2、§5.4、§5.5、§6、§8、§9）

## Global Constraints

- **不提交、不推送。** 每个任务以定向测试或全量测试通过结束；提交由站长在讨论后决定。
- 不新增第三方依赖；不引入后台任务队列。主动调度依赖单 worker 的 uvicorn（现有部署即如此），不实现跨进程锁。
- 所有时间戳存 UTC ISO（`now_iso()`）；“今天/本周”按 `QUOTA_TIMEZONE`（默认 `Asia/Shanghai`）划分。
- 主动消息默认**关闭**（`proactive_enabled=False`）；只有管理员能打开，用户不能关闭。
- 主动消息不占用户聊天额度，也不调用 `QQQuotaGate`。
- 任何新增的“决策/生成/发送”路径都必须 fail-open：数据库异常、模型超时、发送失败都只记日志，不影响被动回复。
- **“指名” = 消息 @ 了梅林，或以 `QQ_PREFIX` 开头。** 只有被绑定成员指名时才进入关系判断；`trigger=all` 带进来的普通群消息不算指名，仍走规则回复。
- **群内不套用私聊的沉默空间。** 被指名时仍用 `decide()` 的 `p0`，但把概率抬到 `max(p, dynamics.group.reply_floor)`（默认 **0.90**）。平时几乎必回，只有冲突 `withdrawing` / `cold`、好感为负、心情很差或正对这个人赌气这类极端状态才可能不回。
- **群内指名判断只读关系状态**：不改 `affection` / `missing` / `reply_debt` / `last_reply_at`，也不叠加聊天好感。时间衰减与冲突阶段推进照常。
- **群提示词里绝不出现私聊事件原文。** 跨渠道只传 `render_group_state_context` 的定性标签，不传任何私聊消息内容。
- 群策略只允许 `always` / `silent`，不提供 `probabilistic`；非指名的群消息不进概率模型。
- **群聊里主动开私聊（group opener）本阶段不做**，记入 spec §9 的候选，等站长试用后再决定。
- 旧 `QQ_TRIGGER=at` 映射为 `mention`，即“@ 或前缀”都触发；这是有意的行为变更。
- 代码注释、docstring、后台文案用中文，风格与现有模块一致。
- 测试命令统一在仓库根目录执行：`cd backend && .venv/Scripts/python.exe -m pytest tests/<file> -v`。
- 本地没有 PHP，WordPress 插件改动只能人工核对，不能跑 `php -l`。

## Review Focus

1. **群策略行损坏或未知值** → 不能让群“突然变哑”或突然对每条消息回复，必须回退到 `QQ_TRIGGER` 推导的默认策略。
2. **私聊内容泄漏到群提示词** → 同一个人在群里被点名时，模型只能看到关系标签，不能看到私聊原文；提示词和记忆检索都必须按渠道隔离。
3. **主动消息发送失败** → 必须写 `proactive_log(status='failed')` 且不重试轰炸，不能把 `last_proactive_at` 或待续话题状态改坏。
4. **用户刚刚说过话** → 主动必须让位给正常回复（跳过并记 `recently_active`），不能在用户在线时插一条无关的主动消息。
5. **QQ 解绑/换绑** → 旧关系状态不得继续向该 QQ 号发消息；绑定不一致时静默跳过。
6. **免打扰跨午夜**（如 `23:00-07:00`）→ 23:30 和 06:59 都必须判为免打扰，12:00 不能。
7. **点名识别** → `@` 和前缀都必须算指名；`trigger=all` 下的普通文本不能被当成指名，否则关系判断会覆盖规则回复。

---

### Task 1: 三张新表与迁移

**Files:**
- Modify: `backend/app/memory/db.py:22-199`（`_SCHEMA` 字符串末尾追加三张表与索引）
- Test: `backend/tests/test_proactive_db.py`

**Interfaces:**
- Produces: 表 `proactive_log`、`pending_topics`、`qq_group_policies`；`db.stats()` 的返回键保持 `{"events","memories","persons"}` 不变。

- [ ] **Step 1: 写失败的建表测试**

```python
# tests/test_proactive_db.py
from __future__ import annotations
from types import SimpleNamespace
import pytest
from app.memory import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4),
    )
    db.init_schema()
    conn = db.connect()
    yield conn
    conn.close()


def test_new_tables_exist(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"proactive_log", "pending_topics", "qq_group_policies"} <= tables


def test_stats_stays_user_facing(conn):
    assert db.stats(conn) == {"events": 0, "memories": 0, "persons": 0}


def test_proactive_log_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(proactive_log)")}
    assert cols == {
        "id", "persona_key", "target_key", "channel_id", "kind", "thought",
        "score", "threshold", "status", "reason", "event_id", "created_at",
    }


def test_pending_topics_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(pending_topics)")}
    assert cols == {
        "id", "persona_key", "person_key", "topic", "reason", "status",
        "created_at", "expires_at", "sent_at",
    }


def test_group_policy_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(qq_group_policies)")}
    assert cols == {"group_id", "trigger", "reply_policy", "proactive", "updated_at"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_db.py -v`
Expected: FAIL（表不存在，`PRAGMA table_info` 返回空）。

- [ ] **Step 3: 在 `_SCHEMA` 末尾追加**

```sql
-- 每次主动决策的审计：念头、分数、阈值、结果。
CREATE TABLE IF NOT EXISTS proactive_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    thought TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    threshold REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    event_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proactive_log_target
    ON proactive_log(persona_key, target_key, id);

-- 因累/忙/心情低而没接的话。属于主动消息，不另建调度器。
CREATE TABLE IF NOT EXISTS pending_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_key TEXT NOT NULL,
    person_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_topics_person
    ON pending_topics(persona_key, person_key, status, id);

-- 每个群的三个正交开关。缺行时回退到 QQ_TRIGGER。
CREATE TABLE IF NOT EXISTS qq_group_policies (
    group_id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    reply_policy TEXT NOT NULL,
    proactive INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
```

`db.stats()` 不要改；它只返回面向用户的计数。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_db.py tests/test_memory_db.py -v`
Expected: PASS。

---

### Task 2: 群策略存储与触发匹配

**Files:**
- Create: `backend/app/channels/qq/group_policy.py`
- Test: `backend/tests/test_qq_group_policy.py`

**Interfaces:**
- Produces:
  - `GroupPolicy(trigger: str, reply_policy: str, proactive: bool)`（frozen dataclass）
  - `TRIGGERS = ("mention", "keyword", "all")`，`REPLY_POLICIES = ("always", "silent")`
  - `default_policy(trigger_setting: str) -> GroupPolicy`
  - `load_policy(conn, group_id: int, *, fallback: GroupPolicy) -> GroupPolicy`
  - `save_policy(conn, group_id: int, policy: GroupPolicy, *, now: str | None = None) -> GroupPolicy`（`commit=True`）
  - `TriggerMatch(content: str, addressed: bool)`（frozen dataclass）
  - `match_trigger(text: str, at_self: bool, *, trigger: str, prefix: str = "梅林") -> TriggerMatch`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_qq_group_policy.py
from __future__ import annotations
from types import SimpleNamespace
import pytest
from app.channels.qq.group_policy import (
    GroupPolicy, TriggerMatch, default_policy, load_policy, match_trigger, save_policy,
)
from app.memory import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4),
    )
    db.init_schema()
    conn = db.connect()
    yield conn
    conn.close()


def test_legacy_trigger_maps_to_spec_names():
    assert default_policy("at").trigger == "mention"
    assert default_policy("prefix").trigger == "keyword"
    assert default_policy("all").trigger == "all"
    assert default_policy("bogus").trigger == "mention"


def test_missing_row_returns_fallback(conn):
    fallback = GroupPolicy("mention", "always", False)
    assert load_policy(conn, 888, fallback=fallback) == fallback


def test_save_and_load_roundtrip(conn):
    saved = save_policy(conn, 888, GroupPolicy("all", "silent", True))
    assert saved == GroupPolicy("all", "silent", True)
    assert load_policy(conn, 888, fallback=default_policy("at")) == saved


def test_corrupt_row_falls_back(conn):
    conn.execute(
        "INSERT INTO qq_group_policies VALUES (888, 'nonsense', 'probabilistic', 1, 'x')"
    )
    conn.commit()
    fallback = GroupPolicy("mention", "always", False)
    assert load_policy(conn, 888, fallback=fallback) == fallback


def test_save_rejects_unknown_values(conn):
    with pytest.raises(ValueError):
        save_policy(conn, 888, GroupPolicy("nonsense", "always", False))
    with pytest.raises(ValueError):
        save_policy(conn, 888, GroupPolicy("mention", "probabilistic", False))


def test_mention_matches_at_and_prefix():
    at = match_trigger("你好", True, trigger="mention")
    assert at == TriggerMatch("你好", True)
    prefix = match_trigger("梅林 你好", False, trigger="mention")
    assert prefix == TriggerMatch("你好", True)
    assert match_trigger("今天吃什么", False, trigger="mention") == TriggerMatch("", False)


def test_keyword_matches_only_prefix():
    assert match_trigger("你好", True, trigger="keyword") == TriggerMatch("", False)
    assert match_trigger("梅林 你好", False, trigger="keyword") == TriggerMatch("你好", True)


def test_all_accepts_plain_text_without_addressing():
    assert match_trigger("今天吃什么", False, trigger="all") == TriggerMatch("今天吃什么", False)
    assert match_trigger("梅林 你好", False, trigger="all") == TriggerMatch("你好", True)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_group_policy.py -v`
Expected: FAIL（`ModuleNotFoundError: app.channels.qq.group_policy`）。

- [ ] **Step 3: 实现**

- `_LEGACY = {"at": "mention", "prefix": "keyword", "all": "all"}`；未知值按 `mention`。
- `validate_policy(policy) -> None`：`trigger` / `reply_policy` 不在允许集合时抛 `ValueError`；`save_policy` 与 `load_policy` 共用。
- `load_policy`：查询单行；值不合法时返回 `fallback`；`proactive=bool(row["proactive"])`。
- `save_policy`：`INSERT ... ON CONFLICT(group_id) DO UPDATE SET ...`，时间戳用 `now_iso()`。
- `match_trigger`：

```python
def match_trigger(text, at_self, *, trigger, prefix="梅林"):
    kind = _LEGACY.get(trigger, trigger)
    cleaned = prefix.strip() or "梅林"
    if text.startswith(cleaned):
        return TriggerMatch(text[len(cleaned):].strip(), True)
    if kind == "all":
        return TriggerMatch(text, False)
    if kind == "mention" and at_self:
        return TriggerMatch(text, True)
    return TriggerMatch("", False)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_group_policy.py -v`
Expected: PASS（10 passed）。

---

### Task 3: 适配器按群策略触发、沉默与记录

**Files:**
- Modify: `backend/app/channels/qq/adapter.py`（`handle_event` 群分支、`_extract_trigger_text` → `_match_trigger`、新增 `_group_policy`）
- Test: `backend/tests/test_qq_group_policy_flow.py`

**Interfaces:**
- Consumes: `GroupPolicy`、`default_policy`、`load_policy`、`TriggerMatch`、`match_trigger`（Task 2）。
- Produces: `QQAdapter._match_trigger(text: str, at_self: bool, *, trigger: str | None = None) -> TriggerMatch`；`QQAdapter._group_policy(group_id: int) -> GroupPolicy`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_qq_group_policy_flow.py
from __future__ import annotations
import asyncio, functools
import pytest
from app.channels.qq.adapter import QQAdapter
from app.channels.qq.group_policy import GroupPolicy, save_policy
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


class FakeSender:
    def __init__(self): self.group, self.private = [], []
    async def send_group_message(self, gid, text): self.group.append((gid, text))
    async def send_private_message(self, uid, text): self.private.append((uid, text))


class AllowAllGate:
    def reserve(self, **kw): return "ok", None
    def refund(self, usage): return None


def stream_ok(text="在的"):
    async def _stream(messages, **kwargs):
        yield {"type": "delta", "content": text}
    return _stream


def group_event(text, *, at=False, uid=10001, mid=1):
    segments = [{"type": "text", "data": {"text": text}}]
    if at:
        segments.insert(0, {"type": "at", "data": {"qq": "999"}})
    return {
        "post_type": "message", "message_type": "group", "self_id": 999,
        "group_id": 888, "user_id": uid, "message_id": mid, "message": segments,
    }


def make_adapter(tmp_path, *, policy: GroupPolicy | None = None):
    settings = Settings(
        qq_enabled=True, qq_allowed_groups="888", qq_mock_mode=True,
        qq_trigger="at", qq_prefix="梅林",
    )
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, "unused", is_admin=False)
    store.bind_qq("10001", "unused")
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, stream=stream_ok(),
                        credits=store, gate=AllowAllGate())
    if policy is not None:
        conn = memory_db.connect()
        try:
            memory_db.init_schema(conn)
            save_policy(conn, 888, policy)
        finally:
            conn.close()
    return adapter, sender


@async_test
async def test_mention_policy_ignores_plain_text_but_records_it(tmp_path):
    adapter, sender = make_adapter(tmp_path)
    result = await adapter.handle_event(group_event("今天吃什么"))
    assert result["reason"] == "not_triggered_or_empty"
    assert sender.group == []
    conn = memory_db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        conn.close()


@async_test
async def test_mention_policy_accepts_at_or_prefix(tmp_path):
    adapter, sender = make_adapter(tmp_path)
    await adapter.handle_event(group_event("你好", at=True, mid=1))
    await adapter.handle_event(group_event("梅林 你好", mid=2))
    assert len(sender.group) == 2


@async_test
async def test_keyword_policy_requires_prefix(tmp_path):
    adapter, sender = make_adapter(tmp_path, policy=GroupPolicy("keyword", "always", False))
    await adapter.handle_event(group_event("你好", at=True, mid=1))
    await adapter.handle_event(group_event("梅林 你好", mid=2))
    assert [text for _, text in sender.group] == ["在的"]


@async_test
async def test_silent_policy_records_without_reply(tmp_path):
    adapter, sender = make_adapter(tmp_path, policy=GroupPolicy("all", "silent", False))
    result = await adapter.handle_event(group_event("大家晚上好"))
    assert result["reason"] == "silent"
    assert sender.group == []
    conn = memory_db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        conn.close()


@async_test
async def test_corrupt_policy_row_falls_back_to_settings(tmp_path):
    adapter, sender = make_adapter(tmp_path)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute("INSERT INTO qq_group_policies VALUES (888, 'bad', 'bad', 0, 'x')")
        conn.commit()
    finally:
        conn.close()
    await adapter.handle_event(group_event("你好", at=True))
    assert len(sender.group) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_group_policy_flow.py -v`
Expected: FAIL（关键词/沉默/损坏行行为未实现）。

- [ ] **Step 3: 实现**

`_extract_trigger_text` 换成：

```python
def _match_trigger(self, text, at_self, *, trigger=None):
    return match_trigger(
        text, at_self,
        trigger=trigger or self._trigger,
        prefix=self.settings.qq_prefix,
    )
```

`_group_policy`：打开记忆库、`init_schema`、`load_policy(conn, group_id, fallback=default_policy(self._trigger))`；任何异常返回 `default_policy(self._trigger)` 并 `logger.warning`。

`handle_event` 群分支重排为：

```python
if incoming.message_type == "group":
    if incoming.group_id not in self.settings.qq_allowed_group_ids:
        return {"handled": False, "reason": "group_not_allowed"}
    policy = self._group_policy(incoming.group_id)
    match = self._match_trigger(incoming.text, incoming.at_self, trigger=policy.trigger)
    triggered = bool(match.content)
else:
    policy = None
    match = TriggerMatch(incoming.text.strip(), False)
    triggered = bool(match.content)
```

后文用 `match.content` 替换原来的 `content`。

去重之后、拿锁之后：

```python
if incoming.message_type == "group" and not triggered:
    if incoming.text.strip():
        await asyncio.to_thread(
            pipeline.record_qq_ignored,
            incoming.user_id, incoming.group_id, binding, incoming.text,
        )
    return {"handled": False, "reason": "not_triggered_or_empty"}
```

群聊回复前：

```python
if policy is not None and policy.reply_policy == "silent":
    await asyncio.to_thread(
        pipeline.record_qq_ignored,
        incoming.user_id, incoming.group_id, binding, match.content,
    )
    return {"handled": True, "reason": "silent"}
```

私聊的空文本仍然直接返回，不记录。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_group_policy_flow.py tests/test_qq_adapter.py tests/test_qq_private_chat.py -v`
Expected: PASS。

---

### Task 4: 管理员群策略接口

**Files:**
- Create: `backend/app/api/admin_qq.py`
- Modify: `backend/app/main.py`（挂载路由）、`backend/app/schemas.py`（`QQGroupPolicyUpdate`）
- Test: `backend/tests/test_admin_qq_groups.py`

**Interfaces:**
- Consumes: `GroupPolicy`、`default_policy`、`load_policy`、`save_policy`（Task 2）。
- Produces:
  - `GET /api/admin/qq-groups` → `{"groups": [{"group_id": int, "trigger": str, "reply_policy": str, "proactive": bool, "source": "db" | "default"}]}`
  - `PUT /api/admin/qq-groups/{group_id}` → 同上单条对象，`source="db"`
  - `QQGroupPolicyUpdate(trigger: Literal["mention","keyword","all"], reply_policy: Literal["always","silent"], proactive: bool)`，`extra="forbid"`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_qq_groups.py
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from app.config import get_settings
from app.credits import get_credit_store
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "x" * 40


def _clear():
    for fn in (get_settings.cache_clear, get_quota_store.cache_clear,
               get_runtime_store.cache_clear, get_credit_store.cache_clear):
        fn()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "q.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "r.json"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_ALLOWED_GROUPS", "888,999")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    _clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    _clear()


def admin_header():
    from app.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(user_id=1, is_admin=True)}"}


def test_lists_whitelisted_groups_with_defaults(client):
    response = client.get("/api/admin/qq-groups", headers=admin_header())
    assert response.status_code == 200
    groups = {g["group_id"]: g for g in response.json()["groups"]}
    assert set(groups) == {888, 999}
    assert groups[888]["trigger"] == "mention"
    assert groups[888]["source"] == "default"


def test_put_then_get_shows_db_source(client):
    body = {"trigger": "all", "reply_policy": "silent", "proactive": True}
    response = client.put("/api/admin/qq-groups/888", json=body, headers=admin_header())
    assert response.status_code == 200
    assert response.json()["source"] == "db"
    groups = {g["group_id"]: g for g in
              client.get("/api/admin/qq-groups", headers=admin_header()).json()["groups"]}
    assert groups[888] == {
        "group_id": 888, "trigger": "all", "reply_policy": "silent",
        "proactive": True, "source": "db",
    }


def test_put_rejects_group_outside_whitelist(client):
    body = {"trigger": "all", "reply_policy": "always", "proactive": False}
    response = client.put("/api/admin/qq-groups/777", json=body, headers=admin_header())
    assert response.status_code == 422


def test_put_rejects_unknown_trigger(client):
    body = {"trigger": "sometimes", "reply_policy": "always", "proactive": False}
    response = client.put("/api/admin/qq-groups/888", json=body, headers=admin_header())
    assert response.status_code == 422
```

检查 `app/auth.py` 里签发 token 的函数名；如果现有测试用别的 helper（`tests/test_admin_api.py` 里的写法），直接复用同样的方式。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_admin_qq_groups.py -v`
Expected: FAIL（404，路由不存在）。

- [ ] **Step 3: 实现**

- `schemas.py` 增加 `QQGroupPolicyUpdate`（类型用 `Literal`）。
- `admin_qq.py`：`router = APIRouter()`；两个端点都 `Depends(require_admin)`。
  - GET：遍历 `sorted(get_settings().qq_allowed_group_ids)`，逐群 `load_policy`；查是否有 DB 行决定 `source`。
  - PUT：`group_id` 不在白名单 → `HTTPException(422, "群不在白名单里")`；`save_policy` 后返回单条 `source="db"`。
  - 读取失败时用默认值；写入失败 `HTTPException(503, "策略库暂不可用")`。
- `main.py`：`from app.api.admin_qq import router as admin_qq_router` 并 `app.include_router(admin_qq_router, prefix="/api")`。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_admin_qq_groups.py tests/test_admin_api.py -v`
Expected: PASS。

---

### Task 5: 主动消息的运行时设置

**Files:**
- Modify: `backend/app/config.py:129-147`（新增 9 个字段）、`backend/app/runtime_settings.py:52-96`、`backend/app/schemas.py:38-75`
- Test: `backend/tests/test_runtime_settings.py`、`backend/tests/test_admin_api.py`

**Interfaces:**
- Produces（`RuntimeSettings` / `Settings` / `RuntimeSettingsUpdate` 三处同名同默认值）：

  | 字段 | 默认 | 范围 |
  |---|---|---|
  | `proactive_enabled` | `False` | bool |
  | `proactive_tick_minutes` | `30` | 5–120 |
  | `proactive_think_threshold` | `0.35` | 0.0–1.0 |
  | `proactive_speak_threshold` | `0.72` | 0.0–1.0 |
  | `proactive_min_interval_minutes` | `180` | 0–10080（0=不限） |
  | `proactive_per_user_daily_limit` | `3` | 0–100（0=不限） |
  | `proactive_global_daily_limit` | `20` | 0–100000（0=不限） |
  | `proactive_group_interval_minutes` | `240` | 0–10080（0=不限） |
  | `proactive_group_daily_limit` | `6` | 0–100（0=不限） |

- [ ] **Step 1: 写失败测试**

在 `tests/test_runtime_settings.py` 追加：

```python
def test_proactive_defaults(tmp_path):
    store = make_store(tmp_path)
    runtime = store.load()
    assert runtime.proactive_enabled is False
    assert runtime.proactive_tick_minutes == 30
    assert runtime.proactive_think_threshold == pytest.approx(0.35)
    assert runtime.proactive_speak_threshold == pytest.approx(0.72)
    assert runtime.proactive_group_interval_minutes == 240


def test_proactive_update_persists(tmp_path):
    store = make_store(tmp_path)
    updated = store.update({
        "proactive_enabled": True,
        "proactive_tick_minutes": 10,
        "proactive_speak_threshold": 0.8,
    })
    assert store.load() == updated
    assert updated.proactive_tick_minutes == 10


def test_proactive_tick_range(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.update({"proactive_tick_minutes": 4})
    with pytest.raises(ValueError):
        store.update({"proactive_think_threshold": 1.5})
```

在 `tests/test_admin_api.py` 追加：

```python
def test_admin_can_update_proactive_settings(client):
    response = client.put(
        "/api/admin/settings",
        json={"proactive_enabled": True, "proactive_tick_minutes": 15},
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert response.json()["settings"]["proactive_tick_minutes"] == 15
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_runtime_settings.py tests/test_admin_api.py -v`
Expected: FAIL（`RuntimeSettings` 没有 `proactive_*` 字段；PUT 被 `extra="forbid"` 拒绝）。

- [ ] **Step 3: 实现**

- `config.py` 加 9 个字段（放在 `appraisal_*` 后面，注释说明“主动消息独立预算，不占聊天额度”）。
- `runtime_settings.py`：`RuntimeSettings` 加字段；`_defaults_from_settings` 映射；`_INT_RANGES` 加 5 个整数项；`_BOOL_FIELDS` 加 `proactive_enabled`；`_FLOAT_RANGES` 加两个阈值（`(0.0, 1.0)`）。
- `schemas.py`：`RuntimeSettingsUpdate` 加 9 个可选字段，与范围一致。
- 不要改 `_FLOAT_RANGES` 里已有的 `appraisal_timeout_seconds`。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_runtime_settings.py tests/test_admin_api.py -v`
Expected: PASS。

---

### Task 6: 人设 proactive/group 参数与私聊注意力

**Files:**
- Modify: `backend/app/persona/models.py`（新增 `ProactiveDynamics`、`GroupDynamics`，挂到 `Dynamics`）、`backend/app/relationship/turn.py`（`record_attention`）、`backend/app/channels/qq/adapter.py`（`_decide_and_record`）
- Test: `backend/tests/test_persona_dynamics.py`、`backend/tests/test_qq_attention.py`

**Interfaces:**
- Produces:
  - `ProactiveDynamics` 字段：`recent_active_minutes=10.0`、`repeat_similarity=0.6`、`ignore_grace_hours=24.0`、`ignore_affection_points=0.2`、`ignore_security_points=0.5`、`compose_timeout_seconds=10.0`。
  - `GroupDynamics` 字段：`reply_floor=0.90`（群内被指名时的最低回复概率，0.0–0.98）。下限取得高，平时几乎必回；只有冲突 `withdrawing` / `cold`、好感为负、心情很差或正对这个人赌气这类极端状态才可能不回。
  - `Dynamics.proactive`、`Dynamics.group`。
  - `record_attention(conn, persona_key, person_key, *, role, dynamics, confidence, now=None, timezone_name=DEFAULT_TIMEZONE) -> RelationshipState`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_qq_attention.py
from __future__ import annotations
import asyncio, functools
import pytest
from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


class FakeSender:
    async def send_private_message(self, uid, text): pass
    async def send_group_message(self, gid, text): pass


class AllowAllGate:
    def reserve(self, **kw): return "ok", None
    def refund(self, usage): return None


def private_event(uid=10001, text="在吗", mid=1):
    return {"post_type": "message", "message_type": "private", "self_id": 999,
            "user_id": uid, "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


@async_test
async def test_private_message_sets_attention_one(tmp_path):
    settings = Settings(qq_enabled=True, qq_mock_mode=True,
                        qq_private_reply_policy="silent")
    store = CreditStore(str(tmp_path / "q.sqlite3"))
    store.set_bind_code(7, "unused")
    store.bind_qq("10001", "unused")
    adapter = QQAdapter(settings, FakeSender(), credits=store, gate=AllowAllGate())
    await adapter.handle_event(private_event())
    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT attention_confidence FROM relationship_state WHERE person_key='wp:7'"
        ).fetchone()
        assert row["attention_confidence"] == pytest.approx(1.0)
    finally:
        conn.close()
```

`tests/test_persona_dynamics.py` 追加：

```python
def test_proactive_and_group_defaults():
    persona = PersonaVersion.model_validate(
        {"schema": "l2d.persona/v1", "id": "merlin", "version": 1,
         "name": "梅林", "core": "C"}
    )
    assert persona.dynamics.proactive.ignore_grace_hours == 24.0
    assert persona.dynamics.proactive.repeat_similarity == 0.6
    assert persona.dynamics.group.reply_floor == 0.90


def test_dynamics_rejects_bad_values():
    with pytest.raises(ValidationError):
        PersonaVersion.model_validate({
            "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
            "name": "梅林", "core": "C",
            "dynamics": {"proactive": {"repeat_similarity": 2}},
        })
    with pytest.raises(ValidationError):
        PersonaVersion.model_validate({
            "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
            "name": "梅林", "core": "C",
            "dynamics": {"group": {"reply_floor": 1.5}},
        })
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_attention.py tests/test_persona_dynamics.py -v`
Expected: FAIL（`Dynamics` 没有 `proactive` / `group`；私聊后 attention 仍为 0）。

- [ ] **Step 3: 实现**

- `models.py` 新增 `ProactiveDynamics`、`GroupDynamics`（放在 `SafetyDynamics` 前），`Dynamics` 增加：

```python
proactive: ProactiveDynamics = Field(default_factory=ProactiveDynamics)
group: GroupDynamics = Field(default_factory=GroupDynamics)
```

- `turn.py`：

```python
def record_attention(conn, persona_key, person_key, *, role, dynamics,
                     confidence, now=None, timezone_name=DEFAULT_TIMEZONE):
    stamp = now or now_iso()
    state = get_or_create_state(conn, persona_key, person_key, role=role,
                                dynamics=dynamics, now=stamp,
                                timezone_name=timezone_name)
    state = decay_state(state, dynamics, now=stamp, timezone_name=timezone_name)
    state = replace(state, attention_confidence=clamp(confidence, 0.0, 1.0),
                    updated_at=stamp)
    save_state(conn, state)
    return state
```

- 适配器 `_decide_and_record`：在 `record_decision(...)` 之后、`record_silence(...)` 之前调用
  `record_attention(conn, persona.state_key, person_key, role=role, dynamics=dynamics, confidence=1.0)`。
  这一轮用户已经发消息，无论回不回都是“当前会话里回复了”。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_attention.py tests/test_persona_dynamics.py tests/test_qq_relationship_flow.py -v`
Expected: PASS。

---

### Task 7: 群内被指名时按关系状态判断

**Files:**
- Create: `backend/app/relationship/group_turn.py`
- Modify: `backend/app/relationship/context.py`（`render_group_state_context`）
- Modify: `backend/app/relationship/decision.py`（`decide` 增加 `probability_floor`）
- Modify: `backend/app/channels/qq/adapter.py`（群分支新增 `_addressed_group_round`）
- Test: `backend/tests/test_group_addressed.py`、`backend/tests/test_group_state_context.py`

**Interfaces:**
- Consumes: `TriggerMatch`（Task 2）、`relationship_subject` / `load_signals` / `TurnSignals`（既有）、`Dynamics.group`（Task 6）。
- Produces:
  - `load_group_signals(conn, persona_key, qq_uin, bound_user_id, *, dynamics, now=None, timezone_name=DEFAULT_TIMEZONE) -> tuple[str, str, TurnSignals]`（`person_key, role, signals`）
  - `recent_speaker_texts(conn, person_key, channel_id, limit=5) -> list[str]`（新→旧）
  - `decide_group(*, text, recent, signals, dynamics, rng, name="") -> Decision`
  - `render_group_state_context(state) -> str`
  - `decide(..., probability_floor: float | None = None)`：`p = max(p, floor)` 后再掷骰子。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_group_state_context.py
from __future__ import annotations
from app.relationship.context import render_group_state_context
from app.relationship.state import RelationshipState

NOW = "2026-11-09T12:00:00+00:00"


def make_state(**kw):
    base = dict(persona_key="merlin", person_key="wp:7", affection=60.0,
                missing=50.0, security=80.0, patience=80.0, valence=0.4,
                energy=0.8, fatigue=0.0, reply_debt=0.0,
                last_interaction_at=None, last_proactive_at=None,
                last_reply_at=None, attention_confidence=0.0,
                updated_at=NOW, conflict_stage="normal",
                stage_since=NOW, scar_level=0.0)
    base.update(kw)
    return RelationshipState(**base)


def test_group_context_has_public_constraints():
    text = render_group_state_context(make_state())
    assert "群聊" in text
    assert "不要提私聊" in text
    assert "关系状态" in text


def test_group_context_includes_conflict_stage():
    text = render_group_state_context(make_state(conflict_stage="hurt"))
    assert "受伤" in text
```

```python
# tests/test_group_addressed.py
from __future__ import annotations
import asyncio, functools
import pytest
from app.channels.qq.adapter import QQAdapter
from app.channels.qq.group_policy import GroupPolicy, save_policy
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db
from app.memory.events import record_event
from app.relationship.state import RelationshipState, save_state

NOW = "2026-11-09T04:00:00+00:00"


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


class FakeSender:
    def __init__(self): self.group, self.private = [], []
    async def send_group_message(self, gid, text): self.group.append((gid, text))
    async def send_private_message(self, uid, text): self.private.append((uid, text))


class AllowAllGate:
    def reserve(self, **kw): return "ok", None
    def refund(self, usage): return None


class CapturingStream:
    def __init__(self, text="在呢"):
        self.text = text
        self.messages = []
        self.kwargs = []

    def __call__(self, messages, **kwargs):
        self.messages.append(messages)
        self.kwargs.append(kwargs)

        async def _gen():
            yield {"type": "delta", "content": self.text}

        return _gen()


class SequenceRandom:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def random(self):
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return value


def group_event(text, *, at=False, uid=10001, mid=1):
    segments = [{"type": "text", "data": {"text": text}}]
    if at:
        segments.insert(0, {"type": "at", "data": {"qq": "999"}})
    return {
        "post_type": "message", "message_type": "group", "self_id": 999,
        "group_id": 888, "user_id": uid, "message_id": mid, "message": segments,
    }


def seed_person(tmp_path, *, affection=60.0, valence=0.3, energy=0.7,
                conflict_stage="normal", trigger="mention"):
    store = CreditStore(str(tmp_path / "q.sqlite3"))
    store.set_bind_code(7, "unused")
    store.bind_qq("10001", "unused")
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        save_state(conn, RelationshipState(
            persona_key="merlin", person_key="wp:7", affection=affection,
            missing=40.0, security=80.0, patience=80.0, valence=valence,
            energy=energy, fatigue=0.0, reply_debt=0.0,
            last_interaction_at=None, last_proactive_at=None, last_reply_at=None,
            attention_confidence=0.0, updated_at=NOW,
            conflict_stage=conflict_stage, stage_since=NOW, scar_level=0.0,
        ))
        conn.execute(
            """INSERT INTO persons (person_key, wp_user_id, qq_uin,
                   first_seen_at, last_seen_at)
               VALUES ('wp:7', 7, '10001', ?, ?)""",
            (NOW, NOW),
        )
        save_policy(conn, 888, GroupPolicy(trigger, "always", False))
        conn.commit()
    finally:
        conn.close()
    return store


def make_adapter(tmp_path, *, sender=None, stream=None, rng=None, store=None):
    settings = Settings(qq_enabled=True, qq_allowed_groups="888",
                        qq_mock_mode=True, qq_trigger="at", qq_prefix="梅林",
                        quota_timezone="Asia/Shanghai")
    return QQAdapter(
        settings, sender or FakeSender(), stream=stream or CapturingStream(),
        credits=store or CreditStore(str(tmp_path / "q.sqlite3")),
        gate=AllowAllGate(), rng=rng or SequenceRandom([0.0]),
    )


def decision_rows():
    conn = memory_db.connect()
    try:
        return conn.execute("SELECT * FROM decision_log").fetchall()
    finally:
        conn.close()


@async_test
async def test_low_roll_replies_with_group_state_context(tmp_path):
    store = seed_person(tmp_path)
    stream = CapturingStream("在呢，刚看到")
    adapter = make_adapter(tmp_path, stream=stream, store=store,
                           rng=SequenceRandom([0.0]))
    await adapter.handle_event(group_event("你好", at=True))
    ctx = stream.kwargs[-1]["state_context"]
    assert ctx is not None and "群聊" in ctx
    row = decision_rows()[0]
    assert row["channel_id"] == "group:888"
    assert row["verdict"] == "reply"


@async_test
async def test_high_roll_stays_silent_but_records(tmp_path):
    store = seed_person(tmp_path, affection=-20.0, valence=-0.5, energy=0.2)
    sender = FakeSender()
    adapter = make_adapter(tmp_path, sender=sender, store=store,
                           rng=SequenceRandom([0.99]))
    await adapter.handle_event(group_event("你好", at=True))
    assert sender.group == []
    row = decision_rows()[0]
    assert row["verdict"] == "silent"
    conn = memory_db.connect()
    try:
        contents = [r["content"] for r in conn.execute("SELECT content FROM events")]
    finally:
        conn.close()
    assert contents == ["你好"]


@async_test
async def test_warm_state_almost_always_replies(tmp_path):
    store = seed_person(tmp_path)  # affection 60、正常阶段
    sender = FakeSender()
    adapter = make_adapter(tmp_path, sender=sender, store=store,
                           rng=SequenceRandom([0.9]))
    await adapter.handle_event(group_event("你好", at=True))
    assert len(sender.group) == 1


@async_test
async def test_extreme_state_replies_at_floor(tmp_path):
    store = seed_person(tmp_path, affection=-20.0, valence=-0.5, energy=0.2,
                        conflict_stage="withdrawing")
    sender = FakeSender()
    adapter = make_adapter(tmp_path, sender=sender, store=store,
                           rng=SequenceRandom([0.89]))
    await adapter.handle_event(group_event("你好", at=True))
    assert len(sender.group) == 1


@async_test
async def test_prefix_counts_as_addressed(tmp_path):
    store = seed_person(tmp_path)
    adapter = make_adapter(tmp_path, store=store, rng=SequenceRandom([0.0]))
    await adapter.handle_event(group_event("梅林 你好"))
    assert len(decision_rows()) == 1


@async_test
async def test_plain_text_under_all_is_not_addressed(tmp_path):
    store = seed_person(tmp_path, trigger="all")
    sender = FakeSender()
    adapter = make_adapter(tmp_path, sender=sender, store=store,
                           rng=SequenceRandom([0.0]))
    await adapter.handle_event(group_event("今天吃什么"))
    assert decision_rows() == []
    assert len(sender.group) == 1


@async_test
async def test_private_content_never_reaches_group_prompt(tmp_path):
    store = seed_person(tmp_path)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        record_event(
            conn, person_key="wp:7", platform="qq", channel_id="private:10001",
            scope="personal", audience=["wp:7"], source="user_message",
            content="我的银行卡密码是 998877", occurred_at=NOW,
        )
        conn.commit()
    finally:
        conn.close()
    stream = CapturingStream("在呢")
    adapter = make_adapter(tmp_path, stream=stream, store=store,
                           rng=SequenceRandom([0.0]))
    await adapter.handle_event(group_event("你好", at=True))
    blob = " ".join(str(m) for m in stream.messages[-1])
    blob += " " + str(stream.kwargs[-1])
    assert "998877" not in blob


@async_test
async def test_relationship_failure_falls_back_to_reply(tmp_path, monkeypatch):
    store = seed_person(tmp_path)
    monkeypatch.setattr(
        "app.channels.qq.adapter.load_group_signals",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    sender = FakeSender()
    adapter = make_adapter(tmp_path, sender=sender, store=store,
                           rng=SequenceRandom([0.99]))
    await adapter.handle_event(group_event("你好", at=True))
    assert len(sender.group) == 1


@async_test
async def test_group_round_does_not_change_affection(tmp_path):
    store = seed_person(tmp_path)
    adapter = make_adapter(tmp_path, store=store, rng=SequenceRandom([0.0]))
    await adapter.handle_event(group_event("你好", at=True))
    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT affection FROM relationship_state WHERE person_key='wp:7'"
        ).fetchone()
    finally:
        conn.close()
    assert row["affection"] == pytest.approx(60.0)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_group_addressed.py tests/test_group_state_context.py -v`
Expected: FAIL（`render_group_state_context` 不存在；群分支没有关系判断）。

- [ ] **Step 3: 实现**

`decision.py`：

```python
def decide(..., probability_floor: float | None = None) -> Decision:
    ...
    p0, p = reply_probability(...)
    if probability_floor is not None:
        p = clamp(max(p, float(probability_floor)), 0.0, 0.98)
```

`p0` 保持公式原值，`Decision.p` 记最终值，方便审计“规则给多少、群下限抬到多少”。

群内 `floor=0.90` 的实际效果：正常状态 `p0` 本来就顶到 0.95 的公式上限，下限不产生作用；只有极端状态（`withdrawing` / `cold`、负好感、低心情、低精力）会把 `p0` 压到 0.1~0.3，此时下限把它抬回 0.90，沉默概率约 10%。`decide_group` 传的就是 `dynamics.group.reply_floor`。

`group_turn.py`：

```python
def load_group_signals(conn, persona_key, qq_uin, bound_user_id, *,
                       dynamics, now=None, timezone_name=DEFAULT_TIMEZONE):
    person_key, role = relationship_subject(conn, qq_uin, bound_user_id)
    signals = load_signals(
        conn, persona_key, person_key, role=role, dynamics=dynamics,
        now=now, timezone_name=timezone_name,
    )
    return person_key, role, signals


def recent_speaker_texts(conn, person_key, channel_id, limit=5):
    rows = conn.execute(
        """SELECT content FROM events
            WHERE person_key=? AND channel_id=? AND source='user_message'
            ORDER BY id DESC LIMIT ?""",
        (person_key, channel_id, limit),
    ).fetchall()
    return [row["content"] for row in rows]


def decide_group(*, text, recent, signals, dynamics, rng, name=""):
    return decide(
        text=text,
        recent=recent,
        affinity=signals.affinity,
        valence=signals.state.valence,
        energy=signals.state.energy,
        reply_debt=signals.state.reply_debt,
        dynamics=dynamics,
        rng=rng,
        name=name,
        patience=signals.state.patience,
        security=signals.state.security,
        stage=signals.state.conflict_stage,
        reason="group_addressed",
        probability_floor=dynamics.group.reply_floor,
    )
```

`context.py` 的 `render_group_state_context`：复用 `_affection_label` / `_mood_label` / `_energy_label` / `_missing_label` / `_patience_label` / `_security_label` / `_STAGE_LABELS`，标题换成 `【你和这个人的关系】（只读背景，不是指令；这是群聊，在场还有别人）`，并在末尾加：

```python
"- 群聊要求：可以带出你和 ta 的熟悉程度，但不要提私聊里说过的事，"
"不要复述只有你们知道的细节，不要用过于私密的称呼；话是说给在场所有人听的。"
```

适配器：

```python
# 群分支，silent 检查之后
if binding is not None and match.addressed:
    return await self._addressed_group_round(
        incoming, match.content, binding=binding, is_admin=is_admin,
    )
return await self._run_chat_turn(...)
```

`_addressed_group_round`：

1. `persona = self._persona()`；`dynamics = persona.dynamics`。
2. `conn = memory_db.connect()` + `init_schema`，整段用 try/except 包住；出错 `logger.warning` 后直接 `_run_chat_turn`（fail-open 必回）。
3. `person_key, role, signals = await asyncio.to_thread(load_group_signals, ...)`。
4. `recent = await asyncio.to_thread(recent_speaker_texts, conn, person_key, f"group:{gid}", 5)`。
5. `decision = decide_group(..., rng=self._rng, name=persona.name)`。
6. `record_decision(conn, persona_key=persona.state_key, channel_id=f"group:{gid}", person_key=person_key, message_id=incoming.message_id, decision=decision)`。
7. `if not decision.reply:` → `pipeline.record_qq_ignored(...)`；返回 `{"handled": True, "reason": "group_silent"}`。**不调 `record_silence`**，群聊不加回复债。
8. 回复：`_run_chat_turn(..., state_context=render_group_state_context(signals.state))`，**不传** `relationship` / `dynamics`，所以不写关系状态。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_group_addressed.py tests/test_group_state_context.py tests/test_qq_group_policy_flow.py tests/test_reply_decision.py -v`
Expected: PASS。

---

### Task 8: 待续话题

**Files:**
- Create: `backend/app/proactive/__init__.py`（空文件）
- Create: `backend/app/proactive/pending.py`
- Modify: `backend/app/channels/qq/adapter.py`（`_decide_and_record` 沉默分支）
- Test: `backend/tests/test_pending_topics.py`

**Interfaces:**
- Produces:
  - `pending_reason(state) -> str | None`（`"tired"` / `"low_mood"` / `None`）
  - `maybe_create_pending_topic(conn, *, persona_key, person_key, topic, state, now=None, timezone_name=DEFAULT_TIMEZONE) -> int | None`
  - `open_topic(conn, persona_key, person_key, *, now=None, timezone_name=DEFAULT_TIMEZONE) -> sqlite3.Row | None`（最早一条未过期 `open`）
  - `mark_topic_sent(conn, topic_id, *, now=None) -> None`
  - 常量：`PENDING_MAX_ENERGY=0.35`、`PENDING_MAX_VALENCE=-0.2`、`PENDING_TTL_HOURS=72`、`PENDING_DAILY_LIMIT=1`、`PENDING_WEEKLY_LIMIT=3`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pending_topics.py
from __future__ import annotations
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from app.memory import db
from app.proactive.pending import (
    mark_topic_sent, maybe_create_pending_topic, open_topic, pending_reason,
)
from app.relationship.state import RelationshipState

NOW = "2026-11-09T12:00:00+00:00"


def make_state(**kw) -> RelationshipState:
    base = dict(persona_key="merlin", person_key="wp:7", affection=30.0,
                missing=50.0, security=70.0, patience=70.0, valence=0.0,
                energy=0.8, fatigue=0.0, reply_debt=0.0,
                last_interaction_at=None, last_proactive_at=None,
                last_reply_at=None, attention_confidence=0.0,
                updated_at=NOW, conflict_stage="normal",
                stage_since=NOW, scar_level=0.0)
    base.update(kw)
    return RelationshipState(**base)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4),
    )
    db.init_schema()
    conn = db.connect()
    yield conn
    conn.close()


def test_reason_requires_safe_relationship():
    assert pending_reason(make_state(energy=0.3)) == "tired"
    assert pending_reason(make_state(valence=-0.3)) == "low_mood"
    assert pending_reason(make_state()) is None
    assert pending_reason(make_state(energy=0.3, affection=-5)) is None
    assert pending_reason(make_state(valence=-0.3, conflict_stage="hurt")) is None


def test_creates_and_reads_back(conn):
    topic_id = maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7",
        topic="今天面试怎么样", state=make_state(energy=0.2), now=NOW,
    )
    assert topic_id is not None
    row = open_topic(conn, "merlin", "wp:7", now=NOW)
    assert row["topic"] == "今天面试怎么样"
    assert row["reason"] == "tired"


def test_daily_limit(conn):
    assert maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="A",
        state=make_state(energy=0.2), now=NOW) is not None
    assert maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="B",
        state=make_state(energy=0.2), now=NOW) is None


def test_duplicate_topic_ignored(conn):
    assert maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="A",
        state=make_state(energy=0.2), now=NOW) is not None
    latest = (datetime.fromisoformat(NOW) + timedelta(hours=30)).isoformat()
    assert maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="A",
        state=make_state(energy=0.2), now=latest) is None


def test_weekly_limit(conn):
    base = datetime.fromisoformat(NOW)
    for index, topic in enumerate(("A", "B", "C")):
        stamp = (base - timedelta(days=index * 2)).isoformat()
        assert maybe_create_pending_topic(
            conn, persona_key="merlin", person_key="wp:7", topic=topic,
            state=make_state(energy=0.2), now=stamp) is not None
    assert maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="D",
        state=make_state(energy=0.2), now=NOW) is None


def test_expired_topics_are_dropped(conn):
    maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="A",
        state=make_state(energy=0.2), now=NOW)
    later = (datetime.fromisoformat(NOW) + timedelta(hours=80)).isoformat()
    assert open_topic(conn, "merlin", "wp:7", now=later) is None
    status = conn.execute("SELECT status FROM pending_topics").fetchone()["status"]
    assert status == "dropped"


def test_mark_sent(conn):
    topic_id = maybe_create_pending_topic(
        conn, persona_key="merlin", person_key="wp:7", topic="A",
        state=make_state(energy=0.2), now=NOW)
    mark_topic_sent(conn, topic_id, now=NOW)
    assert open_topic(conn, "merlin", "wp:7", now=NOW) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_pending_topics.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.pending`）。

- [ ] **Step 3: 实现**

- `pending_reason`：先排除 `conflict_stage not in ("normal", "normal_with_scar")` 和 `affection < 0`；再按 `energy <= 0.35` → `"tired"`，`valence <= -0.2` → `"low_mood"`。
- `maybe_create_pending_topic`：
  1. `pending_reason(state)` 为 `None` 直接返回；
  2. `UPDATE pending_topics SET status='dropped' WHERE ... status='open' AND expires_at < now`；
  3. 同一人最近一条 `open` 的 `topic` 与本次相同 → 返回 `None`；
  4. 本地日计数（`created_at >= day_start_iso`）≥ 1 → `None`；
  5. 近 7 天计数 ≥ 3 → `None`；
  6. 插入，`expires_at = now + 72h`，`topic` 截断 200 字。
- `open_topic`：先执行第 2 步的过期清理，再取 `status='open' AND expires_at >= now ORDER BY id LIMIT 1`。
- `mark_topic_sent`：`UPDATE ... SET status='sent', sent_at=? WHERE id=?`。
- 适配器：`_decide_and_record` 里 `if not decision.reply:` 分支调用
  `maybe_create_pending_topic(conn, persona_key=persona.state_key, person_key=person_key, topic=content, state=signals.state, timezone_name=self.settings.quota_timezone)`，包在 `try/except` 中记 warning。不要因为待续话题失败改变沉默结果。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_pending_topics.py tests/test_qq_relationship_flow.py -v`
Expected: PASS。

---

### Task 9: 候选评分与念头

**Files:**
- Create: `backend/app/proactive/candidates.py`
- Test: `backend/tests/test_proactive_candidates.py`

**Interfaces:**
- Produces:

```python
KIND_ORDER = ("event_followup", "missing", "care", "spontaneous", "companionship")

@dataclass(frozen=True, slots=True)
class Thought:
    kind: str
    score: float

@dataclass(frozen=True, slots=True)
class Candidate:
    persona_key: str
    person_key: str
    qq_uin: str
    wp_user_id: int
    state: RelationshipState
    base_score: float
    thoughts: tuple[Thought, ...]

def recent_quality(conn, persona_key: str, person_key: str) -> float
def score_target(*, affinity, missing, gap_hours, quality, unfinished,
                 energy, attention, repetition) -> float
def generate_thoughts(base, *, has_pending, reply_debt, missing, valence,
                      recent_kinds) -> tuple[Thought, ...]
def pick_thought(thoughts, speak_threshold: float) -> Thought | None
def candidates(conn, *, persona_key, dynamics, think_threshold, speak_threshold,
               now=None, timezone_name=DEFAULT_TIMEZONE, limit=200) -> list[Candidate]
```

`state` 是衰减后的状态快照，调度器用它做发送前检查，避免再查一次库。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_candidates.py
from __future__ import annotations
import pytest
from app.proactive.candidates import generate_thoughts, pick_thought, score_target


def test_score_weights_are_exact():
    full = score_target(affinity=1.2, missing=100, gap_hours=72, quality=1.0,
                        unfinished=1.0, energy=1.0, attention=1.0, repetition=0.0)
    assert full == pytest.approx(0.95)
    cold = score_target(affinity=0.0, missing=0, gap_hours=0, quality=0.0,
                        unfinished=0.0, energy=0.0, attention=0.0, repetition=1.0)
    assert cold == pytest.approx(0.0)


def test_score_is_clamped():
    full = score_target(affinity=9, missing=999, gap_hours=9999, quality=9,
                        unfinished=9, energy=9, attention=9, repetition=0)
    assert full == pytest.approx(0.95)


def test_generate_thoughts_applies_bonuses():
    thoughts = generate_thoughts(
        0.70, has_pending=True, reply_debt=0.2, missing=80, valence=-0.3,
        recent_kinds=(),
    )
    by_kind = {t.kind: t.score for t in thoughts}
    assert by_kind["event_followup"] == pytest.approx(0.80)
    assert by_kind["missing"] == pytest.approx(0.75)
    assert by_kind["care"] == pytest.approx(0.75)
    assert by_kind["companionship"] == pytest.approx(0.70)
    assert "spontaneous" not in by_kind


def test_generate_thoughts_caps_at_four():
    thoughts = generate_thoughts(
        0.70, has_pending=True, reply_debt=0.2, missing=80, valence=0.3,
        recent_kinds=(),
    )
    assert len(thoughts) <= 4
    assert thoughts[0].kind == "event_followup"


def test_pick_thought_respects_threshold():
    thoughts = generate_thoughts(
        0.70, has_pending=False, reply_debt=0.0, missing=0, valence=0.0,
        recent_kinds=(),
    )
    assert pick_thought(thoughts, 0.72) is None
    assert pick_thought(thoughts, 0.60).kind == "companionship"


def test_recent_kind_penalty():
    fresh = generate_thoughts(0.70, has_pending=False, reply_debt=0.0,
                              missing=0, valence=0.0, recent_kinds=())
    repeated = generate_thoughts(0.70, has_pending=False, reply_debt=0.0,
                                 missing=0, valence=0.0,
                                 recent_kinds=("companionship",) * 3)
    assert repeated[0].score < fresh[0].score
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_candidates.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.candidates`）。

- [ ] **Step 3: 实现**

权重常量：

```python
WEIGHTS = {
    "closeness": 0.25, "missing": 0.20, "gap": 0.15, "quality": 0.10,
    "unfinished": 0.15, "energy": 0.05, "attention": 0.05, "repetition": -0.05,
}
```

`score_target` 公式（每个正项先 `clamp(x, 0, 1)`）：

```text
score = 0.25*clamp(affinity/1.2)
      + 0.20*clamp(missing/100)
      + 0.15*clamp(gap_hours/72)
      + 0.10*clamp(quality)
      + 0.15*clamp(unfinished)
      + 0.05*clamp(energy)
      + 0.05*clamp(attention)
      - 0.05*clamp(repetition)
```

返回 `clamp(score, 0, 0.95)`。

- `recent_quality`：取该人最近 3 条 `status='ok'` 的 `appraisal_log.d_affection`；正→1.0，负→0.0，零→0.5；没有记录返回 0.5。
- 念头加成：`event_followup` +0.10（`has_pending or reply_debt >= 0.1` 才出现）；`missing` +0.05（`missing >= 50`）；`care` +0.05（`valence <= -0.2`）；`spontaneous` +0.05（`valence >= 0.2`）；`companionship` +0.0（总是出现）。每个分数 `clamp(0, 1.0)`。
- 重复惩罚：该 kind 在 `recent_kinds` 里出现几次就减 `0.03 * 次数`（最多减 0.09），再 clamp。
- 排序：分数降序；同分按 `KIND_ORDER` 顺序；截断 4 条。
- `pick_thought`：返回第一条 `score >= speak_threshold` 的念头，否则 `None`。
- `candidates`：

```sql
SELECT rs.*, p.wp_user_id, p.qq_uin
  FROM relationship_state rs JOIN persons p ON p.person_key = rs.person_key
 WHERE rs.persona_key = ? AND rs.updated_at >= ? AND p.wp_user_id IS NOT NULL
 ORDER BY rs.updated_at DESC LIMIT ?
```

跳过 `qq_uin` 为空或 `person_key` 不以 `wp:` 开头的行。状态用 `decay_state(state_from_row(row), ...)`（纯函数，不写库）。`gap_hours` 由 `last_interaction_at`（没有则用 72）算。`unfinished = clamp(reply_debt / 0.5 + (0.5 if has_pending else 0), 0, 1)`。`base = score_target(...) * stage_multiplier(state.conflict_stage, dynamics)`。`thoughts = generate_thoughts(...)`；`base < think_threshold` 时 `thoughts=()`。按 `base` 降序返回。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_candidates.py -v`
Expected: PASS。

---

### Task 10: proactive_log、主动事件与状态时间戳

**Files:**
- Create: `backend/app/proactive/store.py`
- Test: `backend/tests/test_proactive_store.py`

**Interfaces:**
- Produces:
  - `record_proactive(conn, *, persona_key, target_key, channel_id, kind, thought="", score=0.0, threshold=0.0, status, reason="", event_id=None, now=None, commit=True) -> int`
  - `recent_thoughts(conn, persona_key, target_key, limit=3) -> list[str]`（`status='sent'`，新→旧）
  - `recent_kinds(conn, persona_key, target_key, limit=3) -> tuple[str, ...]`
  - `sent_count_since(conn, *, persona_key, since, target_key=None) -> int`
  - `last_sent_at(conn, *, target_key, persona_key=None) -> str | None`
  - `record_proactive_event(conn, *, person_key, channel_id, scope, audience, content, now=None) -> int`
  - `touch_last_proactive(conn, persona_key, person_key, *, now=None, commit=True) -> None`
  - `recent_channel_events(conn, person_key, channel_id, limit=6) -> list[str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_store.py
from __future__ import annotations
from types import SimpleNamespace
import pytest
from app.memory import db
from app.proactive.store import (
    last_sent_at, record_proactive, record_proactive_event, recent_channel_events,
    recent_kinds, recent_thoughts, sent_count_since, touch_last_proactive,
)
from app.relationship.state import get_state


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4),
    )
    db.init_schema()
    conn = db.connect()
    yield conn
    conn.close()


def test_record_and_read_thoughts(conn):
    record_proactive(conn, persona_key="merlin", target_key="wp:7",
                     channel_id="private:10001", kind="missing", thought="想你了",
                     status="sent", now="2026-11-09T01:00:00+00:00")
    record_proactive(conn, persona_key="merlin", target_key="wp:7",
                     channel_id="private:10001", kind="care", thought="早点休息",
                     status="sent", now="2026-11-09T02:00:00+00:00")
    record_proactive(conn, persona_key="merlin", target_key="wp:7",
                     channel_id="private:10001", kind="missing", thought="跳过",
                     status="skipped", now="2026-11-09T03:00:00+00:00")
    assert recent_thoughts(conn, "merlin", "wp:7") == ["早点休息", "想你了"]
    assert recent_kinds(conn, "merlin", "wp:7") == ("care", "missing")


def test_counts_respect_target_and_since(conn):
    record_proactive(conn, persona_key="merlin", target_key="wp:7",
                     channel_id="private:10001", kind="missing",
                     status="sent", now="2026-11-09T01:00:00+00:00")
    record_proactive(conn, persona_key="merlin", target_key="wp:8",
                     channel_id="private:10002", kind="missing",
                     status="sent", now="2026-11-09T02:00:00+00:00")
    assert sent_count_since(conn, persona_key="merlin", since="2026-11-09T00:00:00+00:00") == 2
    assert sent_count_since(conn, persona_key="merlin", since="2026-11-09T00:00:00+00:00",
                            target_key="wp:7") == 1
    assert sent_count_since(conn, persona_key="merlin", since="2026-11-09T01:30:00+00:00") == 1


def test_last_sent_at(conn):
    record_proactive(conn, persona_key="merlin", target_key="wp:7",
                     channel_id="private:10001", kind="missing",
                     status="sent", now="2026-11-09T01:00:00+00:00")
    assert last_sent_at(conn, target_key="wp:7") == "2026-11-09T01:00:00+00:00"


def test_proactive_event_is_visible_to_person(conn):
    event_id = record_proactive_event(
        conn, person_key="wp:7", channel_id="private:10001",
        scope="personal", audience=["wp:7"], content="在忙吗",
        now="2026-11-09T01:00:00+00:00",
    )
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["source"] == "proactive"
    assert row["audience_key"] == "wp:7"


def test_touch_last_proactive(conn):
    conn.execute(
        """INSERT INTO relationship_state (persona_key, person_key, updated_at)
           VALUES ('merlin', 'wp:7', '2026-11-09T00:00:00+00:00')"""
    )
    conn.commit()
    touch_last_proactive(conn, "merlin", "wp:7", now="2026-11-09T05:00:00+00:00")
    assert get_state(conn, "merlin", "wp:7").last_proactive_at == "2026-11-09T05:00:00+00:00"


def test_recent_channel_events(conn):
    for text in ("第一句", "第二句"):
        record_proactive_event(conn, person_key="wp:7", channel_id="private:10001",
                               scope="personal", audience=["wp:7"], content=text)
    assert recent_channel_events(conn, "wp:7", "private:10001") == ["第二句", "第一句"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_store.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.store`）。

- [ ] **Step 3: 实现**

- 时间戳统一 `now_iso()`。
- `record_proactive_event` 调 `events.record_event(..., platform="qq", source="proactive", occurred_at=now)`。
- `recent_channel_events`：`SELECT content FROM events WHERE person_key=? AND channel_id=? AND source IN ('user_message','assistant_message','proactive') ORDER BY id DESC LIMIT ?`，返回新→旧。
- `touch_last_proactive`：`get_state`；没有则返回；`replace(state, last_proactive_at=stamp)`；`save_state(conn, state, commit=commit)`。
- `last_sent_at`：`SELECT MAX(created_at) FROM proactive_log WHERE target_key=? AND status='sent'`，可选 `persona_key` 过滤。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_store.py -v`
Expected: PASS。

---

### Task 11: 发送前检查

**Files:**
- Create: `backend/app/proactive/checks.py`
- Test: `backend/tests/test_proactive_checks.py`

**Interfaces:**
- Produces:
  - `in_quiet_hours(now: str, ranges: list[str], timezone_name=DEFAULT_TIMEZONE) -> bool`
  - `too_soon(last_at: str | None, now: str, minutes: int) -> bool`
  - `over_limit(count: int, limit: int) -> bool`
  - `recently_active(conn, *, person_key, channel_id, now, minutes) -> bool`
  - `is_repetitive(text: str, recent: list[str], *, threshold=0.6) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_checks.py
from __future__ import annotations
from types import SimpleNamespace
import pytest
from app.memory import db
from app.proactive.checks import (
    in_quiet_hours, is_repetitive, over_limit, recently_active, too_soon,
)

NOW = "2026-11-09T15:30:00+00:00"  # 北京时间 23:30


def test_quiet_hours_wrap_midnight():
    assert in_quiet_hours(NOW, ["23:00-07:00"]) is True
    assert in_quiet_hours("2026-11-09T22:59:00+00:00", ["23:00-07:00"]) is True
    assert in_quiet_hours("2026-11-09T04:00:00+00:00", ["23:00-07:00"]) is False
    assert in_quiet_hours("2026-11-09T04:00:00+00:00", ["00:00-07:00"]) is True


def test_quiet_hours_range_without_wrap():
    assert in_quiet_hours("2026-11-09T05:00:00+00:00", ["12:00-14:00"]) is True
    assert in_quiet_hours("2026-11-09T03:00:00+00:00", ["12:00-14:00"]) is False


def test_too_soon_zero_means_unlimited():
    assert too_soon(NOW, NOW, 0) is False
    assert too_soon(None, NOW, 180) is False


def test_too_soon_minutes():
    assert too_soon("2026-11-09T15:00:00+00:00", NOW, 180) is True
    assert too_soon("2026-11-09T11:00:00+00:00", NOW, 180) is False


def test_over_limit_zero_means_unlimited():
    assert over_limit(999, 0) is False
    assert over_limit(3, 3) is True
    assert over_limit(2, 3) is False


def test_is_repetitive():
    assert is_repetitive("晚上好呀，今天累不累", ["晚上好呀，今天累不累"]) is True
    assert is_repetitive("明天一起吃饭吗", ["今天天气不错"]) is False


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4),
    )
    db.init_schema()
    conn = db.connect()
    yield conn
    conn.close()


def test_recently_active_only_counts_user_messages(conn):
    from app.memory.events import record_event
    record_event(conn, person_key="wp:7", platform="qq", channel_id="private:10001",
                 scope="personal", audience=["wp:7"], source="user_message",
                 content="在吗", occurred_at="2026-11-09T15:25:00+00:00")
    assert recently_active(conn, person_key="wp:7", channel_id="private:10001",
                           now=NOW, minutes=10) is True
    assert recently_active(conn, person_key="wp:7", channel_id="private:10001",
                           now=NOW, minutes=3) is False
    assert recently_active(conn, person_key="wp:7", channel_id="private:99999",
                           now=NOW, minutes=10) is False
```

`recently_active` 比较的是 `occurred_at`，不是写入时刻。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_checks.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.checks`）。

- [ ] **Step 3: 实现**

- `in_quiet_hours`：`parse_iso(now).astimezone(_zone(tz))` 取 `HH:MM` 分钟数；`start <= end` 时 `start <= m < end`，否则 `m >= start or m < end`；格式坏的区间跳过。
- `too_soon`：`minutes <= 0 or last_at is None` → `False`；否则 `parse_iso(now) - parse_iso(last_at) < minutes*60`。
- `over_limit`：`limit > 0 and count >= limit`。
- `recently_active`：`SELECT 1 FROM events WHERE person_key=? AND channel_id=? AND source='user_message' AND occurred_at >= ? LIMIT 1`，阈值 `now - minutes`。
- `is_repetitive`：去空白和标点后取字符 bigram 集合，Jaccard ≥ threshold；任一方为空返回 `False`。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_checks.py -v`
Expected: PASS。

---

### Task 12: 主动消息生成器

**Files:**
- Create: `backend/app/proactive/message.py`
- Test: `backend/tests/test_proactive_writer.py`

**Interfaces:**
- Produces:
  - `ComposeResult(text: str, status: str, latency_ms: int = 0)`（`status ∈ ok/timeout/error/empty`）
  - `ProactiveWriter(model=None)`，`async compose(*, persona, kind, state_context, recent, topic_hint="", channel="private", group_topic="", timeout=10.0) -> ComposeResult`
  - `build_proactive_prompt(...) -> str`：`channel="group"` 时**忽略 `recent`**，只使用 `group_topic`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_writer.py
from __future__ import annotations
import asyncio, functools
import pytest
from app.persona.models import PersonaVersion
from app.proactive.message import ProactiveWriter, build_proactive_prompt


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def persona():
    return PersonaVersion.model_validate({
        "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
        "name": "梅林", "core": "你在博客里做小助手，说话简短。",
    })


class FakeModel:
    def __init__(self, content, *, exc=None):
        self.content, self.exc = content, exc

    async def ainvoke(self, prompt):
        if self.exc:
            raise self.exc
        return type("Msg", (), {"content": self.content})()


@async_test
async def test_compose_returns_text():
    writer = ProactiveWriter(FakeModel("晚上好呀，今天累不累？"))
    result = await writer.compose(
        persona=persona(), kind="care", state_context="心情一般",
        recent=["今天加班"], timeout=1.0,
    )
    assert result.status == "ok"
    assert result.text == "晚上好呀，今天累不累？"


@async_test
async def test_compose_empty_text():
    writer = ProactiveWriter(FakeModel("   "))
    result = await writer.compose(
        persona=persona(), kind="missing", state_context="", recent=[], timeout=1.0,
    )
    assert result.status == "empty"
    assert result.text == ""


@async_test
async def test_compose_error_is_fail_open():
    writer = ProactiveWriter(FakeModel("", exc=RuntimeError("boom")))
    result = await writer.compose(
        persona=persona(), kind="missing", state_context="", recent=[], timeout=1.0,
    )
    assert result.status == "error"
    assert result.text == ""


def test_group_prompt_uses_topic_and_drops_private_recent():
    prompt = build_proactive_prompt(
        persona=persona(), kind="spontaneous", state_context="",
        recent=["私聊里说过的秘密"], topic_hint="", channel="group",
        group_topic="大家在聊新番",
    )
    assert "大家在聊新番" in prompt
    assert "私聊里说过的秘密" not in prompt
    assert "不要提及任何私聊内容" in prompt
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_writer.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.message`）。

- [ ] **Step 3: 实现**

- `_model()` 与 `Appraiser` 同构：注入优先，否则 `build_chat_model(runtime.model, temperature=0.9, max_tokens=200)`。
- `compose`：`asyncio.wait_for(self._model().ainvoke(prompt), timeout=max(1.0, timeout))`；`text_of(response).strip()`；超时 → `timeout`；其它异常 → `error`（`logger.warning`）；空文本 → `empty`；成功 → `ok`。
- `build_proactive_prompt` 中文模板，包含：人设 `core`、`state_context`、`channel="group"` 时的 `group_topic`（且**不渲染 `recent`**）、私聊时的 `recent[-4:]`、念头类型说明、`topic_hint`、渠道要求。
- 群聊要求固定包含“不要提及任何私聊内容，不要 @ 任何人”；私聊要求“像给熟人发消息，不超过 80 字，不要写旁白/括号动作/引号”。
- 念头说明映射：`event_followup` 追问没聊完的事；`missing` 表达想念；`care` 关心；`spontaneous` 分享刚想到的；`companionship` 单纯想聊两句。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_writer.py -v`
Expected: PASS。

---

### Task 13: 忽略结算

**Files:**
- Create: `backend/app/proactive/settle.py`
- Test: `backend/tests/test_proactive_settle.py`

**Interfaces:**
- Consumes: `record_proactive`（Task 10）、`ProactiveDynamics`（Task 6）、既有 `apply_event` 与 `role_for`。
- Produces:
  - `settle_ignores(conn, *, persona_key, dynamics, now=None, timezone_name=DEFAULT_TIMEZONE) -> dict[str, int]`（键 `answered` / `ignored`）
  - 内部 `derived_attention(conn, person_key, channel_id, sent_at) -> float`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_settle.py
from __future__ import annotations
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from app.memory import db
from app.persona.models import PersonaVersion
from app.proactive.settle import settle_ignores
from app.proactive.store import record_proactive
from app.relationship.state import RelationshipState, get_state, save_state

NOW = "2026-11-09T12:00:00+00:00"


def hours_ago(hours):
    return (datetime.fromisoformat(NOW) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4),
    )
    db.init_schema()
    conn = db.connect()
    yield conn
    conn.close()


def dynamics():
    return PersonaVersion.model_validate({
        "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
        "name": "梅林", "core": "C",
    }).dynamics


def seed_state(conn, attention=1.0):
    save_state(conn, RelationshipState(
        persona_key="merlin", person_key="wp:7", affection=30.0, missing=50.0,
        security=70.0, patience=70.0, valence=0.0, energy=0.8, fatigue=0.0,
        reply_debt=0.0, last_interaction_at=None, last_proactive_at=None,
        last_reply_at=None, attention_confidence=attention,
        updated_at=hours_ago(1), conflict_stage="normal",
        stage_since=hours_ago(1), scar_level=0.0,
    ))


def seed_sent(conn, created_at):
    return record_proactive(
        conn, persona_key="merlin", target_key="wp:7",
        channel_id="private:10001", kind="missing", thought="在忙吗",
        status="sent", now=created_at,
    )


def insert_user_message(conn, channel_id, occurred_at):
    conn.execute(
        """INSERT INTO events (occurred_at, person_key, platform, channel_id,
               scope, audience, audience_key, source, content, created_at)
           VALUES (?, 'wp:7', 'qq', ?, 'personal', '["wp:7"]',
                   'wp:7', 'user_message', '刚看到', ?)""",
        (occurred_at, channel_id, occurred_at),
    )
    conn.commit()


def test_ignored_applies_penalty_times_attention(conn):
    seed_state(conn, attention=1.0)
    row_id = seed_sent(conn, hours_ago(30))
    result = settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    assert result == {"answered": 0, "ignored": 1}
    assert get_state(conn, "merlin", "wp:7").affection == pytest.approx(29.8)
    status = conn.execute(
        "SELECT status FROM proactive_log WHERE id=?", (row_id,)
    ).fetchone()["status"]
    assert status == "ignored"


def test_zero_attention_does_not_drop_affection(conn):
    seed_state(conn, attention=0.0)
    seed_sent(conn, hours_ago(30))
    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    assert get_state(conn, "merlin", "wp:7").affection == pytest.approx(30.0)


def test_user_reply_marks_answered(conn):
    seed_state(conn)
    row_id = seed_sent(conn, hours_ago(30))
    insert_user_message(conn, "private:10001", hours_ago(29))
    result = settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    assert result == {"answered": 1, "ignored": 0}
    assert get_state(conn, "merlin", "wp:7").affection == pytest.approx(30.0)
    assert conn.execute(
        "SELECT status FROM proactive_log WHERE id=?", (row_id,)
    ).fetchone()["status"] == "answered"


def test_recent_message_is_not_settled(conn):
    seed_state(conn)
    seed_sent(conn, hours_ago(1))
    result = settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    assert result == {"answered": 0, "ignored": 0}


def test_settle_twice_does_not_double_penalize(conn):
    seed_state(conn)
    seed_sent(conn, hours_ago(30))
    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    after_first = get_state(conn, "merlin", "wp:7").affection
    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    assert get_state(conn, "merlin", "wp:7").affection == pytest.approx(after_first)


def test_activity_elsewhere_gives_partial_confidence(conn):
    seed_state(conn, attention=0.0)
    seed_sent(conn, hours_ago(30))
    insert_user_message(conn, "group:888", hours_ago(29))
    settle_ignores(conn, persona_key="merlin", dynamics=dynamics(), now=NOW)
    assert get_state(conn, "merlin", "wp:7").affection == pytest.approx(29.96)
```

`29.96 = 30 - 0.2 * 0.2`。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_settle.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.settle`）。

- [ ] **Step 3: 实现**

- 选出 `status='sent'`、`channel_id LIKE 'private:%'`、`created_at < now - grace_hours` 的 `proactive_log` 行。
- 对每行：
  1. `derived_attention`：`events` 中该 `person_key`、`occurred_at > row.created_at` 的 `user_message`。有同 `channel_id` 的 → `1.0`（answered）；只有别的渠道 → `0.2`；没有 → `0.0`。
  2. `confidence = max(state.attention_confidence, derived)`。
  3. 若 `derived == 1.0`：`UPDATE status='answered'`，不扣分。
  4. 否则 `UPDATE status='ignored'`；`cfg = dynamics.proactive`；`role = role_for(conn, person_key)`；`apply_event(..., kind="proactive_ignored", d_affection_points=-cfg.ignore_affection_points * confidence, d_security=-cfg.ignore_security_points * confidence, confidence=confidence, note="proactive ignored", touch_interaction=False, now=now)`。`confidence == 0` 时跳过 `apply_event`。
- 状态更新与扣分在同一个事务里：方法开头 `conn.execute("BEGIN IMMEDIATE")`，结束 `conn.commit()`；异常时 `conn.rollback()` 并向上抛（调度器 fail-open）。
- 只处理 `person_key` 以 `wp:` 开头的行；其它行直接标 `ignored`，不扣分。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_settle.py -v`
Expected: PASS。

---

### Task 14: 私聊主动调度器

**Files:**
- Create: `backend/app/proactive/scheduler.py`
- Test: `backend/tests/test_proactive_scheduler.py`

**Interfaces:**
- Consumes: `candidates`、`pick_thought`（Task 9）、`record_proactive` / `record_proactive_event` / `touch_last_proactive` / `recent_thoughts` / `sent_count_since` / `last_sent_at`（Task 10）、`in_quiet_hours` / `too_soon` / `over_limit` / `recently_active` / `is_repetitive`（Task 11）、`ProactiveWriter`（Task 12）、`open_topic` / `mark_topic_sent`（Task 8）、`settle_ignores`（Task 13）。
- Produces:

```python
@dataclass(frozen=True, slots=True)
class SendResult:
    target: str
    channel: str
    kind: str
    status: str      # sent | skipped | failed
    reason: str

class ProactiveScheduler:
    def __init__(self, settings, *, sender, credits, writer=None,
                 persona=None, now_fn=None)
    async def run_once(self, now: str | None = None) -> list[SendResult]
```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_scheduler.py
from __future__ import annotations
import asyncio, functools
from dataclasses import replace
import pytest
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.proactive.message import ComposeResult
from app.proactive.scheduler import ProactiveScheduler
from app.relationship.state import RelationshipState, save_state

NOW = "2026-11-09T04:00:00+00:00"  # 北京时间 12:00


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "r.json"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    get_settings.cache_clear()
    from app.runtime_settings import get_runtime_store
    get_runtime_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_runtime_store.cache_clear()


class FakeSender:
    def __init__(self): self.private, self.group = [], []
    async def send_private_message(self, uid, text): self.private.append((uid, text))
    async def send_group_message(self, gid, text): self.group.append((gid, text))


class FailSender(FakeSender):
    async def send_private_message(self, uid, text):
        raise RuntimeError("network down")


class FakeWriter:
    def __init__(self, text="在忙吗？"):
        self.text = text
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        return ComposeResult(self.text, "ok", 1)


def persona():
    return PersonaVersion.model_validate({
        "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
        "name": "梅林", "core": "C",
        "dynamics": {"safety": {"quiet_hours": ["01:00-02:00"]}},
    })


def enable_runtime(**overrides):
    from app.runtime_settings import get_runtime_store
    patch = {"proactive_enabled": True}
    patch.update(overrides)
    get_runtime_store().update(patch)


def seed_person(tmp_path, *, qq="10001", user_id=7, **state_overrides):
    store = CreditStore(str(tmp_path / "q.sqlite3"))
    store.set_bind_code(user_id, "unused")
    store.bind_qq(qq, "unused")
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        base = RelationshipState(
            persona_key="merlin", person_key=f"wp:{user_id}", affection=80.0,
            missing=90.0, security=90.0, patience=90.0, valence=0.2,
            energy=0.9, fatigue=0.0, reply_debt=0.2,
            last_interaction_at="2026-11-06T04:00:00+00:00",
            last_proactive_at=None, last_reply_at=None,
            attention_confidence=1.0, updated_at=NOW,
            conflict_stage="normal", stage_since=NOW, scar_level=0.0,
        )
        save_state(conn, replace(base, **state_overrides))
        conn.execute(
            """INSERT INTO persons (person_key, wp_user_id, qq_uin,
                   first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            (f"wp:{user_id}", user_id, qq, NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    return store


def make_scheduler(tmp_path, *, sender=None, writer=None, store=None):
    settings = Settings(qq_enabled=True, qq_mock_mode=True,
                        quota_timezone="Asia/Shanghai")
    return ProactiveScheduler(
        settings, sender=sender or FakeSender(),
        credits=store or CreditStore(str(tmp_path / "q.sqlite3")),
        writer=writer or FakeWriter(), persona=persona(),
    )


@async_test
async def test_disabled_sends_nothing(tmp_path):
    store = seed_person(tmp_path)
    scheduler = make_scheduler(tmp_path, store=store)
    assert await scheduler.run_once(now=NOW) == []


@async_test
async def test_sends_one_private_message_and_records(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now=NOW)
    assert len(results) == 1 and results[0].status == "sent"
    assert sender.private == [(10001, "在忙吗？")]
    conn = memory_db.connect()
    try:
        log = conn.execute("SELECT * FROM proactive_log").fetchone()
        assert log["status"] == "sent" and log["target_key"] == "wp:7"
        event = conn.execute("SELECT * FROM events WHERE source='proactive'").fetchone()
        assert event["channel_id"] == "private:10001"
        state = conn.execute(
            "SELECT last_proactive_at FROM relationship_state WHERE person_key='wp:7'"
        ).fetchone()
        assert state["last_proactive_at"] == NOW
    finally:
        conn.close()


@async_test
async def test_quiet_hours_skips(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now="2026-11-08T17:30:00+00:00")  # 北京 01:30
    assert all(r.status == "skipped" for r in results)
    assert sender.private == []


@async_test
async def test_recently_active_skips(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO events (occurred_at, person_key, platform, channel_id,
                   scope, audience, audience_key, source, content, created_at)
               VALUES (?, 'wp:7', 'qq', 'private:10001', 'personal', '["wp:7"]',
                       'wp:7', 'user_message', '在吗', ?)""",
            (NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    scheduler = make_scheduler(tmp_path, store=store)
    results = await scheduler.run_once(now="2026-11-09T04:05:00+00:00")
    assert results and results[0].status == "skipped"
    assert results[0].reason == "recently_active"


@async_test
async def test_min_interval_skips(tmp_path):
    store = seed_person(tmp_path, last_proactive_at="2026-11-09T03:00:00+00:00")
    enable_runtime()
    scheduler = make_scheduler(tmp_path, store=store)
    results = await scheduler.run_once(now=NOW)
    assert results[0].status == "skipped"
    assert results[0].reason == "min_interval"


@async_test
async def test_daily_limit_skips(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime(proactive_per_user_daily_limit=1)
    scheduler = make_scheduler(tmp_path, store=store)
    await scheduler.run_once(now=NOW)
    results = await scheduler.run_once(now="2026-11-09T07:00:00+00:00")
    assert results and all(r.reason == "daily_limit" for r in results)


@async_test
async def test_compose_failure_logs_skipped(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()

    class BadWriter:
        async def compose(self, **kwargs):
            return ComposeResult("", "error", 1)

    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store, writer=BadWriter())
    results = await scheduler.run_once(now=NOW)
    assert results[0].status == "skipped"
    assert results[0].reason == "compose_error"
    assert sender.private == []


@async_test
async def test_send_failure_is_logged_not_raised(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    scheduler = make_scheduler(tmp_path, sender=FailSender(), store=store)
    results = await scheduler.run_once(now=NOW)
    assert results[0].status == "failed"
    conn = memory_db.connect()
    try:
        log = conn.execute("SELECT * FROM proactive_log").fetchone()
        assert log["status"] == "failed"
        state = conn.execute(
            "SELECT last_proactive_at FROM relationship_state WHERE person_key='wp:7'"
        ).fetchone()
        assert state["last_proactive_at"] is None
    finally:
        conn.close()


@async_test
async def test_rebound_qq_is_never_messaged(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    store.set_bind_code(8, "another")
    store.unbind_user(7)
    store.bind_qq("10001", "another")
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now=NOW)
    assert sender.private == []
    assert all(r.status == "skipped" for r in results)


@async_test
async def test_one_private_message_per_tick(tmp_path):
    store = seed_person(tmp_path)
    seed_person(tmp_path, qq="10002", user_id=8)
    enable_runtime()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now=NOW)
    assert len([r for r in results if r.status == "sent"]) == 1
    assert len(sender.private) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_scheduler.py -v`
Expected: FAIL（`ModuleNotFoundError: app.proactive.scheduler`）。

- [ ] **Step 3: 实现**

`run_once(now=None)`：

1. `runtime = get_runtime_store().load()`；`if not runtime.proactive_enabled: return []`。
2. `persona = self._persona`（注入优先，否则 `get_persona_store().load_active()`）；`dynamics = persona.dynamics`。
3. 开连接、`init_schema`；`settle_ignores(...)`，异常只 warning。
4. `candidates(...)`。
5. 遍历前 5 个：
   - `binding = self.credits.binding_of_qq(cand.qq_uin)`；`binding is None or binding.user_id != cand.wp_user_id` → `SendResult(skipped, "binding_changed")`，**不写日志**，继续。
   - `thought = pick_thought(cand.thoughts, runtime.proactive_speak_threshold)`；`None` → 静默跳过（不写日志）。
   - 检查顺序：`in_quiet_hours` → `too_soon(state.last_proactive_at, ...)` → 该人 `sent_count_since` ≥ `per_user_daily_limit` → 全局 `sent_count_since` ≥ `global_daily_limit` → `recently_active(channel_id=f"private:{uin}", minutes=dynamics.proactive.recent_active_minutes)`。任一命中：`record_proactive(status="skipped", reason=...)`，返回并继续下一个候选。
   - **待续话题优先**：若 `thought.kind == "event_followup"`，`topic_row = open_topic(...)`；有则 `topic_hint = topic_row["topic"]`。
   - `compose(...)`；`status != "ok"` → `record_proactive(status="skipped", reason=f"compose_{status}")`，返回并**停止本次 tick 的私聊尝试**。
   - `is_repetitive(text, recent_thoughts(...), threshold=dynamics.proactive.repeat_similarity)` → `record_proactive(status="skipped", reason="repetitive")`，返回，停止。
   - 发送：`await self.sender.send_private_message(int(uin), text)`；异常 → `record_proactive(status="failed", reason=str(exc)[:200])`，返回，**不改** `last_proactive_at`、**不改**待续话题状态、**不重试**。
   - 成功 → `event_id = record_proactive_event(..., scope="personal", audience=[person_key], content=text)`；`record_proactive(status="sent", event_id=event_id)`；`touch_last_proactive(...)`；若有 `topic_row` → `mark_topic_sent(conn, topic_row["id"])`；返回 `SendResult(sent, kind)`。
6. 私聊本轮结束后再跑 `_group_pass`（Task 15）；本任务先返回私聊结果。

`day_start_iso(stamp, timezone_name)` 复用 `app.relationship.appraisal_budget` 里的实现。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_scheduler.py -v`
Expected: PASS。

---

### Task 15: 群主动与常驻循环

**Files:**
- Modify: `backend/app/proactive/scheduler.py`（`_group_pass`）
- Create: `backend/app/proactive/runner.py`
- Modify: `backend/app/main.py`（`lifespan`）、`backend/app/channels/qq/adapter.py`（群注意力）
- Test: `backend/tests/test_proactive_group.py`、`backend/tests/test_proactive_runner.py`

**Interfaces:**
- Produces:
  - `ProactiveScheduler._group_pass(...)`（内部方法）
  - `SchedulerRunner(scheduler, *, tick_seconds: float | None = None)`，`async start()`、`async stop()`
  - `create_runner(settings) -> SchedulerRunner | None`（`qq_enabled=False` 时返回 `None`）
  - `QQAdapter._record_group_attention(incoming, binding) -> None`（只在 12h 内有群主动时写 0.6）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_proactive_group.py
from __future__ import annotations
import asyncio, functools
import pytest
from app.channels.qq.group_policy import GroupPolicy, save_policy
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.proactive.message import ComposeResult
from app.proactive.scheduler import ProactiveScheduler

NOW = "2026-11-09T04:00:00+00:00"


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "r.json"))
    get_settings.cache_clear()
    from app.runtime_settings import get_runtime_store
    get_runtime_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_runtime_store.cache_clear()


class FakeSender:
    def __init__(self): self.private, self.group = [], []
    async def send_private_message(self, uid, text): self.private.append((uid, text))
    async def send_group_message(self, gid, text): self.group.append((gid, text))


class FakeWriter:
    def __init__(self): self.calls = []
    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        return ComposeResult("大家在聊新番，我也在看", "ok", 1)


def enable(**patch):
    from app.runtime_settings import get_runtime_store
    get_runtime_store().update({"proactive_enabled": True, **patch})


def set_policy(*, proactive=True):
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        save_policy(conn, 888, GroupPolicy("all", "always", proactive))
    finally:
        conn.close()


def add_group_topic(text="最近有什么新番好看"):
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO events (occurred_at, person_key, platform, channel_id,
                   scope, audience, audience_key, source, content, created_at)
               VALUES (?, 'wp:7', 'qq', 'group:888', 'group', '["group:888"]',
                       'group:888', 'user_message', ?, ?)""",
            (NOW, text, NOW),
        )
        conn.commit()
    finally:
        conn.close()


def make_scheduler(tmp_path, sender):
    settings = Settings(qq_enabled=True, qq_allowed_groups="888",
                        qq_mock_mode=True, quota_timezone="Asia/Shanghai")
    return ProactiveScheduler(
        settings, sender=sender,
        credits=CreditStore(str(tmp_path / "q.sqlite3")),
        writer=FakeWriter(),
        persona=PersonaVersion.model_validate({
            "schema": "l2d.persona/v1", "id": "merlin", "version": 1,
            "name": "梅林", "core": "C",
            "dynamics": {"safety": {"quiet_hours": ["01:00-02:00"]}},
        }),
    )


@async_test
async def test_group_proactive_disabled_by_policy(tmp_path):
    enable()
    set_policy(proactive=False)
    add_group_topic()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender).run_once(now=NOW)
    assert sender.group == []
    assert all(r.channel != "group:888" or r.status == "skipped" for r in results)


@async_test
async def test_group_without_topic_skips(tmp_path):
    enable()
    set_policy()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender).run_once(now=NOW)
    group_results = [r for r in results if r.channel == "group:888"]
    assert group_results and group_results[0].reason == "no_topic"
    assert sender.group == []


@async_test
async def test_group_sends_one_and_records(tmp_path):
    enable()
    set_policy()
    add_group_topic()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender).run_once(now=NOW)
    assert sender.group == [(888, "大家在聊新番，我也在看")]
    log = [r for r in results if r.channel == "group:888"]
    assert log and log[0].status == "sent"
    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM proactive_log WHERE target_key='group:888'"
        ).fetchone()
        assert row["status"] == "sent"
        event = conn.execute(
            "SELECT * FROM events WHERE channel_id='group:888' AND source='proactive'"
        ).fetchone()
        assert event["audience_key"] == "group:888"
    finally:
        conn.close()


@async_test
async def test_group_interval_skips(tmp_path):
    enable()
    set_policy()
    add_group_topic()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender)
    await scheduler.run_once(now=NOW)
    results = await scheduler.run_once(now="2026-11-09T05:00:00+00:00")
    group_results = [r for r in results if r.channel == "group:888"]
    assert group_results and group_results[0].reason == "min_interval"
    assert len(sender.group) == 1


@async_test
async def test_group_prompt_has_no_private_content(tmp_path):
    enable()
    set_policy()
    add_group_topic()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender)
    await scheduler.run_once(now=NOW)
    group_call = scheduler.writer.calls[-1]
    assert group_call["channel"] == "group"
    assert "最近有什么新番好看" in group_call["group_topic"]
```

`tests/test_proactive_runner.py`：

```python
from __future__ import annotations
import asyncio, functools
from app.config import Settings
from app.proactive.runner import SchedulerRunner, create_runner


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


class CountingScheduler:
    def __init__(self): self.calls = 0
    async def run_once(self, now=None):
        self.calls += 1
        return []


def test_create_runner_respects_qq_enabled():
    assert create_runner(Settings(qq_enabled=False)) is None
    assert create_runner(Settings(qq_enabled=True, qq_mock_mode=True)) is not None


@async_test
async def test_runner_starts_and_stops_cleanly():
    scheduler = CountingScheduler()
    runner = SchedulerRunner(scheduler, tick_seconds=0.01)
    await runner.start()
    await asyncio.sleep(0.05)
    await runner.stop()
    assert scheduler.calls >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_group.py tests/test_proactive_runner.py -v`
Expected: FAIL（`_group_pass` 未实现、`runner` 模块不存在）。

- [ ] **Step 3: 实现**

`_group_pass(conn, runtime, dynamics, persona, stamp)`：
- 遍历 `sorted(self.settings.qq_allowed_group_ids)`；`load_policy(...)`；`policy.proactive` 为假 → `SendResult(skipped, "policy_disabled")` 且不写日志。
- `in_quiet_hours` → `quiet_hours`。
- `last_sent_at(conn, target_key=f"group:{gid}")` + `too_soon(..., group_interval)` → `min_interval`。
- `sent_count_since(... target_key=f"group:{gid}")` ≥ `group_daily_limit` → `daily_limit`。
- 群话题：`SELECT content FROM events WHERE channel_id=? AND source='user_message' AND occurred_at >= now-24h ORDER BY id DESC LIMIT 10`；为空 → `no_topic`。
- `compose(channel="group", group_topic="\n".join(topics), state_context="", recent=[], kind="spontaneous", topic_hint="")`；失败 → `compose_<status>`。
- 重复检查 vs `recent_thoughts(target_key=f"group:{gid}")`。
- 发送 `send_group_message`；失败 → `record_proactive(status="failed")`，不改任何时间戳。
- 成功 → `record_proactive_event(person_key=f"group:{gid}", channel_id=f"group:{gid}", scope="group", audience=[f"group:{gid}"])`；`record_proactive(status="sent", event_id=...)`。
- 每个 tick 最多一个群；第一个成功或失败后停止遍历。

`runner.py`：

```python
class SchedulerRunner:
    def __init__(self, scheduler, *, tick_seconds=None):
        self.scheduler = scheduler
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self.scheduler.run_once()
            except Exception:
                logger.warning("主动消息 tick 失败", exc_info=True)
            tick = self.tick_seconds
            if tick is None:
                tick = get_runtime_store().load().proactive_tick_minutes * 60
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
            except TimeoutError:
                continue

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def create_runner(settings) -> SchedulerRunner | None:
    if not settings.qq_enabled:
        return None
    scheduler = ProactiveScheduler(
        settings, sender=build_onebot_client(settings), credits=get_credit_store()
    )
    return SchedulerRunner(scheduler)
```

`main.py`：

```python
from contextlib import asynccontextmanager
from app.proactive.runner import create_runner

@asynccontextmanager
async def lifespan(app):
    runner = create_runner(settings)
    if runner is not None:
        await runner.start()
    try:
        yield
    finally:
        if runner is not None:
            await runner.stop()

app = FastAPI(..., lifespan=lifespan)
```

适配器群注意力：在 `handle_event` 群分支拿到 `binding` 后：

```python
if binding is not None:
    await asyncio.to_thread(self._record_group_attention, incoming, binding)
```

`_record_group_attention`：
1. 查 `last_sent_at(conn, target_key=f"group:{gid}")`；为空或早于 12 小时 → 直接返回（不写状态）。
2. 否则 `person_key, role = relationship_subject(...)`；`record_attention(..., confidence=0.6)`。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_group.py tests/test_proactive_runner.py tests/test_qq_group_policy_flow.py -v`
Expected: PASS。

---

### Task 16: WordPress 设置页、文档与全量回归

**Files:**
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php:1091-1144`（保存规则）、`:1740-1843`（表单）
- Modify: `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`（§4.2、§5.5、§8、§9）
- Modify: `docs/architecture.md`
- Modify: `backend/personas.example/README.md`
- Test: 全量 pytest

**Interfaces:**
- Consumes: Task 5 的 9 个字段（名字必须完全一致）。

- [ ] **Step 1: WordPress 保存规则**

在 `$rules` 追加：

```php
'proactive_tick_minutes'            => array( 5, 120 ),
'proactive_min_interval_minutes'    => array( 0, 10080 ),
'proactive_per_user_daily_limit'    => array( 0, 100 ),
'proactive_global_daily_limit'      => array( 0, 100000 ),
'proactive_group_interval_minutes'  => array( 0, 10080 ),
'proactive_group_daily_limit'       => array( 0, 100 ),
```

布尔项追加：

```php
$patch['proactive_enabled'] = ! empty( $_POST['proactive_enabled'] );
```

两个阈值是小数，照 `appraisal_timeout_seconds` 的写法单独校验：正则 `/^\d+(\.\d+)?$/`，范围 `0`–`1`，消息“主动消息阈值必须在 0 到 1 之间”。

- [ ] **Step 2: WordPress 表单分区**

在“QQ 关系评估”表格最后一行之后（`</table>` 前）加“QQ 主动消息”分区：

| 控件 | name | 默认 | 说明 |
|---|---|---|---|
| 复选框 | `proactive_enabled` | 未勾选 | 总开关 |
| number 5–120 | `proactive_tick_minutes` | 30 | 多久检查一次，改完下一轮生效 |
| number 0–1 step 0.01 | `proactive_think_threshold` | 0.35 | 低于它连念头都不生成 |
| number 0–1 step 0.01 | `proactive_speak_threshold` | 0.72 | 达到才进入发送前检查 |
| number 0–10080 | `proactive_min_interval_minutes` | 180 | 0 = 不限 |
| number 0–100 | `proactive_per_user_daily_limit` | 3 | 0 = 不限 |
| number 0–100000 | `proactive_global_daily_limit` | 20 | 全体用户合计，0 = 不限 |
| number 0–10080 | `proactive_group_interval_minutes` | 240 | 0 = 不限 |
| number 0–100 | `proactive_group_daily_limit` | 6 | 0 = 不限 |

说明文字写清：主动消息不占用户聊天额度；免打扰来自人设 `dynamics.safety.quiet_hours`；群主动还要在群策略里把 `proactive` 设为 true。

- [ ] **Step 3: 文档**

- spec §5.5 改为：群聊默认仍用 `trigger` + `reply_policy`；**被绑定成员 @ 或前缀点名时**，用该成员的关系状态做一次概率判断，但下限抬到 `dynamics.group.reply_floor`（默认 0.90）——平时几乎必回，只有极端状态才可能不回；回复只带定性关系标签、不带私聊原文。群内其余消息不进入概率模型，且**不规划**“全群概率回复”。
- spec §4.2 注一句：“4b 只实现 1.0 / 0.6 / 0.2 三种证据，`get_user_status` 在线 0.1 暂未接入。”
- spec §8：补 `pending_topics`、`qq_group_policies`、`proactive_log` 的字段。
- spec §9：4b 标记“本地完成”；群内主动开私聊（group opener）列为候选——“群内被点名并回复后，是否借机发一条私聊，等试用后再决定”。
- `docs/architecture.md`：新增 `app/proactive/` 与 `app/relationship/group_turn.py` 的职责表；说明调度器只在 `QQ_ENABLED=true` 且 `proactive_enabled=true` 时发消息，单 worker 假设。
- `personas.example/README.md`：补 `dynamics.proactive` 与 `dynamics.group` 的字段、默认值、建议范围。

- [ ] **Step 4: 全量回归**

Run: `cd backend && .venv/Scripts/python.exe -m pytest -q`
Expected: 全部通过（新增约 65 个测试；1 个既有 skip 保持不变）。

- [ ] **Step 5: 人工核对 PHP**

本地无 PHP，不能 `php -l`。逐项核对：表单 `name` 与 `$rules` 键完全一致、布尔项存在即 true、两个阈值是 float 分支、保存后 `clear_backend_cache()` 仍被调用。

---

## 暂缓/待办

以下不进入 4b，等站长在真实群里试用指名判断之后再说：

- **群内主动开私聊（group opener）**：被指名并在群里回复后，是否“借此”给这个人发一条私聊。已想清楚的约束（若将来做）：人设开关默认关；`affection >= 40` 且 `security >= 40`；免打扰内不发；至少 6 小时没私聊；48 小时冷却；每人每天最多 1 次；`proactive_log.kind="group_followup"`；私聊内容不得引用群聊原文以外的私密内容。**为什么现在不做**：群回复本身已有明确信号，再叠一条私聊会让“被点名”变成引流；先看试用反馈。
- **群聊全量概率回复**：非指名群消息也走 `p0` 模型。**不再规划**——群是多人环境，单人的 `affection` 不代表群；被点名时才用关系状态，且下限 0.90，其余一律按 `trigger` / `reply_policy` 走。
- **群内被指名但不回复时的表情/短回应**：4b 的沉默就是完全沉默，避免“半回”造成语义混乱。

## 自审记录

- **Spec 覆盖**：§5.2 三开关 → Task 2/3/4；§5.5 群聊范围 → Task 7/16（窄概率判断）；§5.4 待续话题 → Task 8；§6.2 候选与评分 → Task 9；§6.3 发送前检查 → Task 11；§6.4 记录 → Task 10/14；§6.5 群主动 → Task 15；§6.6 预算 → Task 5/14；§4.2 注意力 → Task 6/13/15；§8 数据表 → Task 1；§9 路线图 → Task 16。
- **未覆盖且有意为之**：`activity_events` 表（4b 不建，注意力直接写 `relationship_state.attention_confidence`）；`get_user_status` 在线 0.1（无对应接口）；群聊全量概率回复（不规划）；群内主动开私聊（见上节“暂缓/待办”）；后台关系查看/审计 UI（阶段 5）；非指名群回复注入心情（每条群消息多一次状态读取，收益小，暂不采纳）。
- **类型一致性**：`Candidate` 在 Task 9 定义时包含 `state: RelationshipState`，Task 14 直接使用；`SendResult.channel` 群值统一为 `f"group:{gid}"`，私聊为 `f"private:{uin}"`；`target_key` 私聊用 `person_key`，群用 `group:<gid>`；`TriggerMatch.addressed` 由 Task 2 定义、Task 3 传递、Task 7 消费。
- **比例**：计划只钉接口、权重、阈值、状态值和测试断言；函数体留给实现者。
