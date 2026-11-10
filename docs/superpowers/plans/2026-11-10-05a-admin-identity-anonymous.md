# 阶段 5a：后台名单、身份整合与匿名模式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让站长在 WordPress 后台维护关系角色、QQ 白名单、群白名单和匿名开关；QQ 绑定后把旧身份的记忆物理合并到 WP 账号；匿名网页/QQ 可以在不登录、不读写记忆的前提下聊天。

**Architecture:** 后端新增 `person_aliases`、`qq_whitelist`、`qq_allowed_groups`、`anonymous_usage`、`anonymous_guide_log` 五张表；`app/memory/merge.py` 提供与平台无关的 `merge_person` / `delete_person_data`；QQ 适配器把身份分成 `trusted`（已绑定、管理员、QQ 白名单）和 `anonymous`（未信任但匿名开关打开）两条路径；WordPress 后台通过现有 admin JWT 调用新增的 `/admin/roles`、`/admin/qq-whitelist`、`/admin/qq-groups` 接口。

**Tech Stack:** FastAPI、Pydantic v2、SQLite（WAL）、LangChain agent、pytest、WordPress PHP 插件、TypeScript 前端。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`

## Global Constraints

- 所有面向用户的绑定/匿名引导文案**不得出现“额度”二字**，必须符合梅林人设（图书馆、登记、记住你）。测试逐条断言。
- 绑定引导、匿名提示、匿名用尽提示等**全部文案可在 WP 后台编辑**：后台留空 = 用代码默认值；`{place}` 用 `str.replace` 替换（不用 `str.format`，管理员输入花括号不会报 500）；单条上限 500 字。
- `special` 好感上限保持 **75**；`normal` 60、`exception` 85、`owner` 100。
- 匿名回合：不读记忆、不写事件/记忆/关系/评估/待续话题，不进群时间线，不触发主动消息。
- 匿名额度与登录用户额度完全分开；IP 只存哈希，不落原始 IP，不记录聊天内容。
- QQ 白名单优先于匿名开关；移除白名单后数据保持休眠，管理员可显式“彻底删除”。
- 身份合并必须覆盖所有带 `person_key` 的表，以及 `appraisal_usage.scope`、`events/memories.audience`。
- 本计划只做设计评审；**修改服务器端代码前必须得到站长明确同意**。部署由站长手动执行。
- 不提交 `.env`、`backend/data/`、知识库、Live2D/Spine 模型资源。
- TDD：先写失败测试 → 看到它因正确原因失败 → 最小实现 → 全绿。测试命令：
  `cd backend && .venv/Scripts/python.exe -m pytest tests/ -q`
- 注释、文档、后台文案使用中文；代码标识符保持英文。

## Review Focus

1. **没有 `X-Forwarded-For` 或伪造头的匿名网页请求**：应回退到 `request.client.host`，再退到固定字符串；不能 500，也不能把所有未知 IP 算成同一个人之外还绕过个人上限。
2. **白名单在候选生成后被移除**：调度器发送前必须重新检查白名单，不能把主动消息发给已经移出名单的 QQ。
3. **同一 QQ 和多个人设都有关系状态时合并**：每个 `persona_key` 只保留一行，取最近更新时间的一行；日志全部保留，不能因主键冲突中断。
4. **群从白名单移除但 `qq_group_policies` 还有行**：群消息必须被忽略；策略行保留，重新加回时沿用旧三开关。
5. **匿名额度边界值**：个人上限 `0` 表示关闭匿名通道；全站上限 `0` 表示不限全站；两者不能混为一谈。

---

### Task 1: 梅林人设的引导与匿名文案

**Files:**
- Modify: `backend/app/channels/qq/commands.py`
- Test: `backend/tests/test_qq_commands.py`

**Interfaces:**
- Produces:
  - `private_guide(place: str) -> str`
  - `anon_first_hint(place: str) -> str`
  - `anon_memory_hint(place: str) -> str`
  - `GROUP_GUIDE: str`
  - `ANON_QUOTA_USER: str`
  - `ANON_QUOTA_GLOBAL: str`
  - `ANON_GROUP_ASK: str`
  - `GUIDE_TEXTS: tuple[str, ...]`（测试用，包含所有面向用户的引导/匿名文案）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_qq_commands.py` 末尾追加：

```python
def test_guide_copy_uses_library_metaphor_and_never_says_quota():
    texts = [
        commands.private_guide("https://blog.example"),
        commands.anon_first_hint("https://blog.example"),
        commands.anon_memory_hint("https://blog.example"),
        commands.GROUP_GUIDE,
        commands.ANON_QUOTA_USER,
        commands.ANON_QUOTA_GLOBAL,
        commands.ANON_GROUP_ASK,
    ]
    for text in texts:
        assert "额度" not in text
    assert "图书馆" in commands.private_guide("https://blog.example")
    assert "登记" in commands.private_guide("https://blog.example")
    assert "记住" in commands.anon_first_hint("https://blog.example")


def test_guide_texts_covers_every_user_facing_guide():
    assert commands.private_guide("x") in commands.GUIDE_TEXTS
    assert commands.GROUP_GUIDE in commands.GUIDE_TEXTS
    assert commands.ANON_QUOTA_USER in commands.GUIDE_TEXTS
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_commands.py -q`
Expected: FAIL，`AttributeError: module ... has no attribute 'anon_first_hint'`

- [ ] **Step 3: 改写常量**

把 `commands.py` 顶部的引导文案改成（保留 `private_guide` 的 `{place}` 拼接）：

```python
_PRIVATE_GUIDE = (
    "唔……我还不太认识你呢。想让我记住你的话，回图书馆里登记一下吧{place}"
    "——登录之后打开「个人资料」，"
    "那张写着「Live2D 小助手 · QQ 绑定」的小卡片里有一串咒文。"
    "拿到以后回来对我说「/绑定 1234-5678」，我就把你记在心里啦。"
)

GROUP_GUIDE = (
    "这位朋友，我们好像还没正式认识呢。悄悄来私聊我一下，"
    "我告诉你怎么在图书馆里留个名字。"
)

ANON_QUOTA_USER = "今天的悄悄话说到这里吧，明天再来找我。"
ANON_QUOTA_GLOBAL = "今天来找我的人太多啦，我有点累了，明天再聊吧。"
ANON_GROUP_ASK = "想让我记住你的话，私下来图书馆里登记一下吧。"


def anon_first_hint(place: str) -> str:
    return (
        "对了，我现在的记性只到今晚。想让我一直记得你，"
        f"去图书馆里登记一下吧{place}。"
    )


def anon_memory_hint(place: str) -> str:
    return (
        "这件事我今晚记得，明天就忘啦。想让我一直记得，"
        f"去图书馆里登记一下吧{place}。"
    )


GUIDE_TEXTS = (
    private_guide(""),
    GROUP_GUIDE,
    ANON_QUOTA_USER,
    ANON_QUOTA_GLOBAL,
    ANON_GROUP_ASK,
    anon_first_hint(""),
    anon_memory_hint(""),
)
```

`private_guide` 仍然只负责把 `place` 拼进 `_PRIVATE_GUIDE`，不要改它的签名。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_commands.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/channels/qq/commands.py backend/tests/test_qq_commands.py
git commit -m "feat(qq): 梅林人设的绑定引导与匿名文案"
```

---

### Task 2: 匿名额度存储与运行时设置

**Files:**
- Create: `backend/app/anonymous_quota.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/runtime_settings.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_anonymous_quota.py`
- Test: `backend/tests/test_runtime_settings.py`（不存在则新建）

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class AnonymousReservation:
      scope: str
      day: str
      limit: int
      remaining: int
      global_limit: int
      global_remaining: int | None

  @dataclass(frozen=True, slots=True)
  class AnonymousQuotaResult:
      status: str  # "ok" | "user_exhausted" | "global_exhausted"
      free: AnonymousReservation | None

  class AnonymousQuotaStore:
      def __init__(self, db_path: str, timezone: str = "Asia/Shanghai",
                   now: Callable[[], datetime] | None = None) -> None: ...
      def today(self) -> str: ...
      def seconds_until_reset(self) -> int: ...
      def reserve(self, scope: str, limit: int, *, global_limit: int = 0,
                  day: str | None = None) -> AnonymousQuotaResult: ...
      def refund(self, free: AnonymousReservation) -> None: ...
      def used(self, scope: str, *, day: str | None = None) -> int: ...

  def get_anonymous_quota_store() -> AnonymousQuotaStore: ...
  ```
- `RuntimeSettings` 新增六个字段（`bool`/`int`）：
  `anonymous_web_enabled=False`、`anonymous_qq_enabled=False`、
  `anonymous_guide_enabled=True`、`anonymous_web_daily_quota=20`、
  `anonymous_qq_daily_quota=20`、`anonymous_global_daily_quota=300`。
- 范围：两个按人上限 `(0, 1000)`，全站上限 `(0, 1_000_000)`。
  个人 `0` = 关闭该通道；全站 `0` = 不限全站。
- `RuntimeSettingsUpdate` 增加同名字段，`extra="forbid"` 保持不变。

- [ ] **Step 1: 写失败的额度测试**

新建 `backend/tests/test_anonymous_quota.py`：

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from app.anonymous_quota import AnonymousQuotaStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def make_store(tmp_path):
    return AnonymousQuotaStore(
        str(tmp_path / "quota.sqlite3"),
        "Asia/Shanghai",
        now=lambda: NOW,
    )


def test_personal_limit_and_refund(tmp_path):
    store = make_store(tmp_path)
    first = store.reserve("web:abc", 2, global_limit=10)
    second = store.reserve("web:abc", 2, global_limit=10)
    assert first.status == "ok"
    assert first.free.remaining == 1
    assert second.status == "ok"
    assert second.free.remaining == 0
    assert store.reserve("web:abc", 2, global_limit=10).status == "user_exhausted"

    store.refund(second.free)
    assert store.reserve("web:abc", 2, global_limit=10).status == "ok"


def test_global_limit_is_shared_across_scopes(tmp_path):
    store = make_store(tmp_path)
    assert store.reserve("web:a", 10, global_limit=2).status == "ok"
    assert store.reserve("web:b", 10, global_limit=2).status == "ok"
    result = store.reserve("qq:12345", 10, global_limit=2)
    assert result.status == "global_exhausted"
    assert result.free is None


def test_zero_personal_limit_closes_channel(tmp_path):
    store = make_store(tmp_path)
    assert store.reserve("web:a", 0, global_limit=10).status == "user_exhausted"


def test_zero_global_limit_means_unlimited(tmp_path):
    store = make_store(tmp_path)
    for _ in range(50):
        assert store.reserve("web:a", 1000, global_limit=0).status == "ok"


def test_scopes_are_isolated(tmp_path):
    store = make_store(tmp_path)
    assert store.reserve("web:a", 1, global_limit=10).status == "ok"
    assert store.reserve("web:b", 1, global_limit=10).status == "ok"
    assert store.used("web:a") == 1
    assert store.used("web:b") == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_anonymous_quota.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.anonymous_quota'`

- [ ] **Step 3: 实现匿名额度**

`anonymous_quota.py` 用两张表，事务语义与 `app/qq_quota.py` 相同：
`BEGIN IMMEDIATE` → 按 scope 占位 → 最后占全站 → 失败整体 rollback。

```sql
CREATE TABLE IF NOT EXISTS anonymous_usage (
    scope TEXT NOT NULL,
    day   TEXT NOT NULL,
    used  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, day)
);
CREATE TABLE IF NOT EXISTS anonymous_global_usage (
    day  TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
);
```

`reserve` 规则：
- `limit <= 0` → `user_exhausted`；
- `global_limit > 0` 时占全站，满了 → `global_exhausted`；
- `global_limit == 0` → 不占全站；
- `free.remaining = limit - used`，`global_remaining = global_limit - global_used`
  或 `None`。

`refund` 只退 `free` 里真实占过的桶（本任务两个桶都算过）。

- [ ] **Step 4: 加运行时设置与接口模型**

`config.py` 在 QQ 额度附近加六个字段，默认值同上。
`runtime_settings.py`：
- `RuntimeSettings` 加六个字段；
- `_defaults_from_settings` 从 `Settings` 读取；
- `_INT_RANGES` 加三项（`anonymous_web_daily_quota`、`anonymous_qq_daily_quota`、
  `anonymous_global_daily_quota`）；
- `_BOOL_FIELDS` 加三项（`anonymous_web_enabled`、`anonymous_qq_enabled`、
  `anonymous_guide_enabled`）。
`schemas.py` 的 `RuntimeSettingsUpdate` 加六个可选字段，范围与上面一致。

- [ ] **Step 5: 写运行时设置测试**

新建 `backend/tests/test_runtime_settings.py`（若已存在则追加）：

```python
def test_anonymous_defaults_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    from app.config import get_settings
    from app.runtime_settings import RuntimeSettingsStore

    get_settings.cache_clear()
    store = RuntimeSettingsStore(
        str(tmp_path / "runtime.json"),
        get_settings(),
    )
    assert store.load().anonymous_web_enabled is False
    assert store.load().anonymous_web_daily_quota == 20
    updated = store.update(
        {"anonymous_web_enabled": True, "anonymous_web_daily_quota": 5}
    )
    assert updated.anonymous_web_enabled is True
    assert updated.anonymous_web_daily_quota == 5


def test_anonymous_quota_range_is_validated(tmp_path):
    import pytest
    from app.config import get_settings
    from app.runtime_settings import RuntimeSettingsStore

    store = RuntimeSettingsStore(str(tmp_path / "r.json"), get_settings())
    with pytest.raises(ValueError):
        store.update({"anonymous_web_daily_quota": 1001})
```

- [ ] **Step 6: 运行两个测试文件**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_anonymous_quota.py tests/test_runtime_settings.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/anonymous_quota.py backend/app/config.py \
  backend/app/runtime_settings.py backend/app/schemas.py \
  backend/tests/test_anonymous_quota.py backend/tests/test_runtime_settings.py
git commit -m "feat(anonymous): 匿名额度存储与运行时开关"
```

---

### Task 2b: 后台可编辑的引导文案

**Files:**
- Create: `backend/app/channels/qq/copy.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/runtime_settings.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_qq_copy.py`

**Interfaces:**
- Consumes: `commands` 的默认常量（Task 1）、`RuntimeSettings`（Task 2）。
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class QQCopy:
      private_guide: str
      group_guide: str
      anon_first_hint: str
      anon_memory_hint: str
      anon_quota_user: str
      anon_quota_global: str
      anon_group_ask: str

      @classmethod
      def from_runtime(cls, runtime: RuntimeSettings, *, place: str) -> "QQCopy": ...
  ```
- `RuntimeSettings` 再加七个 `str = ""` 字段：
  `copy_private_guide`、`copy_group_guide`、`copy_anon_first_hint`、
  `copy_anon_memory_hint`、`copy_anon_quota_user`、`copy_anon_quota_global`、
  `copy_anon_group_ask`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_qq_copy.py`：

```python
from dataclasses import replace

from app.channels.qq import commands
from app.channels.qq.copy import QQCopy
from app.runtime_settings import RuntimeSettings


def _runtime(**overrides) -> RuntimeSettings:
    base = RuntimeSettings()  # 全默认
    return replace(base, **overrides)


def test_empty_override_falls_back_to_code_default():
    copy = QQCopy.from_runtime(_runtime(), place="/blog")
    assert copy.private_guide == commands.private_guide("/blog")
    assert copy.group_guide == commands.GROUP_GUIDE
    assert copy.anon_quota_user == commands.ANON_QUOTA_USER


def test_override_wins_and_place_is_substituted():
    copy = QQCopy.from_runtime(
        _runtime(copy_private_guide="  来吧，登记处在 {place}  "),
        place="/library",
    )
    assert copy.private_guide == "来吧，登记处在 /library"


def test_stray_braces_survive():
    copy = QQCopy.from_runtime(
        _runtime(copy_anon_quota_user="明天再来 {>_<}"),
        place="",
    )
    assert copy.anon_quota_user == "明天再来 {>_<}"


def test_overlong_override_is_rejected():
    import pytest

    from app.runtime_settings import RuntimeSettingsStore

    store = RuntimeSettingsStore(":memory:", RuntimeSettings())
    with pytest.raises(ValueError):
        store.update({"copy_group_guide": "字" * 501})
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_copy.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.channels.qq.copy'`

- [ ] **Step 3: 加字段与校验**

- `config.py`：七个字段，默认 `""`。
- `runtime_settings.py`：`RuntimeSettings` 七个字段；
  `_defaults_from_settings` 传入；新增
  `_STRING_MAX = {"copy_private_guide": 500, ...}`（七个全覆盖）；
  `_validate` 里对字符串字段检查 `isinstance(str)` 且 `len <= 500`，
  超长抛 `ValueError`。
- `schemas.py`：`RuntimeSettingsUpdate` 七个可选
  `str | None = Field(default=None, max_length=500)`。

- [ ] **Step 4: 实现 `QQCopy`**

`copy.py` 的 `from_runtime`：逐字段 `(override or "").strip()`，
为空则取 `commands` 默认值，最后统一 `value.replace("{place}", place)`。
默认值也走一次替换，这样后台改不改行为一致。

- [ ] **Step 5: 运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_copy.py tests/test_runtime_settings.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/channels/qq/copy.py backend/app/config.py \
  backend/app/runtime_settings.py backend/app/schemas.py \
  backend/tests/test_qq_copy.py
git commit -m "feat(qq): 后台可编辑的引导文案"
```

---

### Task 3: 身份别名、记忆合并与彻底删除

**Files:**
- Create: `backend/app/memory/merge.py`
- Modify: `backend/app/memory/db.py`
- Modify: `backend/app/memory/identity.py`
- Modify: `backend/app/memory/pipeline.py`
- Test: `backend/tests/test_memory_merge.py`
- Test: `backend/tests/test_memory_delete.py`（追加断言）

**Interfaces:**
- Produces:
  ```python
  def canonical_key(conn: sqlite3.Connection, person_key: str) -> str: ...
  def merge_person(
      conn: sqlite3.Connection,
      *,
      source_keys: list[str],
      target_key: str,
      now: str | None = None,
  ) -> dict[str, int]: ...
  def delete_person_data(
      conn: sqlite3.Connection,
      person_key: str,
  ) -> dict[str, int]: ...
  ```
- `identity.linked_keys()` 在原有 wp/qq 关联之外，纳入 `person_aliases`
  指向同一 canonical 的所有 key；自身仍排第一。
- `pipeline.delete_user_memory(user_id)` 改为调用 `delete_person_data`，
  返回值保留 `memories` / `events` 两个键，并新增各表计数。
- `qq_viewer()` 在 `link_identity` 之后调用
  `merge_person(conn, source_keys=[qq_key], target_key=wp_key)`。

- [ ] **Step 1: 写失败的合并测试**

新建 `backend/tests/test_memory_merge.py`，至少覆盖：

```python
def test_merge_moves_every_person_scoped_row(conn):
    # 种下 qq:1 的事件、记忆、关系状态、关系事件、决策、评估、待续话题、主动日志
    ...
    result = merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")
    assert result["events"] == 1
    assert result["memories"] == 1
    assert merge.canonical_key(conn, "qq:1") == "wp:2"
    for table in ("events", "memories", "relationship_events",
                  "decision_log", "appraisal_log", "pending_topics"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE person_key='qq:1'"
        ).fetchone()[0] == 0
    audience = conn.execute(
        "SELECT audience, audience_key FROM events LIMIT 1"
    ).fetchone()
    assert audience["audience"] == '["wp:2"]'
    assert audience["audience_key"] == "wp:2"


def test_merge_keeps_latest_relationship_state_per_persona(conn):
    # 同一 persona_key 下 wp:2 较新、qq:1 较旧
    ...
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")
    rows = conn.execute(
        "SELECT * FROM relationship_state WHERE person_key='wp:2'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["affection"] == 70.0  # wp 较新
    assert rows[0]["last_proactive_at"] == "2026-11-09T00:00:00+00:00"


def test_merge_is_idempotent(conn):
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")
    second = merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")
    assert second["events"] == 0


def test_delete_person_data_clears_aliases_and_all_tables(conn):
    merge.merge_person(conn, source_keys=["qq:1"], target_key="wp:2")
    merge.delete_person_data(conn, "wp:2")
    assert conn.execute("SELECT COUNT(*) FROM person_aliases").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM relationship_state"
    ).fetchone()[0] == 0
```

`conn` fixture 沿用 `test_qq_group_policy.py` 的写法：
`SimpleNamespace(memory_db_path=..., embedding_dim=4)` + `monkeypatch.setattr(db, "get_settings", ...)`
+ `db.init_schema()`。

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_memory_merge.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.memory.merge'`

- [ ] **Step 3: 加表与实现合并**

`db.py` 的 `_SCHEMA` 加：

```sql
CREATE TABLE IF NOT EXISTS person_aliases (
    alias_key     TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_person_aliases_canonical
    ON person_aliases(canonical_key);
```

`merge.py`：
- `_PERSON_TABLES = ("events", "memories", "relationship_events",
  "decision_log", "appraisal_log", "pending_topics")`
- `_ROLE_RANK = ("normal", "special", "exception", "owner")`
- `canonical_key` 用循环跟随别名，最多 16 跳，检测环后返回当前值。
- `merge_person`：
  1. 解析 `target = canonical_key(target_key)`，把每个 source 也解析成 canonical 并去重；
  2. 对每个 source：`INSERT OR REPLACE INTO person_aliases`，
     并把 `canonical_key = source` 的旧别名改指 target；
  3. 对 `events` / `memories`：先按 source 查出 `id, scope, audience`，
     把 JSON 里的 source key 换成 target，重算 `audience_key(scope, audience)`，
     再 `UPDATE person_key/audience/audience_key`；
  4. 其余 `_PERSON_TABLES` 直接 `UPDATE ... SET person_key=? WHERE person_key IN (...)`；
  5. `proactive_log.target_key` 同样更新；
  6. `appraisal_usage`：把 `person:<source>` 的 used 按天累加到
     `person:<target>`，再删 source 行；
  7. `relationship_state`：按 `(persona_key)` 分组，每个 persona 只留一行。
     目标行存在时，较新的一行覆盖数值字段，`last_proactive_at` 取两者较大值；
     目标行不存在时，把最新 source 行的 `person_key` 改成 target；
  8. `persons`：目标行 role 取两者更高者，`first_seen_at` 取较早，
     `last_seen_at` 取较晚，`display_name/note` 优先保留非空目标值；
     合并 `qq_uin` / `wp_user_id` 后删除 source 行。
- 返回每张表受影响的行数；已合并时返回全 0。
- `delete_person_data`：`keys = linked_keys(conn, person_key)` 再补上所有
  `person_aliases` 里 canonical 属于这组 key 的 alias；删 memories 前先
  `MEMORIES_VEC.delete_ids`；然后按同一组 key 删除所有表、`appraisal_usage`
  的 `person:<key>`、`person_aliases`、`persons`。

- [ ] **Step 4: 接上绑定与删除**

`identity.linked_keys` 在现有结果后追加别名 key（按 canonical 相等），
保持原 `person_key` 在第一位。

`pipeline.qq_viewer` 已绑定时：

```python
wp_key = identity.person_key_web(bound_user_id)
qq_key = identity.person_key_qq(qq_uin)
identity.link_identity(conn, [wp_key, qq_key],
                       wp_user_id=bound_user_id, qq_uin=str(qq_uin))
merge.merge_person(conn, source_keys=[qq_key], target_key=wp_key)
keys = identity.linked_keys(conn, person_key)
```

`pipeline.delete_user_memory` 改为：

```python
conn = _connect()
try:
    return merge.delete_person_data(conn, identity.person_key_web(user_id))
finally:
    conn.close()
```

保留 `test_memory_delete.py` 现有断言；新增一条断言：删号后
`relationship_state` / `relationship_events` / `decision_log` 也为 0。

- [ ] **Step 5: 运行合并/删除/身份测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_memory_merge.py tests/test_memory_delete.py tests/test_memory_db.py tests/test_memory_pipeline.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/memory/merge.py backend/app/memory/db.py \
  backend/app/memory/identity.py backend/app/memory/pipeline.py \
  backend/tests/test_memory_merge.py backend/tests/test_memory_delete.py
git commit -m "feat(memory): 身份别名、记忆合并与彻底删除"
```

---

### Task 4: 关系角色后台接口

**Files:**
- Create: `backend/app/api/admin_roles.py`
- Modify: `backend/app/api/__init__.py`（或 `app/main.py` 里现有 router 注册处）
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_admin_roles.py`

**Interfaces:**
- Consumes: `identity.touch_person`、`role_for`、`role_config`、
  `get_persona_store().load_active().dynamics`。
- Produces:
  - `GET /api/admin/roles?query=` →
    `{"roles": [{"wp_user_id","person_key","display_name","role","cap"}],
      "role_caps": {"owner":100,"exception":85,"special":75,"normal":60}}`
  - `PUT /api/admin/roles` body `{"wp_user_id": int, "role": RoleName}` → 单条
  - `DELETE /api/admin/roles/{wp_user_id}` → 重置为 `normal`
- `RoleName = Literal["owner", "exception", "special", "normal"]`
- `RoleUpdate` 加进 `schemas.py`，`extra="forbid"`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_admin_roles.py`，复用 `test_memory_delete.py` 的
JWT fixture 写法：

```python
def test_admin_role_list_shows_caps_and_auto_owns_admin(client):
    response = client.get("/api/admin/roles", headers=_headers(99, admin=True))
    assert response.status_code == 200
    data = response.json()
    assert data["role_caps"] == {
        "owner": 100, "exception": 85, "special": 75, "normal": 60
    }
    me = next(item for item in data["roles"] if item["wp_user_id"] == 99)
    assert me["role"] == "owner"


def test_set_and_reset_role(client):
    response = client.put(
        "/api/admin/roles",
        headers=_headers(99, admin=True),
        json={"wp_user_id": 7, "role": "exception"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "exception"

    conn = memory_db.connect()
    try:
        assert role_for(conn, "wp:7") == "exception"
    finally:
        conn.close()

    response = client.delete("/api/admin/roles/7", headers=_headers(99, admin=True))
    assert response.status_code == 204
    conn = memory_db.connect()
    try:
        assert role_for(conn, "wp:7") == "normal"
    finally:
        conn.close()


def test_non_admin_is_rejected(client):
    assert client.get(
        "/api/admin/roles", headers=_headers(7)
    ).status_code == 403
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_admin_roles.py -q`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 实现接口**

`admin_roles.py`：
- `_read(query)` 打开记忆库，`init_schema`，查
  `SELECT person_key, display_name, wp_user_id, role FROM persons
   WHERE wp_user_id IS NOT NULL`，可选 `display_name LIKE ?` 或用
  `CAST(wp_user_id AS TEXT) = ?` 搜索；按 `role` 高低、`last_seen_at` 倒序。
- 当前管理员调用 GET 时，把自己的 `wp:<id>` 插入/更新为 `owner`
  （`identity.touch_person` + `UPDATE persons SET role='owner'`）。
- `cap` 从 active persona 的 `dynamics.relationship.roles[role].cap` 读；
  `role_config` 回退 normal。
- PUT：`wp_user_id` 不存在时先 `touch_person`，再 `UPDATE persons SET role=?`。
- DELETE：`UPDATE persons SET role='normal' WHERE person_key=?`；不存在也返回 204。
- 所有数据库操作放进 `asyncio.to_thread`，异常返回 503。

- [ ] **Step 4: 运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_admin_roles.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/admin_roles.py backend/app/schemas.py \
  backend/tests/test_admin_roles.py
git commit -m "feat(admin): 关系角色名单接口"
```

---

### Task 5: QQ 白名单存储与后台接口

**Files:**
- Create: `backend/app/qq_whitelist.py`
- Create: `backend/app/api/admin_qq_whitelist.py`
- Modify: `backend/app/memory/db.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/main.py`（注册 router）
- Test: `backend/tests/test_admin_qq_whitelist.py`

**Interfaces:**
- Produces:
  ```python
  def is_whitelisted(conn: sqlite3.Connection, qq_uin: str) -> bool: ...
  def is_whitelisted_qq(qq_uin: str) -> bool: ...  # 自己开库，给适配器用
  def entry_for(conn: sqlite3.Connection, qq_uin: str) -> sqlite3.Row | None: ...
  def add_entry(conn, qq_uin, *, note=None, added_by=None,
                now=None) -> None: ...
  def remove_entry(conn, qq_uin) -> bool: ...
  def list_entries(conn) -> list[sqlite3.Row]: ...
  ```
- API：
  - `GET /api/admin/qq-whitelist`
  - `POST /api/admin/qq-whitelist` body
    `{"qq_uin": "123456", "role": "special", "note": "老读者"}`
  - `PUT /api/admin/qq-whitelist/{qq_uin}` body `{"role": ..., "note": ...}`
  - `DELETE /api/admin/qq-whitelist/{qq_uin}`（移出名单，数据休眠）
  - `DELETE /api/admin/qq-whitelist/{qq_uin}/data`（彻底删除）
- `QQWhitelistAdd` / `QQWhitelistUpdate` 加进 `schemas.py`；
  `qq_uin` 用 `pattern=r"^\d{5,12}$"`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_admin_qq_whitelist.py`：

```python
def test_add_and_list_unbound_qq(client):
    response = client.post(
        "/api/admin/qq-whitelist",
        headers=_headers(99, admin=True),
        json={"qq_uin": "123456", "role": "special", "note": "老读者"},
    )
    assert response.status_code == 200
    data = client.get(
        "/api/admin/qq-whitelist", headers=_headers(99, admin=True)
    ).json()
    entry = next(item for item in data["entries"] if item["qq_uin"] == "123456")
    assert entry["role"] == "special"
    assert entry["cap"] == 75
    assert entry["bound_user_id"] is None


def test_reject_already_bound_qq(client):
    store = get_credit_store()
    store.set_bind_code(7, BIND_CODE, False)
    store.bind_qq("123456", BIND_CODE)
    response = client.post(
        "/api/admin/qq-whitelist",
        headers=_headers(99, admin=True),
        json={"qq_uin": "123456", "role": "special"},
    )
    assert response.status_code == 409


def test_remove_keeps_data_and_purge_deletes_it(client):
    client.post(
        "/api/admin/qq-whitelist",
        headers=_headers(99, admin=True),
        json={"qq_uin": "123456", "role": "normal"},
    )
    # 手动种一条事件
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO events (occurred_at, person_key, platform, channel_id,
                   scope, audience, audience_key, source, content, created_at)
               VALUES ('2026-11-10T00:00:00+00:00', 'qq:123456', 'qq',
                       'private:123456', 'personal', '["qq:123456"]',
                       'qq:123456', 'user_message', '你好',
                       '2026-11-10T00:00:00+00:00')"""
        )
        conn.commit()
    finally:
        conn.close()

    assert client.delete(
        "/api/admin/qq-whitelist/123456", headers=_headers(99, admin=True)
    ).status_code == 204
    conn = memory_db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE person_key='qq:123456'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    assert client.delete(
        "/api/admin/qq-whitelist/123456/data", headers=_headers(99, admin=True)
    ).status_code == 204
    conn = memory_db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE person_key='qq:123456'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_admin_qq_whitelist.py -q`
Expected: FAIL，404

- [ ] **Step 3: 建表与实现存储**

`db.py` 加：

```sql
CREATE TABLE IF NOT EXISTS qq_whitelist (
    qq_uin   TEXT PRIMARY KEY,
    note     TEXT,
    added_by TEXT,
    added_at TEXT NOT NULL
);
```

`qq_whitelist.py` 的 `add_entry` 只写名单；API 负责
`identity.touch_person(conn, f"qq:{uin}", qq_uin=uin)` 和设置 `persons.role`。
`remove_entry` 只删名单并把 role 重置为 `normal`，**不删数据**。
`is_whitelisted_qq` 自己 `connect()` / `init_schema()` / `close()`，
给适配器在线程池里调用。

- [ ] **Step 4: 实现 API**

- GET：名单每行 join `persons.role`，`cap` 从 active persona 取；
  对每个未绑定 QQ 用 `get_credit_store().binding_of_qq()` 填
  `bound_user_id`（已绑定的正常不应出现在名单里，但旧数据可能）。
- POST：先查 `binding_of_qq`，已绑定 → 409；`add_entry` + `touch_person`
  + 设置 role；返回单条。
- PUT：只允许改名单内 QQ；更新 role/note。
- DELETE `/data`：`merge.delete_person_data(conn, f"qq:{uin}")`，再删名单行。
- 全部用 `asyncio.to_thread`；非管理员 403 由 `require_admin` 负责。

- [ ] **Step 5: 运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_admin_qq_whitelist.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/qq_whitelist.py backend/app/api/admin_qq_whitelist.py \
  backend/app/memory/db.py backend/app/schemas.py backend/app/main.py \
  backend/tests/test_admin_qq_whitelist.py
git commit -m "feat(admin): QQ 白名单与彻底删除接口"
```

---

### Task 6: 群白名单运行时存储与接口

**Files:**
- Create: `backend/app/channels/qq/group_allowlist.py`
- Modify: `backend/app/memory/db.py`
- Modify: `backend/app/api/admin_qq.py`
- Modify: `backend/app/channels/qq/adapter.py`
- Modify: `backend/app/proactive/scheduler.py`
- Test: `backend/tests/test_qq_group_allowlist.py`
- Test: `backend/tests/test_admin_qq_groups.py`（追加）

**Interfaces:**
- Produces:
  ```python
  SEED_META_KEY = "qq_allowed_groups_seeded_v1"
  def ensure_seeded(conn, env_groups: set[int]) -> None: ...
  def allowed_group_ids(conn) -> set[int]: ...
  def add_group(conn, group_id: int) -> None: ...
  def remove_group(conn, group_id: int) -> bool: ...
  ```
- `admin_qq._read_all` 改用运行时名单；新增
  `POST /api/admin/qq-groups` body `{"group_id": int}` 和
  `DELETE /api/admin/qq-groups/{group_id}`。
- `update_group_policy` 的白名单检查改为运行时名单。

- [ ] **Step 1: 写失败的名单测试**

新建 `backend/tests/test_qq_group_allowlist.py`：

```python
def test_seeds_once_from_env(conn):
    ensure_seeded(conn, {111, 222})
    assert allowed_group_ids(conn) == {111, 222}
    remove_group(conn, 222)
    ensure_seeded(conn, {111, 222})  # 第二次不应把 222 加回来
    assert allowed_group_ids(conn) == {111}


def test_empty_env_seeds_empty_list(conn):
    ensure_seeded(conn, set())
    assert allowed_group_ids(conn) == set()


def test_add_and_remove(conn):
    assert add_group(conn, 333) is None
    assert allowed_group_ids(conn) == {333}
    assert remove_group(conn, 333) is True
    assert remove_group(conn, 333) is False
```

`conn` fixture 同 Task 3。

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_group_allowlist.py -q`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 建表与实现**

`db.py` 加：

```sql
CREATE TABLE IF NOT EXISTS qq_allowed_groups (
    group_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);
```

`group_allowlist.py`：
- `ensure_seeded` 读 `meta` 的 `SEED_META_KEY`；没有就插入
  `get_settings().qq_allowed_group_ids` 并写 marker（值为当前时间或 `"1"`）。
- `allowed_group_ids` 先 `ensure_seeded(conn, get_settings().qq_allowed_group_ids)`，
  再 `SELECT group_id`。
- `add_group` 用 `INSERT OR IGNORE`；`remove_group` 返回 `rowcount > 0`。

- [ ] **Step 4: 接口与适配器接线**

`admin_qq.py`：
- `_read_all` 用 `allowed_group_ids(conn)` 代替 `settings.qq_allowed_group_ids`。
- `POST /admin/qq-groups` 校验 `group_id > 0`，`add_group`，返回 `_read_all()`
  里对应的那条（默认策略）。
- `DELETE /admin/qq-groups/{group_id}` 只删名单，不删 `qq_group_policies`。
- `PUT` 检查 `group_id in allowed_group_ids(conn)`。

`adapter.py` 群分支：
```python
allowed = await asyncio.to_thread(self._allowed_group_ids)
if incoming.group_id not in allowed:
    return {"handled": False, "reason": "group_not_allowed"}
```
新增 `_allowed_group_ids()`：开记忆库、`init_schema`、返回
`group_allowlist.allowed_group_ids(conn)`。

`scheduler._group_pass`：`for gid in sorted(allowed_group_ids(conn)):`。

- [ ] **Step 5: 追加接口测试并运行**

在 `backend/tests/test_admin_qq_groups.py` 追加：

```python
def test_add_and_remove_group_from_admin(client):
    response = client.post(
        "/api/admin/qq-groups",
        headers=_headers(99, admin=True),
        json={"group_id": 555},
    )
    assert response.status_code == 200
    groups = client.get(
        "/api/admin/qq-groups", headers=_headers(99, admin=True)
    ).json()["groups"]
    assert any(item["group_id"] == 555 for item in groups)

    assert client.delete(
        "/api/admin/qq-groups/555", headers=_headers(99, admin=True)
    ).status_code == 204
    groups = client.get(
        "/api/admin/qq-groups", headers=_headers(99, admin=True)
    ).json()["groups"]
    assert all(item["group_id"] != 555 for item in groups)


def test_policy_rejects_group_outside_runtime_whitelist(client):
    response = client.put(
        "/api/admin/qq-groups/999",
        headers=_headers(99, admin=True),
        json={"trigger": "mention", "reply_policy": "always", "proactive": False},
    )
    assert response.status_code == 422
```

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_group_allowlist.py tests/test_admin_qq_groups.py tests/test_qq_group_policy_flow.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/channels/qq/group_allowlist.py backend/app/memory/db.py \
  backend/app/api/admin_qq.py backend/app/channels/qq/adapter.py \
  backend/app/proactive/scheduler.py \
  backend/tests/test_qq_group_allowlist.py backend/tests/test_admin_qq_groups.py
git commit -m "feat(qq): 群白名单运行时化与后台增删"
```

---

### Task 7: QQ 适配器的 trusted / anonymous 两种身份

**Files:**
- Modify: `backend/app/channels/qq/adapter.py`
- Modify: `backend/app/memory/db.py`（`anonymous_guide_log`）
- Create: `backend/app/channels/qq/anonymous_guide.py`
- Modify: `backend/app/qq_quota.py`
- Test: `backend/tests/test_qq_whitelist_flow.py`
- Test: `backend/tests/test_qq_anonymous_flow.py`
- Test: `backend/tests/test_qq_quota.py`（追加）

**Interfaces:**
- `QQAdapter.__init__` 新增可选注入：
  `anonymous_quota: AnonymousQuotaStore | None = None`、
  `runtime: Callable[[], RuntimeSettings] | None = None`。
- `_run_chat_turn(..., anonymous: bool = False)`。
- `QQQuotaGate.reserve(..., user_id: int | None)`：`user_id is None` 时
  强制走独立 QQ 额度，忽略 `qq_quota_shared`。
- `anonymous_guide.py`：
  ```python
  GUIDE_INTERVAL_DAYS = 7
  def should_send(conn, scope: str, now: str) -> bool: ...
  def mark_sent(conn, scope: str, now: str) -> None: ...
  ```
- `db.py` 加：
  ```sql
  CREATE TABLE IF NOT EXISTS anonymous_guide_log (
      scope        TEXT PRIMARY KEY,
      last_sent_at TEXT NOT NULL
  );
  ```

- [ ] **Step 1: 写白名单行为失败测试**

新建 `backend/tests/test_qq_whitelist_flow.py`，复用
`test_qq_private_chat.py` 的 `FakeSender` / `make_persona` / `AllowAllGate`：

```python
def test_whitelisted_unbound_private_chat_writes_memory(tmp_path):
    qq_whitelist.add_entry(...); identity.touch_person(..., "qq:12345", qq_uin="12345")
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(private_event(12345, "你好")))
    conn = memory_db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE person_key='qq:12345'"
        ).fetchone()[0] >= 2
        assert conn.execute(
            "SELECT COUNT(*) FROM relationship_state WHERE person_key='qq:12345'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_whitelist_wins_over_anonymous(tmp_path):
    qq_whitelist.add_entry(...)
    runtime.update({"anonymous_qq_enabled": True})
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(private_event(12345, "你好")))
    conn = memory_db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE person_key='qq:12345'"
        ).fetchone()[0] > 0
    finally:
        conn.close()


def test_removed_whitelist_falls_back_to_anonymous(tmp_path):
    qq_whitelist.add_entry(...)
    runtime.update({"anonymous_qq_enabled": True})
    qq_whitelist.remove_entry(...)
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(private_event(12345, "你好")))
    conn = memory_db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE person_key='qq:12345'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
```

- [ ] **Step 2: 写匿名行为失败测试**

新建 `backend/tests/test_qq_anonymous_flow.py`：

```python
def test_anonymous_private_replies_without_memory(tmp_path):
    runtime.update({
        "anonymous_qq_enabled": True,
        "anonymous_qq_daily_quota": 2,
        "anonymous_global_daily_quota": 10,
    })
    adapter = make_adapter(tmp_path)
    result = asyncio.run(adapter.handle_event(private_event(12345, "你好")))
    assert result["handled"] is True
    assert "额度" not in sender.private[0][1]
    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn)["events"] == 0
        assert conn.execute("SELECT COUNT(*) FROM relationship_state").fetchone()[0] == 0
    finally:
        conn.close()


def test_anonymous_quota_exhausted_uses_persona_copy(tmp_path):
    runtime.update({"anonymous_qq_enabled": True, "anonymous_qq_daily_quota": 1})
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(private_event(12345, "一", message_id=1)))
    asyncio.run(adapter.handle_event(private_event(12345, "二", message_id=2)))
    assert sender.private[-1][1] == commands.ANON_QUOTA_USER


def test_anonymous_off_unbound_still_guides(tmp_path):
    runtime.update({"anonymous_qq_enabled": False})
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(private_event(12345, "你好")))
    assert "图书馆" in sender.private[-1][1]
    assert memory_db.stats(memory_db.connect())["events"] == 0


def test_anonymous_group_does_not_write_timeline(tmp_path):
    runtime.update({"anonymous_qq_enabled": True})
    group_allowlist.add_group(...)  # 888
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(group_event(12345, "大家好")))
    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn)["events"] == 0
    finally:
        conn.close()


def test_first_anonymous_private_reply_gets_guide_once(tmp_path):
    runtime.update({"anonymous_qq_enabled": True, "anonymous_guide_enabled": True})
    adapter = make_adapter(tmp_path)
    asyncio.run(adapter.handle_event(private_event(12345, "你好", message_id=1)))
    assert "图书馆" in sender.private[-1][1]
    asyncio.run(adapter.handle_event(private_event(12345, "在吗", message_id=2)))
    assert "图书馆" not in sender.private[-1][1]
```

- [ ] **Step 3: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_whitelist_flow.py tests/test_qq_anonymous_flow.py -q`
Expected: FAIL；白名单 QQ 仍收到引导，匿名 QQ 仍被引导拦住，或 `anonymous_quota` 属性不存在。

- [ ] **Step 4: 实现身份判定**

`adapter.handle_event` 在拿到 `binding` 后：

```python
whitelisted = False
if binding is None and not admin_whitelisted:
    whitelisted = await asyncio.to_thread(
        qq_whitelist.is_whitelisted_qq, str(incoming.user_id)
    )
trusted = binding is not None or admin_whitelisted or whitelisted
runtime = self._runtime()
anonymous = runtime.anonymous_qq_enabled and not trusted
```

顺序：
1. 群白名单检查（Task 6）不变；
2. 群未触发：只有 `trusted` 才 `record_qq_ignored`；
3. 命令：`CommandContext.bound_user_id` 对白名单 QQ 仍为 `None`，
   命令照常可用；
4. 未信任且非匿名 → 私聊发 `copy.private_guide`、群聊发 `copy.group_guide`
   （`copy = QQCopy.from_runtime(runtime, place=self.settings.wp_site_url)`，Task 2b）；
5. 匿名：
   - 私聊 → `_coordinator.submit(..., handler=lambda texts: self._private_round(..., anonymous=True))`；
   - 群聊 → `_run_chat_turn(..., binding=None, is_admin=False,
     is_private=False, anonymous=True)`，不做掷骰子、不记时间线。
6. trusted：原有群/私聊路径；`binding` 可能是 `None`，所有
   `getattr(binding, "user_id", None)` 已兼容。

- [ ] **Step 5: 实现匿名回合**

`_private_round` 加 `anonymous: bool = False`。匿名时跳过
`_load_private_signals` / `_appraise_private` / `_decide_and_record`，
直接 `_run_chat_turn(anonymous=True)`。

`_run_chat_turn`：
- 新增参数 `anonymous: bool = False`；
- 额度：
  ```python
  if anonymous:
      quota = self._anonymous_quota()
      result = quota.reserve(
          f"qq:{incoming.user_id}",
          runtime.anonymous_qq_daily_quota,
          global_limit=runtime.anonymous_global_daily_quota,
      )
      if result.status == "user_exhausted":
          return await self._send(incoming, copy.anon_quota_user,
                                  reason="anonymous_quota")
      if result.status == "global_exhausted":
          return await self._send(incoming, copy.anon_quota_global,
                                  reason="anonymous_quota")
  else:
      status, usage = self._reserve(...)
  ```
- 记忆：匿名时 `memory_context = ""`，不调用 `qq_memory_context`；
- 入库：匿名时不调用 `record_qq_turn`、不写 `_history`；
- 关系：匿名路径不传 `relationship` / `dynamics`，自然不写；
- 退款：匿名用 `quota.refund(result.free)`；非匿名沿用 `_refund(usage)`；
- 回复后如果 `anonymous and is_private and runtime.anonymous_guide_enabled`：
  调用 `anonymous_guide.should_send`，命中则 `mark_sent` 并把
  `copy.anon_first_hint` 追加到回复；
  如果用户原话包含 `("记得", "记住", "记忆", "忘")` 且未命中首次提示，
  改追加 `copy.anon_memory_hint`。

`QQQuotaGate.reserve`：
```python
if runtime.qq_quota_shared and user_id is not None:
    ...
```
`user_id is None` 时直接走独立额度分支。

- [ ] **Step 6: 运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_qq_whitelist_flow.py tests/test_qq_anonymous_flow.py tests/test_qq_quota.py tests/test_qq_private_chat.py tests/test_qq_relationship_flow.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/channels/qq/adapter.py backend/app/channels/qq/anonymous_guide.py \
  backend/app/memory/db.py backend/app/qq_quota.py \
  backend/tests/test_qq_whitelist_flow.py backend/tests/test_qq_anonymous_flow.py \
  backend/tests/test_qq_quota.py
git commit -m "feat(qq): QQ 白名单与匿名聊天模式"
```

---

### Task 8: 主动消息支持白名单 QQ

**Files:**
- Modify: `backend/app/proactive/candidates.py`
- Modify: `backend/app/proactive/scheduler.py`
- Test: `backend/tests/test_proactive_whitelist.py`

**Interfaces:**
- `Candidate.wp_user_id: int | None`。
- 候选 SQL：WP 用户保持原条件；白名单 `qq:` 用户额外要求
  `rs.last_reply_at IS NOT NULL`（至少私聊过一次）。
- 调度器发送前：
  - `wp:` 候选仍需绑定一致；
  - `qq:` 候选要求“当前未绑定”且“仍在白名单”。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_proactive_whitelist.py`：

```python
def test_whitelisted_qq_with_private_chat_is_candidate(tmp_path):
    seed_whitelisted_qq(tmp_path, last_reply_at=NOW)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        result = candidates(conn, persona_key="merlin", dynamics=persona().dynamics,
                            think_threshold=0.0, speak_threshold=0.0, now=NOW)
    finally:
        conn.close()
    assert any(c.person_key == "qq:12345" for c in result)


def test_whitelisted_qq_without_reply_is_not_candidate(tmp_path):
    seed_whitelisted_qq(tmp_path, last_reply_at=None)
    ...
    assert all(c.person_key != "qq:12345" for c in result)


def test_scheduler_skips_removed_whitelist_before_send(tmp_path):
    # 生成候选后删名单，再 run_once；sender.private 为空
    ...


def test_scheduler_sends_to_whitelisted_unbound(tmp_path):
    # last_reply_at 非空，名单仍在；sender.private 有一条
    ...
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_whitelist.py -q`
Expected: FAIL，白名单 QQ 不出现在候选里。

- [ ] **Step 3: 改候选查询**

`candidates.py`：

```sql
SELECT rs.*, p.wp_user_id, p.qq_uin
  FROM relationship_state rs
  JOIN persons p ON p.person_key = rs.person_key
 WHERE rs.persona_key = ? AND rs.updated_at >= ?
   AND (
     (p.wp_user_id IS NOT NULL AND rs.person_key LIKE 'wp:%')
     OR (
       rs.person_key LIKE 'qq:%'
       AND rs.last_reply_at IS NOT NULL
       AND p.qq_uin IN (SELECT qq_uin FROM qq_whitelist)
     )
   )
 ORDER BY rs.updated_at DESC
 LIMIT ?
```

循环里：
- `qq_uin` 为空 → 跳过；
- `person_key` 既不是 `wp:` 也不是白名单 `qq:` → 跳过；
- `Candidate.wp_user_id = int(row["wp_user_id"]) if row["wp_user_id"] is not None else None`。

- [ ] **Step 4: 改发送前检查**

`scheduler._private_pass`：
```python
binding = self.credits.binding_of_qq(cand.qq_uin)
if cand.person_key.startswith("wp:"):
    if binding is None or binding.user_id != cand.wp_user_id:
        skip("binding_changed")
        continue
else:
    if binding is not None:
        skip("binding_changed")
        continue
    if not qq_whitelist.is_whitelisted(conn, cand.qq_uin):
        skip("not_whitelisted")
        continue
```

- [ ] **Step 5: 运行测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_proactive_whitelist.py tests/test_proactive_scheduler.py tests/test_proactive_candidates.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/proactive/candidates.py backend/app/proactive/scheduler.py \
  backend/tests/test_proactive_whitelist.py
git commit -m "feat(proactive): 白名单 QQ 的主动消息"
```

---

### Task 9: 匿名网页接口与前端访客路径

**Files:**
- Modify: `backend/app/api/chat.py`
- Modify: `frontend/src/api/chat.ts`
- Modify: `frontend/src/ui/assistant.ts`
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`
- Test: `backend/tests/test_chat_anonymous.py`

**Interfaces:**
- Produces:
  - `POST /api/chat/anonymous`，无需 JWT；
  - 请求体与 `/api/chat` 相同；
  - 响应头 `X-Anonymous-Remaining`（可选）；
  - 403：匿名网页未开启；
  - 429：`ANON_QUOTA_USER` / `ANON_QUOTA_GLOBAL`。
- `client_ip(request: Request) -> str`
- `anonymous_scope(ip: str) -> str`
- `ChatOptions.anonymous?: boolean`；`streamChat` 根据它选
  `/chat/anonymous` 或 `/chat`。
- WP 选项 `l2d_ai_anonymous_web`，保存设置时同步；`auth_config()` 的
  `required` 在该选项为 `1` 时为 `false`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_chat_anonymous.py`：

```python
def test_anonymous_chat_disabled_returns_403(client):
    response = client.post(
        "/api/chat/anonymous",
        json={"messages": [{"role": "user", "content": "你好"}]},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert response.status_code == 403


def test_anonymous_chat_streams_without_memory(client, enable_anonymous):
    response = client.post(
        "/api/chat/anonymous",
        json={"messages": [{"role": "user", "content": "你好"}]},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert response.status_code == 200
    assert "data:" in response.text
    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn)["events"] == 0
        assert memory_db.stats(conn)["memories"] == 0
    finally:
        conn.close()


def test_anonymous_quota_is_per_ip(client, enable_anonymous):
    for _ in range(2):
        assert client.post(
            "/api/chat/anonymous",
            json={"messages": [{"role": "user", "content": "你好"}]},
            headers={"X-Forwarded-For": "1.1.1.1"},
        ).status_code == 200
    response = client.post(
        "/api/chat/anonymous",
        json={"messages": [{"role": "user", "content": "你好"}]},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert response.status_code == 429
    assert "额度" not in response.json()["detail"]


def test_anonymous_quota_global_limit(client, enable_anonymous):
    # 每 IP 10、全站 2
    ...
    assert third.status_code == 429


def test_missing_forwarded_for_uses_client_host(client, enable_anonymous):
    response = client.post(
        "/api/chat/anonymous",
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert response.status_code == 200


def test_logged_in_chat_still_works(client):
    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}]},
        headers=_headers(),
    )
    assert response.status_code == 200
```

`enable_anonymous` fixture 用 `get_runtime_store().update({...})` 打开开关，
并把两个上限设成 2/10。`stream_reply` 在测试里用 mock 模式或已有 fixture；
如果真实模型不可用，沿用现有 `test_chat_api.py` 的 mock 方式。

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_chat_anonymous.py -q`
Expected: FAIL，404

- [ ] **Step 3: 重构聊天端点**

把现有 `chat` 的函数体抽成：

```python
async def _chat_response(
    request: ChatRequest,
    *,
    user: AuthUser | None,
    anonymous: bool,
    scope: str | None,
    request_obj: Request,
) -> EventSourceResponse:
```

- 认证：调用方已经决定 `user`；匿名时 `user=None`。
- 额度：
  - 匿名：`runtime.anonymous_web_enabled` 为假 → 403；
    `scope = anonymous_scope(client_ip(request_obj))`；
    `anonymous_quota.reserve(scope, runtime.anonymous_web_daily_quota,
    global_limit=runtime.anonymous_global_daily_quota)`；
    429 文案取 `QQCopy.from_runtime(runtime, place=settings.wp_site_url)` 的
    `anon_quota_user` / `anon_quota_global`；
  - 登录：保留现有 `QuotaStore` 逻辑。
- SSE 生成器：
  - 匿名：不调用 `record_web_user_event`、`web_memory_context`、
    `record_web_assistant_event`、`schedule_extraction`；
  - `stream_reply(..., is_admin=False, memory_context=None)`；
  - finally 里匿名退款用 `anonymous_quota.refund`，登录用 `store.refund`。
- 响应头：
  - 匿名 `X-Anonymous-Remaining` / `X-Anonymous-Limit`；
  - 登录保持现有 `X-Quota-*`。
- `POST /chat` 调用 `_chat_response(user=user, anonymous=False, ...)`；
  `POST /chat/anonymous` 调用 `_chat_response(user=None, anonymous=True, ...)`。

`client_ip`：
```python
def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"
```
`anonymous_scope` 用 `hashlib.sha256(f"{ip}:{settings.jwt_secret}".encode()).hexdigest()[:32]`，
前缀 `web:`；不记录原始 IP。

- [ ] **Step 4: 前端选路**

`frontend/src/api/chat.ts`：
```ts
export interface ChatOptions {
  token?: string | null;
  anonymous?: boolean;
  signal?: AbortSignal;
}
...
const path = options.anonymous ? '/chat/anonymous' : '/chat';
const response = await fetch(`${apiBase}${path}`, { ... });
```

`frontend/src/ui/assistant.ts` 的 `send()`：
```ts
const anonymous =
  token === null &&
  !this.config.auth.loggedIn &&
  Boolean(this.config.auth.tokenUrl);
...
{ token, anonymous, signal: this.abort.signal }
```

`wp-live2d-ai.php`：
- 加常量 `OPTION_ANONYMOUS_WEB = 'l2d_ai_anonymous_web';`
- `save_backend_settings` 末尾：
  `update_option( self::OPTION_ANONYMOUS_WEB, ! empty( $_POST['anonymous_web_enabled'] ) ? '1' : '0' );`
- `auth_config()`：
  `'required' => (bool) apply_filters( 'l2d_ai_auth_required', '1' !== get_option( self::OPTION_ANONYMOUS_WEB, '0' ) ),`

- [ ] **Step 5: 运行后端测试与前端类型检查**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_chat_anonymous.py tests/test_chat_api.py -q`
Expected: PASS
Run: `cd frontend && pnpm build`
Expected: `tsc --noEmit` 与 vite build 通过（不要求启动页面）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/chat.py backend/tests/test_chat_anonymous.py \
  frontend/src/api/chat.ts frontend/src/ui/assistant.ts \
  wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php
git commit -m "feat(anonymous): 匿名网页聊天与前端访客路径"
```

---

### Task 10: WordPress 后台页面与匿名设置区

**Files:**
- Create: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-roles.php`
- Create: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-qq-whitelist.php`
- Create: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-groups.php`
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`
- Test: 手动检查清单（PHP 无自动化测试）

**Interfaces:**
- 三个页面都挂在 `options-general.php`：
  `l2d-ai-roles` / `l2d-ai-qq-whitelist` / `l2d-ai-groups`。
- 每个类提供 `init()`，在插件 `init()` 里调用。
- 所有写操作走 `admin_post_*` + `check_admin_referer` +
  `L2D_AI_Plugin::request_backend()`。
- 角色下拉选项：`normal`（60）、`special`（75）、`exception`（85）、
  `owner`（100）；页面显示“上限只影响后续增长，不会扣当前好感”。
- QQ 白名单页两个删除动作：
  - “移出名单”：`DELETE /admin/qq-whitelist/{qq}`；
  - “彻底删除数据”：`DELETE /admin/qq-whitelist/{qq}/data`，带二次确认。

- [ ] **Step 1: 角色名单页**

`class-l2d-ai-roles.php`：
- `add_options_page('关系角色', '关系角色', 'manage_options', 'l2d-ai-roles', ...)`。
- 表格列：WP 用户 ID、昵称、角色、好感上限、操作。
- 搜索框：`?page=l2d-ai-roles&s=关键词`。
- 行内表单 POST 到 `admin-post.php`，action `l2d_ai_save_role`，
  字段 `wp_user_id`、`role`、nonce。
- 保存后 `wp_safe_redirect` 回列表并带 `updated=1`。

- [ ] **Step 2: QQ 白名单页**

`class-l2d-ai-qq-whitelist.php`：
- 添加表单：QQ 号（`^\d{5,12}$`）、角色、备注。
- 列表列：QQ、角色、好感上限、备注、添加时间、绑定状态、操作。
- “移出名单”和“彻底删除数据”两个按钮，后者
  `onclick="return confirm('这会删除这个 QQ 的全部记忆和关系，无法恢复。确定吗？')"`。
- 后端 409（已绑定）要在页面显示成“这个 QQ 已经绑定了账号，请到角色名单里设置”。

- [ ] **Step 3: 群白名单页**

`class-l2d-ai-groups.php`：
- 添加群号表单。
- 每个群一行：群号、`trigger` 下拉（mention/keyword/all）、
  `reply_policy` 下拉（always/silent）、`proactive` 复选框、保存、移除。
- 保存三开关：`PUT /admin/qq-groups/{id}`；
- 移除：`DELETE /admin/qq-groups/{id}`，提示“策略会保留，重新加回时沿用”。

- [ ] **Step 4: 主设置页匿名分区**

在 `render_settings_page()` 的“QQ 主动消息”之后加“匿名模式”：
- 复选框 `anonymous_web_enabled`、`anonymous_qq_enabled`、
  `anonymous_guide_enabled`；
- 数字 `anonymous_web_daily_quota`、`anonymous_qq_daily_quota`、
  `anonymous_global_daily_quota`；
- 说明文字写清“匿名聊天不读记忆、不写记忆、不触发主动消息”、
  “个人 0 = 关闭该通道，全站 0 = 不限”。
- `save_backend_settings` 的 `$rules` 加三个数字项；布尔项显式
  `! empty($_POST[...])`。
- 在插件 `init()` 里 include + `::init()` 三个新类。

**文案编辑区**（同一页，“匿名模式”之后）：

- 七个 `textarea`：`copy_private_guide`、`copy_group_guide`、
  `copy_anon_first_hint`、`copy_anon_memory_hint`、
  `copy_anon_quota_user`、`copy_anon_quota_global`、`copy_anon_group_ask`；
- 每个框的 `placeholder` 显示代码默认值（从 `/admin/settings` 返回的
  `defaults` 里读 `copy_*`），留空 = 使用默认；
- `copy_private_guide` 下方注明“`{place}` 会替换成站点地址”；
- 每个框计入 `maxlength="500"`；
- 保存：`sanitize_textarea_field( wp_unslash( $_POST[...] ) )` 后直接放进
  `$patch`，**不参与** `$rules` 的类型转换；空串也照传，用于“恢复默认”。

- [ ] **Step 5: PHP 语法与手动检查**

Run: `php -l wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`（如果本机有 PHP）
以及三个新文件各跑一次。
Expected: `No syntax errors detected`。

手动清单：
- 管理员能看到三个新页面，非管理员看不到；
- 角色页改 `exception` 后，后端 `GET /admin/roles` 显示 85；
- QQ 白名单添加后，用该 QQ 私聊能收到 AI 回复；
- 群白名单添加后，群内 @ 能触发；
- 匿名网页开关打开后，未登录页面能聊天；关闭后回到登录提示。

- [ ] **Step 6: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php \
  wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-roles.php \
  wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-qq-whitelist.php \
  wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-groups.php
git commit -m "feat(wp): 角色、QQ 白名单、群白名单与匿名设置页"
```

---

### Task 11: 文档、全量测试与自审

**Files:**
- Modify: `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`
- Modify: `docs/architecture.md`
- Modify: `docs/tutorial.md`（如有后台操作章节）
- Test: 全量 `backend/tests/`

- [ ] **Step 1: 更新 spec**

在 spec 的第 5/7/9 节补：
- 四种角色与上限（owner 100 / exception 85 / special 75 / normal 60）；
- QQ 白名单、群白名单、匿名开关和匿名额度；
- 匿名回合不读写记忆；
- 身份合并与 `person_aliases`；
- 主动消息只对“私聊过一次”的白名单 QQ 生效。

- [ ] **Step 2: 更新 architecture**

在 `docs/architecture.md` 加一段“身份与名单”：
- `person_key` 规范；
- `person_aliases` 的 canonical 规则；
- 新表清单与后台接口；
- 匿名路径不写库的边界。

- [ ] **Step 3: 全量测试**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过；把实际数字写进提交说明。

- [ ] **Step 4: 自审**

逐条检查：
- 所有引导文案无“额度”；
- `special` 仍是 75；
- 匿名路径没有 `record_*` / `add_memory` / `relationship_*` 调用；
- 删除路径覆盖 `relationship_state`、`relationship_events`、`decision_log`、
  `appraisal_log`、`pending_topics`、`proactive_log`、`appraisal_usage`、
  `person_aliases`、`persons`；
- 群白名单移除后适配器和调度器都读运行时名单。

- [ ] **Step 5: 提交**

```bash
git add docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md \
  docs/architecture.md docs/tutorial.md
git commit -m "docs: 阶段 5a 后台名单、身份整合与匿名模式"
```

---

## Self-Review

**Spec coverage:**
- 角色名单与四档上限 → Task 4、Task 10。
- QQ 白名单（未绑定可聊天、记忆、主动消息、角色）→ Task 5、Task 7、Task 8。
- 群白名单运行时化 + 三个开关 → Task 6、Task 10。
- 匿名网页/QQ、独立额度、不读写记忆 → Task 2、Task 7、Task 9。
- 绑定文案去“额度”、梅林人设 → Task 1。
- 文案后台可编辑、留空回退默认 → Task 2b、Task 10。
- 身份整合（含未来适配器）与彻底删除 → Task 3。
- 主动消息“聊过一次” → Task 8。

**Type consistency:** `AnonymousQuotaResult.status` 在 Task 2/7/9 一致；
`Candidate.wp_user_id` 在 Task 8 改为 `int | None`，调用处已按
`startswith("wp:")` 分支处理；`QQQuotaGate.reserve` 的 `user_id`
在 Task 7 允许 `None`，Task 9 的登录路径仍传 `int`。

**Review Focus tests:** 五个失败模式分别在 Task 9（无 XFF）、Task 8（发送前
复查白名单）、Task 3（多 persona 合并）、Task 6（移除群保留策略）、
Task 2（0 值语义）里有对应测试。

**Proportion:** 计划只写接口、表结构和测试断言，不粘贴完整实现；
剩余细节留给实现者按现有代码风格决定。
