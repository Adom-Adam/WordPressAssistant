# QQ 绑定与 QQ 独立额度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让博客账号与 QQ 绑定起来，QQ 私聊/群聊按绑定身份回复，并提供可切换的"QQ 与网页共享额度 / QQ 独立额度"两套计量。

**Architecture:** 绑定码明文只存 WordPress user meta，后端 `user_profile` 只存 SHA-256 哈希与 `is_admin` 标记；QQ 侧用命令注册表截获 `/`、`\` 开头的消息（永不进入 agent），其余消息按"命令 → 绑定判定 → 额度 → agent"顺序处理。额度由新增的 `QQQuotaStore`（独立计数表）与现有 `QuotaStore`（网页计数）经 `QQQuotaGate` 组合，模式由运行时设置 `qq_quota_shared` 决定。

**Tech Stack:** Python 3.11 / FastAPI / SQLite（sqlite3）/ pytest；WordPress PHP 插件（无解释器，靠逐行审阅）；现有 OneBot 11 适配层。

**Spec:** `docs/superpowers/specs/2026-11-01-qq-binding-shared-quota-design.md`

## Global Constraints

- 固定文案（引导、绑定结果、额度用尽、异常）一律梅林口吻；操作三要素必须完整：**个人资料页 / 绑定码 / `/绑定 1234-5678`**；不得出现"系统、接口、绑定状态、用户 ID"这类工程词。
- 所有 QQ 命令以 `/`、`\`、`／`、`＼` 起手；命中即由系统处理，**永不传给 agent，也不写入对话历史**。
- 只在收到消息后回复；**不做任何主动推送**；未绑定用户每条消息都回一次引导，不做时间窗节流。
- 绑定码为 8 位数字，展示为 `XXXX-XXXX`；明文只存在 WordPress（meta `l2d_ai_qq_bind_code`），后端只存 SHA-256 十六进制哈希。
- 解绑只能在网页端；已绑定的 QQ 必须先网页解绑，才能重新绑定；QQ 侧不提供解绑命令。
- 默认值：`qq_daily_quota=10`、`qq_member_daily_quota=30`、`qq_global_daily_quota=0`、`qq_quota_shared=false`、`qq_unlimited=false`。
- `qq_global_daily_quota` 在共享与独立两种模式下都生效，使用**独立的 QQ 全站计数**；`qq_unlimited` 只在独立模式生效。
- 管理员（绑定账号 `is_admin`，或 QQ 号在 `QQ_ADMIN_USERS`）不限量，且**不占用任何 QQ 计数**。
- QQ 额度参数全部通过 WordPress 后台写入 `runtime_settings.json`，立即生效，不改 `.env`、不重启。
- 后端时间以 `QUOTA_TIMEZONE`（默认 Asia/Shanghai）跨零点重置。
- 不新增第三方依赖；仓库按公开处理，文档里不出现真实 IP/域名/密钥。
- PHP 在本机没有解释器：PHP 任务的验证方式是把改动逐行读一遍（转义、nonce、能力校验、括号配对），不伪造测试结果。
- 只做本地提交，不推送、不部署。分支：`feat/qq-binding`（从 `main` 切出）。

## Review Focus

1. **绑定码被别人抢绑**：同一 QQ 已绑账号、或目标账号已绑另一个 QQ 时，必须拒绝且不覆盖；网页解绑后旧码立即失效。
2. **额度边界**：`qq_daily_quota=0` 表示不能聊；`qq_global_daily_quota=0` 表示不限；`qq_unlimited` 只跳过按人/按会员；共享模式下 QQ 全站计数与网页全站计数各占一份、失败同时回滚。
3. **命令异常输入**：全角前缀、前后空格、未知命令、群里发私聊专用命令、空参数、带字母的参数——一律不进 agent、不进历史，并且给出确定文案。
4. **无产出退款**：agent 抛异常/超时/只回 error 时额度必须退回；有正文时不退；共享模式要连 QQ 全站计数一起退。
5. **重复消息与打扰**：NapCat 重发同一 `message_id` 只处理一次；未绑定用户每条新消息都回一次引导，且不发消息时零推送。

---

### Task 1: 绑定数据层（绑定码哈希、绑定/解绑查询）

**Files:**
- Modify: `backend/app/credits.py`（`CREDIT_SCHEMA_SQL`、`CreditStore._init_schema`、新增绑定方法）
- Test: `backend/tests/test_qq_bind_store.py`（新建）

**Interfaces:**
- Consumes: `CreditStore.__init__(db_path, timezone, max_credits, now)`；`_connection()`；`_now_iso()`；`_expired()`。
- Produces:
  - `hash_bind_code(code: str) -> str`（模块级）
  - `is_valid_bind_code(raw: str) -> bool`（模块级）
  - `@dataclass(frozen=True, slots=True) class BindResult: status: str; user_id: int | None`，`status ∈ {"ok","invalid_code","qq_taken","account_taken"}`
  - `@dataclass(frozen=True, slots=True) class QQBinding: user_id: int; tier: str; is_admin: bool`
  - `CreditStore.set_bind_code(user_id: int, code_hash: str, is_admin: bool) -> None`
  - `CreditStore.bind_qq(qq_uin: str, code_hash: str) -> BindResult`
  - `CreditStore.unbind_user(user_id: int) -> bool`
  - `CreditStore.binding_of_qq(qq_uin: str) -> QQBinding | None`
  - `CreditStore.qq_uin_of(user_id: int) -> str | None`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_qq_bind_store.py
from app.credits import CreditStore, hash_bind_code, is_valid_bind_code

def make_store(tmp_path):
    return CreditStore(str(tmp_path / "quota.sqlite3"))

def test_hash_normalizes_width_and_separators():
    assert hash_bind_code("1234-5678") == hash_bind_code("１２３４ ５６７８")
    assert hash_bind_code("1234-5678") == hash_bind_code(" 12345678 ")
    assert len(hash_bind_code("1234-5678")) == 64

def test_is_valid_bind_code():
    assert is_valid_bind_code("1234-5678")
    assert is_valid_bind_code("12345678")
    assert not is_valid_bind_code("1234-567")
    assert not is_valid_bind_code("123456789")
    assert not is_valid_bind_code("abcd-efgh")

def test_bind_code_hash_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=True)
    result = store.bind_qq("10001", hash_bind_code("1234-5678"))
    assert (result.status, result.user_id) == ("ok", 7)
    assert store.qq_uin_of(7) == "10001"
    binding = store.binding_of_qq("10001")
    assert binding is not None and binding.user_id == 7 and binding.is_admin is True
    assert binding.tier == "user"

def test_invalid_code_is_rejected(tmp_path):
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    assert store.bind_qq("10001", hash_bind_code("8765-4321")).status == "invalid_code"

def test_qq_already_bound_to_other_account(tmp_path):
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1111-1111"), is_admin=False)
    store.set_bind_code(8, hash_bind_code("2222-2222"), is_admin=False)
    assert store.bind_qq("10001", hash_bind_code("1111-1111")).status == "ok"
    assert store.bind_qq("10001", hash_bind_code("2222-2222")).status == "qq_taken"
    assert store.qq_uin_of(8) is None

def test_account_already_bound_to_other_qq(tmp_path):
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1111-1111"), is_admin=False)
    store.set_bind_code(7, hash_bind_code("3333-3333"), is_admin=False)
    assert store.bind_qq("10001", hash_bind_code("1111-1111")).status == "ok"
    assert store.bind_qq("20002", hash_bind_code("3333-3333")).status == "account_taken"
    assert store.qq_uin_of(7) == "10001"

def test_unbind_clears_qq_and_rotates(tmp_path):
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1111-1111"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1111-1111"))
    assert store.unbind_user(7) is True
    assert store.qq_uin_of(7) is None
    assert store.binding_of_qq("10001") is None
    assert store.unbind_user(7) is False

def test_migration_adds_columns_to_existing_db(tmp_path):
    import sqlite3
    path = tmp_path / "quota.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE user_profile (
            user_id INTEGER PRIMARY KEY,
            tier TEXT NOT NULL DEFAULT 'user',
            tier_expires_at TEXT,
            qq_uin TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO user_profile (user_id, created_at, updated_at)
        VALUES (5, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00');
        """
    )
    conn.commit()
    conn.close()

    store = CreditStore(str(path))
    store.set_bind_code(5, hash_bind_code("1234-5678"), is_admin=False)
    assert store.bind_qq("10001", hash_bind_code("1234-5678")).user_id == 5
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_qq_bind_store.py -v`
Expected: FAIL / ERROR（`hash_bind_code` 等未定义）

- [ ] **Step 3: 实现**

在 `credits.py` 顶部补 `import hashlib, unicodedata`。模块级：

```python
def _normalize_bind_code(raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(raw or ""))
    return "".join(ch for ch in text if ch.isdigit())

def is_valid_bind_code(raw: str) -> bool:
    return len(_normalize_bind_code(raw)) == 8

def hash_bind_code(code: str) -> str:
    return hashlib.sha256(_normalize_bind_code(code).encode("ascii")).hexdigest()
```

`CREDIT_SCHEMA_SQL` 的 `user_profile` 增加两列：`bind_code_hash TEXT`、`is_admin INTEGER NOT NULL DEFAULT 0`；并在 `_init_schema()` 里：

1. `conn.executescript(CREDIT_SCHEMA_SQL)` 之后调用 `self._migrate_profile_columns(conn)`：用 `PRAGMA table_info(user_profile)` 取现有列名，缺 `bind_code_hash` / `is_admin` 时 `ALTER TABLE user_profile ADD COLUMN ...`。
2. `CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile_qq_uin ON user_profile(qq_uin) WHERE qq_uin IS NOT NULL`，用 `try/except sqlite3.OperationalError` 包住，失败只 `logger.warning`（唯一性还有 `bind_qq` 的事务内检查兜底）。

`set_bind_code`：`_ensure_profile_row()` 后 `UPDATE user_profile SET bind_code_hash=?, is_admin=?, updated_at=? WHERE user_id=?`。

`bind_qq`：单个 `BEGIN IMMEDIATE` 事务内按序判断（顺序即规格 §4.3）：
1. `SELECT user_id FROM user_profile WHERE bind_code_hash=?` 无行 → `invalid_code`
2. `SELECT user_id FROM user_profile WHERE qq_uin=?` 有行 → `qq_taken`（包括同一个账号，等同于"早就记住你啦"）
3. 目标行 `qq_uin` 非空且不等于传入 QQ → `account_taken`
4. `UPDATE user_profile SET qq_uin=?, updated_at=? WHERE user_id=?` → `ok`

`unbind_user`：`UPDATE user_profile SET qq_uin=NULL, bind_code_hash=NULL, updated_at=? WHERE user_id=?`，返回 `rowcount > 0`。

`binding_of_qq`：查 `user_id, tier, tier_expires_at, is_admin`；`tier=="member"` 且到期 → 返回 `tier="user"`（复用 `_expired()`）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_qq_bind_store.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/credits.py backend/tests/test_qq_bind_store.py
git commit -m "feat(credits): QQ 绑定码哈希与绑定/解绑存储"
```

---

### Task 2: 独立 QQ 额度存储

**Files:**
- Create: `backend/app/qq_quota.py`
- Test: `backend/tests/test_qq_quota.py`（新建）

**Interfaces:**
- Consumes: `app.config.get_settings()`（`quota_db_path`、`quota_timezone`）。
- Produces:
  - `@dataclass(frozen=True, slots=True) class QQReservation: qq_uin: str; day: str; counted: bool; limit: int | None; remaining: int | None; global_counted: bool; global_limit: int; global_remaining: int | None`
  - `@dataclass(frozen=True, slots=True) class QQQuotaResult: status: str; free: QQReservation | None`，`status ∈ {"ok","user_exhausted","global_exhausted"}`
  - `class QQQuotaStore: __init__(db_path: str, timezone: str = "Asia/Shanghai", now: Callable[[], datetime] | None = None); today() -> str; reserve(qq_uin: str, limit: int | None, *, global_limit: int = 0) -> QQQuotaResult; refund(free: QQReservation) -> None`
  - `get_qq_quota_store() -> QQQuotaStore`（`lru_cache`）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_qq_quota.py
from datetime import datetime
from zoneinfo import ZoneInfo

from app.qq_quota import QQQuotaStore

def make_store(tmp_path, now=None):
    return QQQuotaStore(str(tmp_path / "quota.sqlite3"), now=now)

def test_reserve_counts_per_user(tmp_path):
    store = make_store(tmp_path)
    first = store.reserve("10001", 2)
    second = store.reserve("10001", 2)
    third = store.reserve("10001", 2)
    assert first.status == "ok" and first.free.counted is True
    assert first.free.remaining == 1
    assert second.status == "ok" and second.free.remaining == 0
    assert third.status == "user_exhausted" and third.free is None

def test_zero_limit_blocks_chat(tmp_path):
    store = make_store(tmp_path)
    assert store.reserve("10001", 0).status == "user_exhausted"

def test_limit_none_skips_per_user(tmp_path):
    store = make_store(tmp_path)
    for _ in range(5):
        assert store.reserve("10001", None).status == "ok"

def test_global_limit_blocks_everyone(tmp_path):
    store = make_store(tmp_path)
    assert store.reserve("10001", None, global_limit=2).status == "ok"
    assert store.reserve("20002", None, global_limit=2).status == "ok"
    blocked = store.reserve("30003", None, global_limit=2)
    assert blocked.status == "global_exhausted"
    assert blocked.free is None

def test_global_zero_means_unlimited(tmp_path):
    store = make_store(tmp_path)
    for _ in range(50):
        assert store.reserve("10001", 100, global_limit=0).status == "ok"

def test_global_failure_rolls_back_per_user(tmp_path):
    store = make_store(tmp_path)
    assert store.reserve("10001", 5, global_limit=1).status == "ok"
    assert store.reserve("10001", 5, global_limit=1).status == "global_exhausted"
    # 回滚后按人计数没有多占：剩余应为 4
    again = store.reserve("10001", 5, global_limit=0)
    assert again.free.remaining == 3

def test_refund_returns_both_buckets(tmp_path):
    store = make_store(tmp_path)
    reserved = store.reserve("10001", 2, global_limit=2)
    store.refund(reserved.free)
    assert store.reserve("10001", 2, global_limit=2).status == "ok"
    assert store.reserve("20002", 2, global_limit=2).status == "ok"

def test_resets_next_day(tmp_path):
    clock = {"now": datetime(2026, 11, 1, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))}
    store = make_store(tmp_path, now=lambda: clock["now"])
    assert store.reserve("10001", 1).status == "ok"
    assert store.reserve("10001", 1).status == "user_exhausted"
    clock["now"] = datetime(2026, 11, 2, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert store.reserve("10001", 1).status == "ok"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_qq_quota.py -v`
Expected: FAIL / ERROR（`No module named 'app.qq_quota'`）

- [ ] **Step 3: 实现**

建表 SQL（同一个 `quota.sqlite3`）：

```sql
CREATE TABLE IF NOT EXISTS qq_daily_usage (
    qq_uin TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (qq_uin, day)
);
CREATE TABLE IF NOT EXISTS qq_global_usage (
    day TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
);
```

写入方式照抄 `QuotaStore.reserve` 的原子风格：`BEGIN IMMEDIATE` → 按人 `INSERT ... ON CONFLICT(qq_uin, day) DO UPDATE SET used = used + 1 WHERE used < ? RETURNING used` → 全站同理 → 任一失败立即 `ROLLBACK` 并返回对应 status（两步都不留副作用）。`limit is None` 跳过按人；`global_limit <= 0` 跳过全站。`refund` 用 `UPDATE ... SET used = MAX(0, used - 1)`，只对 `counted` / `global_counted` 为真的桶执行。

`today()` 与 `_now` 注入方式与 `QuotaStore` 一致（`datetime.now(ZoneInfo(timezone))`）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_qq_quota.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/qq_quota.py backend/tests/test_qq_quota.py
git commit -m "feat(qq): 独立 QQ 额度存储（按人 + QQ 全站）"
```

---

### Task 3: QQ 额度运行参数（布尔字段 + 配置默认值）

**Files:**
- Modify: `backend/app/config.py`（`Settings` 新增 5 项）
- Modify: `backend/app/runtime_settings.py`（字段、`_INT_RANGES`、`_BOOL_FIELDS`、`_validate`、`_defaults_from_settings`）
- Modify: `backend/app/schemas.py`（`RuntimeSettingsUpdate` 新增 5 项）
- Test: `backend/tests/test_runtime_settings.py`（追加）

**Interfaces:**
- Consumes: 现有 `RuntimeSettingsStore.load()/update()`。
- Produces: `RuntimeSettings` 新字段 `qq_daily_quota: int`、`qq_member_daily_quota: int`、`qq_global_daily_quota: int`、`qq_quota_shared: bool`、`qq_unlimited: bool`；`Settings` 同名默认值。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_runtime_settings.py
def test_qq_defaults(tmp_path):
    store = make_store(tmp_path)  # 该文件已有的 helper：make_store(tmp_path, **overrides)
    values = store.load()
    assert values.qq_daily_quota == 10
    assert values.qq_member_daily_quota == 30
    assert values.qq_global_daily_quota == 0
    assert values.qq_quota_shared is False
    assert values.qq_unlimited is False

def test_qq_bool_fields_accept_bool_only(tmp_path):
    store = make_store(tmp_path)
    updated = store.update({"qq_quota_shared": True, "qq_unlimited": True})
    assert updated.qq_quota_shared is True and updated.qq_unlimited is True
    with pytest.raises(ValueError, match="qq_quota_shared"):
        store.update({"qq_quota_shared": 1})

def test_qq_int_ranges(tmp_path):
    store = make_store(tmp_path)
    updated = store.update(
        {"qq_daily_quota": 5, "qq_member_daily_quota": 15, "qq_global_daily_quota": 200}
    )
    assert (updated.qq_daily_quota, updated.qq_member_daily_quota) == (5, 15)
    with pytest.raises(ValueError, match="qq_daily_quota"):
        store.update({"qq_daily_quota": 1001})
```

（`_make_store` 是该测试文件里已有的 helper 名；若实际名不同，按现有写法取用。）

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_runtime_settings.py -v -k qq`
Expected: FAIL（`RuntimeSettings` 没有 `qq_daily_quota`）

- [ ] **Step 3: 实现**

- `config.py`：`qq_daily_quota: int = 10`、`qq_member_daily_quota: int = 30`、`qq_global_daily_quota: int = 0`、`qq_quota_shared: bool = False`、`qq_unlimited: bool = False`。
- `runtime_settings.py`：`RuntimeSettings` 追加同名字段（顺序放在 `credit_max_per_user` 之后）；`_defaults_from_settings` 映射；`_INT_RANGES` 加 `"qq_daily_quota": (0, 1000)`、`"qq_member_daily_quota": (0, 1000)`、`"qq_global_daily_quota": (0, 1_000_000)`；新增 `_BOOL_FIELDS = {"qq_quota_shared", "qq_unlimited"}`，`_validate` 里在 int 分支之前加：

```python
if field in _BOOL_FIELDS:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value
```

- `schemas.py`：`RuntimeSettingsUpdate` 加

```python
qq_daily_quota: int | None = Field(default=None, ge=0, le=1000)
qq_member_daily_quota: int | None = Field(default=None, ge=0, le=1000)
qq_global_daily_quota: int | None = Field(default=None, ge=0, le=1_000_000)
qq_quota_shared: bool | None = None
qq_unlimited: bool | None = None
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_runtime_settings.py tests/test_admin_api.py -v`
Expected: PASS（含新增 3 个）

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/app/runtime_settings.py backend/app/schemas.py backend/tests/test_runtime_settings.py
git commit -m "feat(settings): QQ 额度运行时参数与布尔设置项"
```

---

### Task 4: 额度门面（共享 / 独立模式）

**Files:**
- Modify: `backend/app/qq_quota.py`（追加 `QQUsage`、`QQQuotaGate`）
- Test: `backend/tests/test_qq_quota.py`（追加）

**Interfaces:**
- Consumes: `QuotaStore.reserve(user_id, limit, *, is_admin=False, global_limit=0, allow_credits=True) -> QuotaReservation | None`、`QuotaStore.refund(reservation)`；`QQQuotaStore.reserve(...)`；`RuntimeSettings`（Task 3 字段）。
- Produces:
  - `@dataclass(frozen=True, slots=True) class QQUsage: kind: str; web: QuotaReservation | None; qq: QQReservation | None`，`kind ∈ {"admin","shared","independent"}`
  - `class QQQuotaGate: __init__(quota: QuotaStore, qq: QQQuotaStore, runtime: Callable[[], RuntimeSettings]) -> None; reserve(*, qq_uin: str, user_id: int, tier: str, is_admin: bool) -> tuple[str, QQUsage]; refund(usage: QQUsage) -> None`，status ∈ `{"ok","user_exhausted","global_exhausted"}`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_qq_quota.py
from app.qq_quota import QQQuotaGate
from app.quota import QuotaStore
from app.runtime_settings import RuntimeSettings

def make_runtime(**overrides):
    base = dict(
        daily_quota=10, member_daily_quota=30, global_daily_quota=1000,
        agent_max_model_calls=5, agent_max_tool_calls=5, agent_max_seconds=30.0,
        model="deepseek-chat", credit_max_per_user=100,
        qq_daily_quota=2, qq_member_daily_quota=4, qq_global_daily_quota=0,
        qq_quota_shared=False, qq_unlimited=False,
    )
    base.update(overrides)
    return RuntimeSettings(**base)

def make_gate(tmp_path, runtime):
    quota = QuotaStore(str(tmp_path / "quota.sqlite3"))
    qq = QQQuotaStore(str(tmp_path / "quota.sqlite3"))
    return QQQuotaGate(quota, qq, lambda: runtime), quota, qq

def test_admin_is_not_counted(tmp_path):
    gate, quota, qq = make_gate(tmp_path, make_runtime(qq_global_daily_quota=1))
    status, usage = gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=True)
    assert status == "ok" and usage.kind == "admin"
    assert usage.web is None and usage.qq is None

def test_independent_mode_uses_qq_quota(tmp_path):
    gate, quota, qq = make_gate(tmp_path, make_runtime())
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "user_exhausted"
    # 网页额度完全没动
    assert quota.usage_snapshot([7])[7] == 0

def test_independent_member_uses_member_limit(tmp_path):
    gate, _, _ = make_gate(tmp_path, make_runtime(qq_daily_quota=1, qq_member_daily_quota=3))
    assert gate.reserve(qq_uin="10001", user_id=7, tier="member", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10001", user_id=7, tier="member", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10001", user_id=7, tier="member", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10001", user_id=7, tier="member", is_admin=False)[0] == "user_exhausted"

def test_unlimited_skips_per_user_but_keeps_global(tmp_path):
    gate, _, _ = make_gate(tmp_path, make_runtime(qq_daily_quota=1, qq_unlimited=True, qq_global_daily_quota=2))
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10002", user_id=8, tier="user", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10003", user_id=9, tier="user", is_admin=False)[0] == "global_exhausted"

def test_shared_mode_consumes_web_quota_and_qq_global(tmp_path):
    gate, quota, qq = make_gate(
        tmp_path, make_runtime(qq_quota_shared=True, qq_daily_quota=1, qq_global_daily_quota=0)
    )
    status, usage = gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)
    assert status == "ok" and usage.kind == "shared" and usage.web is not None
    # 按人额度（qq_daily_quota=1）在共享模式下不生效
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"

def test_shared_mode_qq_global_failure_refunds_web(tmp_path):
    gate, quota, qq = make_gate(
        tmp_path, make_runtime(qq_quota_shared=True, global_daily_quota=1000, qq_global_daily_quota=1)
    )
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert quota.usage_snapshot([7])[7] == 1
    assert gate.reserve(qq_uin="20002", user_id=7, tier="user", is_admin=False)[0] == "global_exhausted"
    # 网页计数被回滚到只剩第一次那一条，而不是 2
    assert quota.usage_snapshot([7])[7] == 1

def test_refund_restores_both_modes(tmp_path):
    gate, _, _ = make_gate(tmp_path, make_runtime(qq_daily_quota=1))
    status, usage = gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)
    gate.refund(usage)
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_qq_quota.py -v -k gate`
Expected: FAIL / ERROR（`QQQuotaGate` 未定义）

- [ ] **Step 3: 实现**

```
reserve:
  if is_admin -> ("ok", QQUsage("admin", None, None))
  if runtime.qq_quota_shared:
      limit = member_daily_quota if tier == "member" else daily_quota
      web = quota.reserve(user_id, limit, is_admin=False, global_limit=runtime.global_daily_quota)
      if web is None -> ("user_exhausted", QQUsage("shared", None, None))
      res = qq.reserve(qq_uin, None, global_limit=runtime.qq_global_daily_quota)
      if res.status != "ok":
          quota.refund(web)
          return (res.status, QQUsage("shared", None, None))
      return ("ok", QQUsage("shared", web, res.free))
  limit = None if runtime.qq_unlimited else (
      runtime.qq_member_daily_quota if tier == "member" else runtime.qq_daily_quota
  )
  res = qq.reserve(qq_uin, limit, global_limit=runtime.qq_global_daily_quota)
  return (res.status, QQUsage("independent", None, res.free))
```

`refund`：`if usage.web: quota.refund(usage.web)`；`if usage.qq: qq.refund(usage.qq)`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_qq_quota.py -v`
Expected: PASS（15 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/qq_quota.py backend/tests/test_qq_quota.py
git commit -m "feat(qq): 共享/独立额度门面与退款"
```

---

### Task 5: 命令注册表

**Files:**
- Create: `backend/app/channels/qq/commands.py`
- Test: `backend/tests/test_qq_commands.py`（新建）

**Interfaces:**
- Consumes: `CreditStore.bind_qq`、`hash_bind_code`、`is_valid_bind_code`（Task 1）。
- Produces:
  - `@dataclass(frozen=True, slots=True) class ParsedCommand: name: str; args: str`
  - `@dataclass(frozen=True, slots=True) class CommandContext: qq_uin: int; is_private: bool; group_id: int | None; args: str; bound_user_id: int | None; is_admin: bool; store: CreditStore`
  - `@dataclass(frozen=True, slots=True) class Command: name: str; aliases: tuple[str, ...]; scope: str; help: str; handler: Callable[[CommandContext], str]`
  - `parse_command(text: str) -> ParsedCommand | None`
  - `dispatch(parsed: ParsedCommand, ctx: CommandContext) -> str`
  - `command_help() -> str`
  - 文案常量：`PRIVATE_GUIDE`、`GROUP_GUIDE`、`BIND_OK`、`BIND_ALREADY`、`BIND_INVALID`、`BIND_ACCOUNT_TAKEN`、`BIND_FORMAT`、`PRIVATE_ONLY`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_qq_commands.py
import pytest

from app.channels.qq import commands
from app.channels.qq.commands import CommandContext, dispatch, parse_command
from app.credits import CreditStore, hash_bind_code

@pytest.fixture
def store(tmp_path):
    return CreditStore(str(tmp_path / "quota.sqlite3"))

def make_ctx(store, *, qq_uin=10001, is_private=True, bound_user_id=None, is_admin=False, args=""):
    return CommandContext(
        qq_uin=qq_uin, is_private=is_private, group_id=None if is_private else 999,
        args=args, bound_user_id=bound_user_id, is_admin=is_admin, store=store,
    )

def test_parse_accepts_all_prefixes():
    for text in ("/绑定 1234-5678", "\\绑定 1234-5678", "／绑定 1234-5678", "＼bind 12345678"):
        parsed = parse_command(text)
        assert parsed is not None and parsed.name == "绑定"
    assert parse_command("/BIND 12345678").name == "bind"

def test_parse_rejects_plain_text():
    assert parse_command("今天天气怎么样") is None
    assert parse_command("/") is None

def test_unknown_command_does_not_reach_agent(store):
    reply = dispatch(parse_command("/天气"), make_ctx(store))
    assert "这条咒文我没见过呢" in reply
    assert "绑定" in reply

def test_help_lists_registry(store):
    reply = dispatch(parse_command("/帮助"), make_ctx(store))
    assert "/绑定" in reply and "1234-5678" in reply

def test_bind_success(store):
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    reply = dispatch(parse_command("/绑定 1234-5678"), make_ctx(store))
    assert reply == commands.BIND_OK
    assert store.qq_uin_of(7) == "10001"

def test_bind_invalid_format(store):
    assert dispatch(parse_command("/绑定 12"), make_ctx(store)) == commands.BIND_FORMAT

def test_bind_wrong_code(store):
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    assert dispatch(parse_command("/绑定 8765-4321"), make_ctx(store)) == commands.BIND_INVALID

def test_bind_account_taken(store):
    store.set_bind_code(7, hash_bind_code("3333-3333"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("3333-3333"))
    assert dispatch(parse_command("/绑定 3333-3333"), make_ctx(store, qq_uin=20002)) == commands.BIND_ACCOUNT_TAKEN

def test_already_bound(store):
    reply = dispatch(parse_command("/绑定 1234-5678"), make_ctx(store, bound_user_id=7))
    assert reply == commands.BIND_ALREADY

def test_private_only_command_in_group(store):
    reply = dispatch(parse_command("/绑定 1234-5678"), make_ctx(store, is_private=False))
    assert reply == commands.PRIVATE_ONLY
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_qq_commands.py -v`
Expected: FAIL / ERROR（`No module named 'app.channels.qq.commands'`）

- [ ] **Step 3: 实现**

```python
import re
import unicodedata

COMMAND_PATTERN = re.compile(r"^\s*[\\/]\s*(?P<cmd>[A-Za-z一-龥]+)\s*(?P<args>.*)$")

def parse_command(text: str) -> ParsedCommand | None:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    match = COMMAND_PATTERN.match(normalized)
    if not match:
        return None
    return ParsedCommand(name=match.group("cmd").lower(), args=match.group("args").strip())
```

（NFKC 会把 `／`→`/`、`＼`→`\`、全角字母数字转半角，所以正则只需处理两种半角前缀。）

文案常量（逐字使用，梅林口吻）：

```python
PRIVATE_GUIDE = (
    "诶，我还不太认识你呢。想和我聊天的话，先回博客一趟——登录之后打开「个人资料」，"
    "那里有张写着「Live2D 小助手 · QQ 绑定」的小卡片，里面会有一串绑定码。"
    "拿到以后回来跟我说一句「/绑定 1234-5678」，我就记住你啦。"
)
GROUP_GUIDE = "这位朋友，我们好像还没正式认识呢。悄悄来私聊我一下，我告诉你怎么打招呼。"
BIND_OK = "记住你啦，以后随时都可以来找我说话。群聊里记得 @ 我一声。"
BIND_ALREADY = "我早就记住你啦。要是想换绑，得先回博客那边解绑一下哦。"
BIND_INVALID = "这串咒文对不上呢……回博客的个人资料页看看最新的绑定码吧。"
BIND_ACCOUNT_TAKEN = "这个博客账号已经跟别的 QQ 牵过手了……先在网页那边解绑，再回来找我吧。"
BIND_FORMAT = "绑定码是 8 位数字哦，长这样：/绑定 1234-5678。"
PRIVATE_ONLY = "私聊我一下，我把流程告诉你。"
```

`COMMANDS: tuple[Command, ...] = (Command("绑定", ("bind",), "private", "把 QQ 绑定到博客账号：/绑定 1234-5678", cmd_bind), Command("帮助", ("help", "commands"), "any", "看看我会哪些命令", cmd_help))`

`dispatch`：按 `name`/`aliases` 匹配（`parsed.name` 已小写）；`scope=="private" and not ctx.is_private` → `PRIVATE_ONLY`；否则 `handler(replace(ctx, args=parsed.args))`。未匹配 → `"这条咒文我没见过呢……我目前会的是：\n" + command_help()`。

`command_help()`：对每个命令输出 `/{name}`（别名用 `、` 连接）+ `help`，一行一条。

`cmd_bind(ctx)`：`ctx.bound_user_id is not None` → `BIND_ALREADY`；`not is_valid_bind_code(ctx.args)` → `BIND_FORMAT`；`ctx.store.bind_qq(str(ctx.qq_uin), hash_bind_code(ctx.args))` 按 `status` 映射到 `BIND_OK` / `BIND_INVALID` / `BIND_ALREADY`（`qq_taken`）/ `BIND_ACCOUNT_TAKEN`（`account_taken`）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_qq_commands.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/channels/qq/commands.py backend/tests/test_qq_commands.py
git commit -m "feat(qq): 可扩展命令注册表与 /绑定 命令"
```

---

### Task 6: 适配器支持私聊、命令与未绑定引导

**Files:**
- Modify: `backend/app/channels/qq/adapter.py`（解析、事件流、发送）
- Modify: `backend/app/channels/qq/client.py`（协议与两个实现加 `send_private_message`）
- Modify: `backend/tests/test_qq_adapter.py`（既有用例补上"已绑定"依赖）
- Test: `backend/tests/test_qq_private_chat.py`（新建）

**Interfaces:**
- Consumes: `parse_command`、`dispatch`、`CommandContext`（Task 5）；`PrivateGUIDE`/`GROUP_GUIDE`；`CreditStore.binding_of_qq`（Task 1）。
- Produces:
  - `OneBotTransport.send_private_message(user_id: int, message: str) -> Any`（协议 + `OneBotClient` + `MockOneBotClient`）
  - `QQAdapter.__init__(settings, sender, *, stream=stream_reply, credits: CreditStore | None = None)`
  - `QQAdapter._reply(key, content, *, is_admin: bool) -> tuple[str, list[ChatMessage], bool]`（第三项 = 是否产出过 delta 文本）
  - 事件返回值 `{"handled": bool, "reason"?: str, "reply"?: str, "send_error"?: str}`，新增 reason：`guide_unbound`、`command`、`quota_exhausted`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_qq_private_chat.py
import asyncio
import functools
from typing import Any

from app.channels.qq import commands
from app.channels.qq.adapter import QQAdapter
from app.config import Settings
from app.credits import CreditStore, hash_bind_code

def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper

class FakeSender:
    def __init__(self):
        self.group = []
        self.private = []
    async def send_group_message(self, group_id, message):
        self.group.append((group_id, message))
    async def send_private_message(self, user_id, message):
        self.private.append((user_id, message))

def stream_ok(text="我在"):
    async def _stream(messages, **kwargs):
        yield {"type": "delta", "content": text}
    return _stream

def private_event(qq_uin, text, message_id=1):
    return {
        "post_type": "message", "message_type": "private", "self_id": 999,
        "user_id": qq_uin, "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }

def group_event(qq_uin, text, message_id=1):
    return {
        "post_type": "message", "message_type": "group", "self_id": 999,
        "group_id": 888, "user_id": qq_uin, "message_id": message_id,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": text}},
        ],
    }

def make_adapter(tmp_path, *, stream=None, private_chat=True):
    settings = Settings(
        qq_enabled=True, qq_allowed_groups="888", qq_admin_users="",
        qq_mock_mode=True,
    )
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, stream=stream or stream_ok(), credits=store)
    return adapter, sender, store

@async_test
async def test_unbound_private_gets_guide_every_message(tmp_path):
    adapter, sender, _ = make_adapter(tmp_path)
    first = await adapter.handle_event(private_event(10001, "在吗", 1))
    second = await adapter.handle_event(private_event(10001, "在吗", 2))
    assert first["reason"] == "guide_unbound"
    assert second["reason"] == "guide_unbound"
    assert [text for _, text in sender.private] == [commands.PRIVATE_GUIDE, commands.PRIVATE_GUIDE]

@async_test
async def test_unbound_group_points_to_private(tmp_path):
    adapter, sender, _ = make_adapter(tmp_path)
    result = await adapter.handle_event(group_event(10001, "你好"))
    assert result["reason"] == "guide_unbound"
    assert sender.group == [(888, commands.GROUP_GUIDE)]

@async_test
async def test_bound_private_reaches_agent(tmp_path):
    adapter, sender, store = make_adapter(tmp_path)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    result = await adapter.handle_event(private_event(10001, "讲个故事"))
    assert result["handled"] is True
    assert sender.private == [(10001, "我在")]

@async_test
async def test_command_never_reaches_agent(tmp_path):
    calls = []
    async def stream(messages, **kwargs):
        calls.append(messages)
        yield {"type": "delta", "content": "不该出现"}
    adapter, sender, store = make_adapter(tmp_path, stream=stream)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    result = await adapter.handle_event(private_event(10001, "/绑定 1234-5678"))
    assert calls == []
    assert result["reason"] == "command"
    assert sender.private[0][1] == commands.BIND_OK

@async_test
async def test_unknown_command_in_group_does_not_reach_agent(tmp_path):
    calls = []
    async def stream(messages, **kwargs):
        calls.append(messages)
        yield {"type": "delta", "content": "不该出现"}
    adapter, sender, _ = make_adapter(tmp_path, stream=stream)
    await adapter.handle_event(group_event(10001, "/天气"))
    assert calls == []
    assert "这条咒文我没见过呢" in sender.group[0][1]

@async_test
async def test_private_and_group_history_are_separate(tmp_path):
    adapter, sender, store = make_adapter(tmp_path)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    await adapter.handle_event(private_event(10001, "私聊一", 1))
    await adapter.handle_event(group_event(10001, "群里一", 2))
    assert set(adapter._history) == {(0, 10001), (888, 10001)}

@async_test
async def test_duplicate_private_message_ignored(tmp_path):
    adapter, sender, _ = make_adapter(tmp_path)
    await adapter.handle_event(private_event(10001, "在吗", 5))
    await adapter.handle_event(private_event(10001, "在吗", 5))
    assert len(sender.private) == 1
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_qq_private_chat.py -v`
Expected: FAIL（私聊事件返回 `not_a_group_text_message`；`send_private_message` 不存在）

- [ ] **Step 3: 实现**

- `client.py`：`OneBotTransport` 加 `async def send_private_message(self, user_id: int, message: str) -> Any`；`OneBotClient` 实现为 `await self.call("send_private_msg", {"user_id": user_id, "message": message})`；`MockOneBotClient` 同步追加一条 `{"action": "send_private_msg", ...}` 到 outbox。
- `adapter.py`：
  - 新增 `@dataclass(frozen=True) class Incoming`，字段 `message_type, group_id, user_id, self_id, text, at_self, message_id`；把 `_parse_group_message` 改成 `_parse_message(payload)`，同时接受 `message_type in {"group","private"}`（私聊 `group_id=0`），其余逻辑不变。
  - `handle_event` 顺序：解析 → 跳过 self → 群聊白名单（私聊跳过）→ 去重 → 触发文本（群聊走 `_extract_trigger_text`，私聊整条文本非空即可）→ **命令** → **绑定判定** → `_reply`。
  - 命令：`parsed = parse_command(content)`；命中则 `dispatch(parsed, CommandContext(...))`，发送后返回 `{"handled": True, "reason": "command", "reply": reply}`，**不写历史**。
  - 绑定判定：`binding = credits.binding_of_qq(str(user_id))`；`admin = user_id in settings.qq_admin_user_ids`；`binding is None and not admin` → 回 `PRIVATE_GUIDE`（私聊）或 `GROUP_GUIDE`（群聊），发送后返回 `reason="guide_unbound"`。
  - 发送：私聊 `send_private_message(user_id, chunk)`，群聊 `send_group_message(group_id, chunk)`；`_send()` 统一处理异常（保留现有日志与 `send_error` 返回）。
  - 历史键：群聊 `(group_id, user_id)`，私聊 `(0, user_id)`。
  - `_reply`：签名加 `is_admin` 参数并返回 `(reply, new_history, produced)`，`produced = bool(parts)`。
  - `credits` 惰性获取：`store = self.credits or get_credit_store()`。

- [ ] **Step 4: 修既有 `test_qq_adapter.py`**

现有用例用随机 QQ 号直接调 `QQAdapter(...)`，改动后会被"未绑定"拦截；同时不能让它落到仓库里的默认数据库。做法：

```python
from app.credits import QQBinding

class AlwaysBound:
    """既有用例都假设“这个 QQ 已经绑过”，这里只提供读接口。"""
    def binding_of_qq(self, qq_uin):
        return QQBinding(user_id=1, tier="user", is_admin=False)
```

在每个 `QQAdapter(...)` 调用里加 `credits=AlwaysBound()`。两个 HTTP 端点用例（`test_event_endpoint_accepts_napcat_signature`、`test_event_endpoint_runs_mock_reply`）额外 `monkeypatch.setenv("QQ_ADMIN_USERS", "456")` 与 `monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))`，让它们走管理员短路、不碰真实额度库。

- [ ] **Step 5: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_qq_private_chat.py tests/test_qq_adapter.py -v`
Expected: PASS（新增 7 个 + 原有全部通过）

- [ ] **Step 6: 提交**

```bash
git add backend/app/channels/qq/adapter.py backend/app/channels/qq/client.py backend/tests/test_qq_private_chat.py backend/tests/test_qq_adapter.py
git commit -m "feat(qq): 私聊、命令分发与未绑定引导"
```

---

### Task 7: QQ 额度接入适配器（含退款与管理员豁免）

**Files:**
- Modify: `backend/app/channels/qq/adapter.py`
- Test: `backend/tests/test_qq_quota_flow.py`（新建）

**Interfaces:**
- Consumes: `QQQuotaGate`、`QQUsage`（Task 4）；Task 6 的事件流。
- Produces: `QQAdapter.__init__(..., gate: QQQuotaGate | None = None)`；额度相关文案常量 `QUOTA_USER`、`QUOTA_GLOBAL`、`AGENT_ERROR`（放 `commands.py` 里与其余固定文案同处）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_qq_quota_flow.py
import asyncio, functools

from app.channels.qq import commands
from app.channels.qq.adapter import QQAdapter
from app.config import Settings
from app.credits import CreditStore, hash_bind_code
from app.qq_quota import QQQuotaGate, QQQuotaStore
from app.quota import QuotaStore
from app.runtime_settings import RuntimeSettings

# 把 test_qq_private_chat.py 里的 async_test / FakeSender / private_event 原样复制到本文件顶部
# （backend/tests 不是包，跨文件 import 不可靠；这几个 helper 只有十几行）

def runtime(**overrides):
    base = dict(
        daily_quota=10, member_daily_quota=30, global_daily_quota=1000,
        agent_max_model_calls=5, agent_max_tool_calls=5, agent_max_seconds=30.0,
        model="deepseek-chat", credit_max_per_user=100,
        qq_daily_quota=1, qq_member_daily_quota=5, qq_global_daily_quota=0,
        qq_quota_shared=False, qq_unlimited=False,
    )
    base.update(overrides)
    return RuntimeSettings(**base)

def make(tmp_path, *, stream, runtime_values=None):
    settings = Settings(qq_enabled=True, qq_allowed_groups="888", qq_mock_mode=True)
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    values = runtime_values or runtime()
    gate = QQQuotaGate(
        QuotaStore(str(tmp_path / "quota.sqlite3")),
        QQQuotaStore(str(tmp_path / "quota.sqlite3")),
        lambda: values,
    )
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, stream=stream, credits=store, gate=gate)
    return adapter, sender, gate

@async_test
async def test_exhausted_quota_replies_in_merlin_voice(tmp_path):
    async def stream(messages, **kwargs):
        yield {"type": "delta", "content": "不该出现"}
    adapter, sender, _ = make(tmp_path, stream=stream)
    await adapter.handle_event(private_event(10001, "第一句", 1))
    result = await adapter.handle_event(private_event(10001, "第二句", 2))
    assert result["reason"] == "quota_exhausted"
    assert sender.private[-1] == (10001, commands.QUOTA_USER)

@async_test
async def test_global_qq_quota_message(tmp_path):
    async def stream(messages, **kwargs):
        yield {"type": "delta", "content": "我在"}
    adapter, sender, _ = make(
        tmp_path, stream=stream,
        runtime_values=runtime(qq_daily_quota=10, qq_global_daily_quota=1),
    )
    await adapter.handle_event(private_event(10001, "第一句", 1))
    result = await adapter.handle_event(private_event(10001, "第二句", 2))
    assert result["reason"] == "quota_exhausted"
    assert sender.private[-1] == (10001, commands.QUOTA_GLOBAL)

@async_test
async def test_refund_when_agent_produces_nothing(tmp_path):
    async def stream(messages, **kwargs):
        yield {"type": "error", "message": "boom"}
    adapter, sender, gate = make(tmp_path, stream=stream)
    first = await adapter.handle_event(private_event(10001, "第一句", 1))
    import sqlite3
    with sqlite3.connect(str(tmp_path / "quota.sqlite3")) as conn:
        used = conn.execute(
            "SELECT used FROM qq_daily_usage WHERE qq_uin='10001'"
        ).fetchone()[0]
    assert used == 0
    assert first["reply"] == commands.AGENT_ERROR
    # 额度没被吃掉：同一天还能成功一次
    result = await adapter.handle_event(private_event(10001, "再来", 2))
    assert result["handled"] is True and result.get("reason") != "quota_exhausted"

@async_test
async def test_admin_binding_is_not_counted(tmp_path):
    async def stream(messages, **kwargs):
        yield {"type": "delta", "content": "我在"}
    settings = Settings(qq_enabled=True, qq_allowed_groups="888", qq_mock_mode=True)
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=True)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    gate = QQQuotaGate(
        QuotaStore(str(tmp_path / "quota.sqlite3")),
        QQQuotaStore(str(tmp_path / "quota.sqlite3")),
        lambda: runtime(qq_daily_quota=0),
    )
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, stream=stream, credits=store, gate=gate)
    result = await adapter.handle_event(private_event(10001, "你好", 1))
    assert result["handled"] is True
    import sqlite3
    with sqlite3.connect(str(tmp_path / "quota.sqlite3")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM qq_daily_usage").fetchone()[0] == 0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_qq_quota_flow.py -v`
Expected: FAIL（`QQAdapter` 不接受 `gate`；`QUOTA_USER` 未定义）

- [ ] **Step 3: 实现**

- `commands.py` 增加：

```python
QUOTA_USER = "今天的魔力用完了，明天再来找我吧。"
QUOTA_GLOBAL = "今天大家聊得太热闹，我的魔力见底了，明天再来吧。"
AGENT_ERROR = "抱歉，我这边好像出了点岔子，稍后再来找我一次吧。"
```

- `adapter.py`：`__init__` 增加 `gate: QQQuotaGate | None = None`（与 `credits` 一样惰性取 `get_qq_quota_gate()`，在 `qq_quota.py` 里加一个 `lru_cache` 工厂：`QQQuotaGate(get_quota_store(), get_qq_quota_store(), lambda: get_runtime_store().load())`）。
- 流程：绑定判定通过后 `status, usage = gate.reserve(qq_uin=str(user_id), user_id=binding.user_id, tier=binding.tier, is_admin=admin or binding.is_admin)`；`status == "user_exhausted"` → 回 `QUOTA_USER`；`"global_exhausted"` → 回 `QUOTA_GLOBAL`；返回 `reason="quota_exhausted"`。
- `_reply` 返回的 `produced` 为 False 时调用 `gate.refund(usage)`；agent 无正文时回复固定为 `AGENT_ERROR`。
- 既有 `test_qq_adapter.py` 的适配器构造再补一个放行门面，避免落到真实额度库：

```python
from app.qq_quota import QQUsage

class AllowAllGate:
    def reserve(self, **kwargs):
        return "ok", QQUsage("admin", None, None)
    def refund(self, usage):
        return None
```

  在每个 `QQAdapter(...)` 调用里加 `gate=AllowAllGate()`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_qq_quota_flow.py tests/test_qq_private_chat.py tests/test_qq_adapter.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add backend/app/channels/qq/adapter.py backend/app/channels/qq/commands.py backend/app/qq_quota.py backend/tests/test_qq_quota_flow.py
git commit -m "feat(qq): 额度接入、管理员豁免与无产出退款"
```

---

### Task 8: `/api/me/bind` 与管理员解绑接口

**Files:**
- Create: `backend/app/api/me_bind.py`
- Modify: `backend/app/main.py`（挂载路由）
- Modify: `backend/app/api/admin_users.py`（新增 `DELETE /admin/users/{user_id}/bind`）
- Modify: `backend/app/schemas.py`（`BindCodeUpdate`）
- Test: `backend/tests/test_bind_api.py`（新建）

**Interfaces:**
- Consumes: `require_user` / `require_admin`；`AuthUser.id`、`AuthUser.is_admin`；`get_credit_store()`；Task 1 的存储方法。
- Produces:
  - `PUT /api/me/bind-code` 请求体 `{"code": "1234-5678"}`，成功 204；格式非法 400 `detail="绑定码应为 8 位数字"`。
  - `GET /api/me/bind` → `{"qq_uin": str | null}`。
  - `DELETE /api/me/bind` → 204。
  - `DELETE /api/admin/users/{user_id}/bind` → 204。
  - `is_admin` 取自 JWT 的 `admin` 声明（**不信任请求体**）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_bind_api.py
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store, hash_bind_code
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "bind-test-secret-" + "z" * 48

def _token(user_id: int, *, admin: bool = False) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "l2d-ai-wordpress",
            "aud": "l2d-ai-backend",
            "sub": str(user_id),
            "name": "测试用户",
            "admin": admin,
            "iat": now,
            "exp": now + 900,
        },
        SECRET,
        algorithm="HS256",
    )

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_credit_store.cache_clear()
    get_runtime_store.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    get_credit_store.cache_clear()

def test_put_bind_code_then_get_and_delete(client):
    token = _token(7)
    response = client.put(
        "/api/me/bind-code", json={"code": "1234-5678"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204
    assert client.get("/api/me/bind", headers={"Authorization": f"Bearer {token}"}).json() == {
        "qq_uin": None
    }
    # 绑定后 GET 反映出来
    get_credit_store().bind_qq("10001", hash_bind_code("1234-5678"))
    assert client.get("/api/me/bind", headers={"Authorization": f"Bearer {token}"}).json() == {
        "qq_uin": "10001"
    }
    assert client.delete(
        "/api/me/bind", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 204
    assert get_credit_store().qq_uin_of(7) is None

def test_put_bind_code_rejects_bad_format(client):
    response = client.put(
        "/api/me/bind-code", json={"code": "abc"},
        headers={"Authorization": f"Bearer {_token(7)}"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "绑定码应为 8 位数字"

def test_is_admin_comes_from_token(client):
    client.put(
        "/api/me/bind-code", json={"code": "1234-5678"},
        headers={"Authorization": f"Bearer {_token(7, admin=True)}"},
    )
    get_credit_store().bind_qq("10001", hash_bind_code("1234-5678"))
    assert get_credit_store().binding_of_qq("10001").is_admin is True

def test_admin_can_force_unbind(client):
    store = get_credit_store()
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    response = client.delete(
        "/api/admin/users/7/bind",
        headers={"Authorization": f"Bearer {_token(1, admin=True)}"},
    )
    assert response.status_code == 204
    assert store.qq_uin_of(7) is None

def test_non_admin_cannot_force_unbind(client):
    response = client.delete(
        "/api/admin/users/7/bind",
        headers={"Authorization": f"Bearer {_token(7, admin=False)}"},
    )
    assert response.status_code == 403
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_bind_api.py -v`
Expected: FAIL（404 Not Found）

- [ ] **Step 3: 实现**

`me_bind.py`：`router = APIRouter()`，三个端点都用 `Depends(require_user)`，数据库调用包 `asyncio.to_thread`（与 `api/quota.py` 一致）。`PUT` 体用 `BindCodeUpdate`；`is_admin=user.is_admin`。

`main.py`：`from app.api.me_bind import router as me_bind_router`，`app.include_router(me_bind_router, prefix="/api")`（紧跟现有 `prefix="/api"` 的路由）。

`admin_users.py`：`@router.delete("/admin/users/{user_id}/bind", status_code=204)`，`Depends(require_admin)`，调用 `unbind_user(user_id)`，无论是否绑过都返回 204。

`schemas.py`：

```python
class BindCodeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=64)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_bind_api.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/me_bind.py backend/app/api/admin_users.py backend/app/main.py backend/app/schemas.py backend/tests/test_bind_api.py
git commit -m "feat(api): 绑定码同步、查询、解绑与管理端强制解绑"
```

---

### Task 9: WordPress 个人资料页绑定卡片

**Files:**
- Create: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-bind.php`
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`（require、`init()` 钩子、`user_token()` / `user_backend_request()`）
- Verify: 逐行审阅（无 PHP 解释器）

**Interfaces:**
- Consumes: `L2D_AI_Plugin::request_backend()` 的传输逻辑、`encode_jwt()`、`jwt_secret()`、`backend_url()`；后端 `GET/PUT/DELETE /api/me/bind*`（Task 8）。
- Produces:
  - `L2D_AI_Plugin::user_backend_request( $user_id, $method, $path, $body = null )`（成功数组 / `WP_Error`）
  - `L2D_AI_Bind::CODE_META = 'l2d_ai_qq_bind_code'`、`SYNC_META = 'l2d_ai_qq_bind_code_synced'`
  - `L2D_AI_Bind::init()` 注册 `show_user_profile`、`admin_post_l2d_ai_bind_refresh`、`admin_post_l2d_ai_bind_unbind`、`deleted_user`

- [ ] **Step 1: 先做 main 插件的可复用传输**

把 `request_backend()` 内部“带 token 发请求”的部分抽成 `private static function backend_request_with_token( $token, $method, $path, $body = null )`；`request_backend()` 变成 `admin_token()` + 该函数。新增：

```php
public static function user_backend_request( $user_id, $method, $path, $body = null ) {
    $token = self::user_token( (int) $user_id );
    if ( is_wp_error( $token ) ) {
        return $token;
    }
    return self::backend_request_with_token( $token, $method, $path, $body );
}
```

`user_token( $user_id )` 与 `admin_token()` 同构，但 `'sub' => (string) $user_id`、`'admin' => user_can( $user_id, 'manage_options' )`、TTL 沿用 `JWT_TTL`。

- [ ] **Step 2: 新类 `L2D_AI_Bind`**

- `render_card( $user )`：`show_user_profile` 回调；`$user_id = (int) $user->ID`；`current_user_can( 'edit_user', $user_id )` 不通过就 return。
- 取当前绑定状态：`L2D_AI_Plugin::user_backend_request( $user_id, 'GET', '/me/bind' )`；`WP_Error` → 渲染"暂时无法同步绑定状态，请稍后刷新页面"，不渲染按钮。
- 本地码：`get_user_meta( $user_id, self::CODE_META, true )`；为空 → 生成 `wp_rand( 10000000, 99999999 )` 存 meta，再拆成 `XXXX-XXXX` 展示。
- 同步：若 `get_user_meta( $user_id, self::SYNC_META, true ) !== $code`，`PUT /me/bind-code` with `array( 'code' => $code )`，成功后写 `SYNC_META`。（`is_admin` 由 JWT 决定，不随请求体发送。）
- 已绑定：显示 `状态：已绑定 QQ <qq_uin>`，不显示绑定码区域，显示一句"如需换绑，请先解绑再刷新绑定码。"
- 未绑定：显示状态、绑定码、复制按钮（`navigator.clipboard.writeText`，失败提示手动选中）、`/绑定 XXXX-XXXX` 示例、刷新按钮、同步失败提示。
- 两个表单都提交到 `admin_url('admin-post.php')`，带 `wp_nonce_field( 'l2d_ai_bind' )` 与 `user_id` 隐藏字段。
- `handle_refresh()`：`check_admin_referer`、能力校验、生成新码写 meta、删 `SYNC_META`、`user_backend_request( ..., 'PUT', '/me/bind-code', array( 'code' => $code ) )`、`wp_safe_redirect` 回 `admin_url('profile.php')` + `?l2d-ai-bind=refreshed`，`exit`。
- `handle_unbind()`：`DELETE /me/bind`，成功后删两个 meta（解绑即换码），重定向 `?l2d-ai-bind=unbound`。
- 解绑按钮加 `onclick="return confirm('解绑后这个 QQ 将无法再和我聊天，确定吗？');"`。
- 全部输出用 `esc_html()` / `esc_attr()` / `esc_url()`。

- [ ] **Step 3: 接入主文件**

`wp-live2d-ai.php` 顶部 `require_once __DIR__ . '/includes/class-l2d-ai-bind.php';`；`init()` 里加 `L2D_AI_Bind::init();`。

- [ ] **Step 4: 逐行审阅**

检查清单（逐条在文件里确认）：`require` 路径正确；每个表单有 nonce 且 handler 里 `check_admin_referer`；每个 handler 先做能力校验；所有回显都有转义；`wp_rand` 生成 8 位且补零（`str_pad`）；`admin_post_*` 钩子名与表单 `action` 值一一对应；括号/`endif` 配对；没有把绑定码写进日志或 URL 查询参数。

- [ ] **Step 5: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-bind.php wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php
git commit -m "feat(wp): 个人资料页 QQ 绑定卡片（刷新/解绑/同步）"
```

---

### Task 10: WordPress 后台 QQ 额度设置段

**Files:**
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`（`save_backend_settings()` 与后端参数表单）
- Verify: 逐行审阅

**Interfaces:**
- Consumes: 后端 `PUT /api/admin/settings`（Task 3 字段）。
- Produces: 提交时始终发送 `qq_quota_shared`、`qq_unlimited` 两个布尔值（未勾选为 `false`），三个整数按范围校验。

- [ ] **Step 1: 保存逻辑**

`$rules` 追加：

```php
'qq_daily_quota'        => array( 0, 1000 ),
'qq_member_daily_quota' => array( 0, 1000 ),
'qq_global_daily_quota' => array( 0, 1000000 ),
```

整数循环之后追加：

```php
$patch['qq_quota_shared'] = ! empty( $_POST['qq_quota_shared'] );
$patch['qq_unlimited']    = ! empty( $_POST['qq_unlimited'] );
```

（必须每次显式发送布尔值：只提交整数时取消勾选不会生效。）

- [ ] **Step 2: 表单**

在后端参数表单 `member_daily_quota` 所在表格之后新增一段，标题行用 `<tr><th colspan="2"><h2>QQ 通道</h2></th></tr>`，随后四到五行：

- 复选框 `name="qq_quota_shared"`，`checked` 用 `$get( $settings, 'qq_quota_shared', false )` 转 `'1'` 比较；说明"开启后 QQ 与网页共用每日额度；关闭则使用下面这套 QQ 独立额度。"
- 数字 `qq_daily_quota`（min 0 max 1000），说明"每个 QQ 每天可用条数，0 表示不能聊。"
- 数字 `qq_member_daily_quota`（min 0 max 1000），说明"绑定账号是会员时使用这一档。"
- 数字 `qq_global_daily_quota`（min 0 max 1000000），说明"QQ 全站每天合计，0 表示不限；共享模式下同样生效。"
- 复选框 `qq_unlimited`，说明"开启后按人和按会员额度都不生效，QQ 全站额度仍然生效。"

沿用现有 `$get()` helper 与 `esc_attr()` 写法。

- [ ] **Step 3: 逐行审阅**

确认：新字段都能从 `$settings` 读到（后端不可达时回退 `$defaults`）；未勾选的复选框会以 `false` 进 `$patch`；`$rules` 的键与后端字段名完全一致；表单没有重复 `name`；`checked()` 与 `! empty()` 的组合对 `"1"` / `true` 都成立。

- [ ] **Step 4: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php
git commit -m "feat(wp): 后台 QQ 额度与共享开关"
```

---

### Task 11: 用户与额度页 QQ 列 + 删除用户解绑

**Files:**
- Modify: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-users.php`（QQ 列）
- Modify: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-bind.php`（`deleted_user` 钩子实现）
- Verify: 逐行审阅

**Interfaces:**
- Consumes: `profile_snapshot()` 已返回 `qq_uin`；`L2D_AI_Plugin::request_backend()`。
- Produces: 表格 QQ 列显示已绑定的 QQ 号，未绑定显示 `—`；删除 WordPress 用户时调用 `DELETE /api/admin/users/{user_id}/bind`。

- [ ] **Step 1: 表格**

在 `render_table()` 的 `<td>—</td>`（QQ 列，`$credit` 之后）改为：

```php
<?php
$qq_uin = isset( $profile['qq_uin'] ) ? (string) $profile['qq_uin'] : '';
echo '' !== $qq_uin ? esc_html( $qq_uin ) : '—';
?>
```

- [ ] **Step 2: 删除钩子**

`L2D_AI_Bind::init()` 已注册 `deleted_user` → `handle_deleted_user( $user_id )`：

```php
if ( ! is_user_logged_in() || ! current_user_can( 'manage_options' ) ) {
    return; // WP-CLI 或非管理员触发的删除不强求同步；后端会在下次绑定时覆盖检查
}
L2D_AI_Plugin::request_backend( 'DELETE', '/admin/users/' . (int) $user_id . '/bind' );
```

（`request_backend` 失败只记日志：`error_log( 'L2D AI 解绑失败: ' . $result->get_error_message() );`，不阻断删除流程。）

- [ ] **Step 3: 逐行审阅**

确认 `$profile` 在该作用域已定义、`int` 转换存在、`esc_html` 已用、`deleted_user` 回调只有一个参数、钩子注册在 `init()` 里。

- [ ] **Step 4: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-users.php wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-bind.php
git commit -m "feat(wp): 用户表显示 QQ 绑定，删除用户时自动解绑"
```

---

### Task 12: 文档

**Files:**
- Modify: `docs/deploy.md`（新增可选环境变量）
- Modify: `docs/architecture.md`（QQ 通道与额度一段）

**Interfaces:**
- Consumes: 前 11 个任务的最终行为。

- [ ] **Step 1: 部署文档**

在 `.env` 段落追加一段说明：`QQ_DAILY_QUOTA`、`QQ_MEMBER_DAILY_QUOTA`、`QQ_GLOBAL_DAILY_QUOTA`、`QQ_QUOTA_SHARED`、`QQ_UNLIMITED` **只是初始默认值**；正式运营请在 WordPress 后台"Live2D 小助手 → 聊天额度与运行参数 → QQ 通道"里改，保存即生效，不需要重启。并写明：绑定码在"用户 → 个人资料"页查看；解绑也只能在那里。

- [ ] **Step 2: 架构文档**

在 QQ 通道一节补：命令前缀规则（`/`、`\`、`／`、`＼`，永不进模型）、绑定码流程（明文在 WP、哈希在后端）、两套额度模式与 `qq_quota_shared` 开关、`qq_global_daily_quota` 独立计数在两种模式下都生效。

- [ ] **Step 3: 提交**

```bash
git add docs/deploy.md docs/architecture.md
git commit -m "docs: QQ 绑定与双额度模式说明"
```

---

## Self-Review

- **Spec coverage:** §4.1→Task 9；§4.2/4.4→Task 1、9；§4.3/4.5→Task 5；§5.1/5.2/5.3/5.4/5.5→Task 6、7；§6.1–6.5→Task 2、3、4、7；§7→Task 3、10、11；§8.1→Task 1、2；§8.2→Task 8；§8.4→Task 11；§9→Task 1、8、9；§10 验收 1–18 分别落在 Task 1（1–4）、4（5–9、12）、6（14–15、17–18）、9（10）、10（11）、11（13）、3（11）。§11/§12 为实现决定，已在 Task 边界内落定。
- **类型一致性：** `BindResult.status` 的四个取值在 Task 1/5/8 一致；`QQQuotaResult.status` 的三个取值在 Task 2/4/7 一致；`QQUsage.kind` 三个取值只出现在 Task 4/7。
- **Review Focus 已落到测试：** (1) Task 1 `test_qq_already_bound_to_other_account` / `test_account_already_bound_to_other_qq`；(2) Task 2 与 Task 4 的 0/不限/边界用例；(3) Task 5 全角/未知/群里私聊专用 + Task 6 命令不进 agent；(4) Task 7 `test_refund_when_agent_produces_nothing`；(5) Task 6 去重与每条必回。
