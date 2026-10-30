# 注册防护、三级身份与额外额度 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 挡住批量注册的垃圾账号，让后台看得到每个用户的注册信息与额度，并把额度扩成「每日额度 → 额外存量额度」两级，中间加一档有期限的会员身份。

**Architecture:** 等级与额度存量放后端 SQLite（新增 `app/credits.py`，和 `app/quota.py` 共用一个库文件、共享建表 SQL）；邮箱与注册信息仍只存在 WordPress，插件新增两个后台页，用现有 `admin_token()` 调后端接口后在 PHP 里合并成表格。额度消耗在 `QuotaStore.reserve()` 的同一个事务里完成「每日 → 存量」，全站额度是硬上限、放在事务最后一步。

**Tech Stack:** FastAPI + pydantic-settings + SQLite（`sqlite3` 标准库）+ pytest；WordPress 插件（PHP，单文件现有结构 + 新增 `includes/` 两个类）；前端 TypeScript + Vite。

**Spec:** [docs/superpowers/specs/2026-10-30-registration-guard-membership-credits-design.md](../specs/2026-10-30-registration-guard-membership-credits-design.md)

## Global Constraints

- 后端测试不调用真实模型，也不联网；跑法固定在 `backend/` 下 `uv run pytest -q`。
- Python 3.11+，代码风格沿用现有文件：中文注释、`from __future__ import annotations`、ruff 通过。
- 建表一律 `CREATE TABLE IF NOT EXISTS`，**不写迁移脚本**；表都在 `QUOTA_DB_PATH` 指向的同一个 SQLite 文件里。
- 后端**不存邮箱、不存昵称**；`/api/admin/*` 的响应里不出现邮箱字段。
- 会员一律带时长：`user_profile.tier_expires_at` 对 `member` 必填，读到过期就惰性降级。
- 额度顺序固定：先扣每日（会刷新），扣不动再扣存量（不刷新）。全站额度是硬上限，两种资金来源都计数，占位失败必须整体回滚。
- 兑换码存明文，格式 16 位 Crockford Base32、四段连字符（`XXXX-XXXX-XXXX-XXXX`）。
- 不提交任何密钥、QQ 号、服务器地址、真实邮箱到仓库。
- 网页端每处新增文案用中文，风格和现有 `app/api/chat.py` 的 429 文案一致。
- **测试隔离**：任何 `monkeypatch.setenv("QUOTA_DB_PATH", ...)` 的 fixture 都必须同时调 `get_credit_store.cache_clear()`（和现有的 `get_quota_store.cache_clear()` 并列）。两个 store 各有独立的 `lru_cache`，漏了会把测试数据写进仓库里的 `backend/data/quota.sqlite3`。

## Review Focus

| 会咬人的输入/状况 | 合理预期 | 钉在哪 |
| --- | --- | --- |
| 兑换码小写、带空格或没有连字符 | 正常兑换，不因为格式差异报错 | Task 3 |
| 用户已被 WordPress 删除，后端仍有 profile 行 | 后台列表按 WP 为准、不报错；快照不会因此崩 | Task 2 |
| 全站额度为 0（表示不限） | 所有人都能正常聊，不能被硬上限逻辑误伤成 429 | Task 4 |
| 当天额度用尽、存量刚好为 0、并发进来 | 全部拒绝，余额不出负数 | Task 4 |
| 管理员本人在用户列表里 | 显示「管理员」，改等级/发额度不报错也不影响不限量 | Task 9 |

---

### Task 1: 等级存储与到期降级

**Files:**
- Create: `backend/app/credits.py`
- Test: `backend/tests/test_credit_store.py`

**Interfaces:**
- Consumes: `app.config.get_settings`（只用 `quota_db_path`、`quota_timezone`）
- Produces:
  - `CREDIT_SCHEMA_SQL: str` —— 五张表的建表语句，`quota.py` 也要执行它
  - `TIERS: tuple[str, ...] = ("user", "member")`
  - `class CreditStore: def __init__(self, db_path: str, timezone: str = "Asia/Shanghai", max_credits: int = 10000, now: Callable[[], datetime] | None = None)`
  - `CreditStore.tier_of(user_id: int) -> tuple[str, str | None]`
  - `CreditStore.set_tier(user_id: int, tier: str, days: int | None = None) -> str | None`
  - `get_credit_store() -> CreditStore`：`lru_cache(maxsize=1)`，用 `settings.quota_db_path`、`settings.quota_timezone`；
    本任务先固定 `max_credits=10_000`，Task 5 再改成读 `settings.credit_max_per_user`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_credit_store.py
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.credits import CreditStore

TZ = ZoneInfo("Asia/Shanghai")


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def make_store(tmp_path, clock: Clock | None = None) -> CreditStore:
    return CreditStore(
        str(tmp_path / "quota.sqlite3"),
        "Asia/Shanghai",
        max_credits=1000,
        now=clock,
    )


def test_tier_defaults_to_user(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.tier_of(42) == ("user", None)


def test_set_member_returns_expiry(tmp_path) -> None:
    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)

    expires = store.set_tier(42, "member", days=7)

    assert expires == "2026-11-06T12:00:00+08:00"
    assert store.tier_of(42) == ("member", "2026-11-06T12:00:00+08:00")


def test_expired_member_is_downgraded_on_read(tmp_path) -> None:
    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)
    store.set_tier(42, "member", days=1)

    clock.current = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)

    assert store.tier_of(42) == ("user", None)
    # 降级要落盘，不能每次读都重新判断
    assert store.tier_of(42) == ("user", None)


def test_renew_extends_from_current_expiry(tmp_path) -> None:
    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)
    store.set_tier(42, "member", days=7)

    clock.current = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)
    expires = store.set_tier(42, "member", days=3)

    # 从原到期日 11-06 往后加 3 天，而不是从今天重算
    assert expires == "2026-11-09T12:00:00+08:00"


def test_set_member_requires_days(tmp_path) -> None:
    import pytest

    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.set_tier(42, "member", days=None)


def test_set_user_clears_expiry(tmp_path) -> None:
    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)
    store.set_tier(42, "member", days=7)

    assert store.set_tier(42, "user", days=None) is None
    assert store.tier_of(42) == ("user", None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_credit_store.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.credits'`

- [ ] **Step 3: 实现 `credits.py` 的骨架**

`CREDIT_SCHEMA_SQL` 逐字照抄设计文档第 6 节（`user_profile` 含 `tier_expires_at`、`qq_uin`；`credit_balance`；`credit_ledger` + 索引；`redeem_code`；`redeem_log`）。

`CreditStore.__init__`：保存 `db_path`、`timezone`、`max_credits`、`self._now = now or (lambda: datetime.now(ZoneInfo(timezone)))`，`mkdir(parents=True, exist_ok=True)` 建父目录，然后 `executescript(CREDIT_SCHEMA_SQL)`。连接用 `sqlite3.connect(self.db_path)` + `row_factory = sqlite3.Row`，写操作显式 `BEGIN IMMEDIATE`。

时间统一存 `self._now().isoformat(timespec="seconds")`（带时区偏移）；比较时用 `datetime.fromisoformat` 解析，**不要直接比字符串**。

`tier_of`：查 `user_profile`；没有行返回 `("user", None)`。若 `tier == "member"` 且 `tier_expires_at` 已过期，先 `UPDATE ... SET tier='user', tier_expires_at=NULL` 落盘，再返回 `("user", None)`。

`set_tier`：`tier` 不在 `TIERS` 抛 `ValueError`；`member` 且 `days is None or days <= 0` 抛 `ValueError("会员必须给出正数天数")`。基准时间 = `max(now, 现有未过期到期时间)`；`user` 时 `tier_expires_at = None`。用 `INSERT ... ON CONFLICT(user_id) DO UPDATE` 落盘，返回新的到期字符串或 `None`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_credit_store.py -q`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/credits.py backend/tests/test_credit_store.py
git commit -m "feat(credits): 用户等级存储与到期惰性降级"
```

---

### Task 2: 额外额度的余额、流水与快照

**Files:**
- Modify: `backend/app/credits.py`
- Test: `backend/tests/test_credit_store.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `CreditStore`、`CREDIT_SCHEMA_SQL`
- Produces:
  - `@dataclass(frozen=True, slots=True) class Profile: user_id: int; tier: str; tier_expires_at: str | None; credit: int; note: str | None; qq_uin: str | None`
  - `CreditStore.balance_of(user_id: int) -> int`
  - `CreditStore.grant_credits(user_id: int, delta: int, reason: str, ref: str | None = None) -> int`
  - `CreditStore.profile_snapshot(user_ids: Iterable[int]) -> dict[int, Profile]`
  - `CreditStore._grant_locked(conn: sqlite3.Connection, user_id: int, delta: int, reason: str, ref: str | None) -> int`（Task 3 的 `redeem` 在事务内复用）

- [ ] **Step 1: 写失败测试**

```python
def test_grant_and_balance(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.balance_of(7) == 0
    assert store.grant_credits(7, 5, "admin_grant") == 5
    assert store.grant_credits(7, -2, "admin_grant") == 3
    assert store.balance_of(7) == 3


def test_grant_never_goes_negative(tmp_path) -> None:
    import pytest

    store = make_store(tmp_path)
    store.grant_credits(7, 2, "admin_grant")
    with pytest.raises(ValueError):
        store.grant_credits(7, -3, "admin_grant")
    assert store.balance_of(7) == 2


def test_grant_respects_max_per_user(tmp_path) -> None:
    import pytest

    store = make_store(tmp_path)  # max_credits=1000
    with pytest.raises(ValueError):
        store.grant_credits(7, 1001, "admin_grant")


def test_ledger_records_every_change(tmp_path) -> None:
    import sqlite3

    store = make_store(tmp_path)
    store.grant_credits(7, 5, "admin_grant")
    store.grant_credits(7, -1, "chat")

    with sqlite3.connect(tmp_path / "quota.sqlite3") as conn:
        rows = conn.execute(
            "SELECT delta, reason FROM credit_ledger WHERE user_id = 7 ORDER BY id"
        ).fetchall()

    assert rows == [(5, "admin_grant"), (-1, "chat")]


def test_profile_snapshot_handles_unknown_and_empty(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_tier(7, "member", days=3)
    store.grant_credits(7, 4, "admin_grant")

    snapshot = store.profile_snapshot([7, 99])
    assert snapshot[7].tier == "member"
    assert snapshot[7].credit == 4
    assert snapshot[99].tier == "user"
    assert snapshot[99].credit == 0
    assert store.profile_snapshot([]) == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_credit_store.py -q`
Expected: FAIL，`AttributeError: 'CreditStore' object has no attribute 'balance_of'`

- [ ] **Step 3: 实现**

`_grant_locked(conn, ...)`：`delta` 不为 0；`delta < 0` 时用 `UPDATE credit_balance SET balance = balance + ?, updated_at = ? WHERE user_id = ? AND balance + ? >= 0 RETURNING balance`（注意参数绑定顺序），无返回行则抛 `ValueError("余额不足，不能扣成负数")`；`delta > 0` 时同样原子更新并在 `balance > max_credits` 时抛 `ValueError("单个用户的额外额度上限是 N 条")`；用户还没有行时先 `INSERT ... ON CONFLICT DO NOTHING`。每次成功都插一条 `credit_ledger`。返回新余额。

`grant_credits`：`BEGIN IMMEDIATE` → `_grant_locked` → `commit`，失败 `rollback` 后抛出。

`balance_of`：`SELECT balance FROM credit_balance WHERE user_id = ?`，无行返回 0。

`profile_snapshot`：空列表直接返回 `{}`；一次性 `SELECT ... FROM user_profile WHERE user_id IN (?,?,...)` 取等级与备注，再一次性 `SELECT user_id, balance FROM credit_balance WHERE user_id IN (...)` 取余额，在 Python 里合并；请求里没有 profile 行的用户填 `tier="user"`、`credit=0`。**不要**在循环里逐用户查询。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_credit_store.py -q`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/credits.py backend/tests/test_credit_store.py
git commit -m "feat(credits): 额外额度余额、流水与批量快照"
```

---

### Task 3: 兑换码

**Files:**
- Modify: `backend/app/credits.py`
- Test: `backend/tests/test_credit_store.py`（追加）

**Interfaces:**
- Consumes: Task 1、Task 2 的全部方法
- Produces:
  - `class RedeemError(Exception)`，构造 `RedeemError(message: str)`
  - `@dataclass(frozen=True, slots=True) class RedeemResult: kind: str; credit_added: int; balance: int; tier: str; tier_expires_at: str | None; message: str`
  - `CreditStore.create_codes(*, kind: str, value: int, count: int = 1, max_uses: int = 1, expires_at: str | None = None, note: str | None = None) -> list[str]`
  - `CreditStore.redeem(user_id: int, code: str) -> RedeemResult`
  - `CreditStore.recent_codes(limit: int = 50) -> list[dict[str, object]]`
  - `CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"`

- [ ] **Step 1: 写失败测试**

```python
def test_create_and_redeem_credit_code(tmp_path) -> None:
    store = make_store(tmp_path)
    codes = store.create_codes(kind="credit", value=5)

    assert len(codes) == 1
    assert len(codes[0]) == 19 and codes[0].count("-") == 3

    result = store.redeem(7, codes[0])
    assert result.kind == "credit"
    assert result.credit_added == 5
    assert result.balance == 5
    assert store.balance_of(7) == 5


def test_redeem_is_tolerant_of_input_format(tmp_path) -> None:
    store = make_store(tmp_path)
    code = store.create_codes(kind="credit", value=1)[0]

    assert store.redeem(7, code.lower()).balance == 1
    assert store.redeem(8, f"  {code.replace('-', '')}  ").balance == 1


def test_code_is_single_use_per_user_but_shareable(tmp_path) -> None:
    import pytest

    store = make_store(tmp_path)
    code = store.create_codes(kind="credit", value=1, max_uses=2)[0]

    store.redeem(7, code)
    with pytest.raises(RedeemError):
        store.redeem(7, code)
    # 另一个用户还能用
    assert store.redeem(8, code).balance == 1


def test_exhausted_code_rejected(tmp_path) -> None:
    import pytest

    store = make_store(tmp_path)
    code = store.create_codes(kind="credit", value=1, max_uses=1)[0]
    store.redeem(7, code)

    with pytest.raises(RedeemError):
        store.redeem(8, code)


def test_expired_code_rejected(tmp_path) -> None:
    import pytest

    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)
    code = store.create_codes(kind="credit", value=1, expires_at="2026-10-31T00:00:00+08:00")[0]

    clock.current = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    with pytest.raises(RedeemError):
        store.redeem(7, code)


def test_member_code_grants_days(tmp_path) -> None:
    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)
    code = store.create_codes(kind="member", value=30)[0]

    result = store.redeem(7, code)

    assert result.kind == "member"
    assert result.tier == "member"
    assert result.tier_expires_at == "2026-11-29T12:00:00+08:00"


def test_unknown_code_rejected(tmp_path) -> None:
    import pytest

    store = make_store(tmp_path)
    with pytest.raises(RedeemError):
        store.redeem(7, "AAAA-BBBB-CCCC-DDDD")
```

测试文件顶部需要 `from app.credits import CreditStore, RedeemError`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_credit_store.py -q`
Expected: FAIL，`ImportError: cannot import name 'RedeemError'`

- [ ] **Step 3: 实现**

`create_codes`：校验 `kind in {"credit","member"}`、`value >= 1`、`1 <= count <= 50`、`1 <= max_uses <= 1000`。码用 `secrets.choice(CODE_ALPHABET)` 生成 16 位再按 4 位插连字符；`INSERT` 撞 `sqlite3.IntegrityError` 时重试（最多 5 次，同一 `count` 内换码）。

`redeem` 的规范化：`code.strip().upper().replace(" ", "")`；**用户没打连字符时按 4 位一组自动补**（长度 16 时插入连字符）。

`redeem` 全程 `BEGIN IMMEDIATE`：

1. `SELECT * FROM redeem_code WHERE code = ?`，无行 → `RedeemError("兑换码不存在")`；
2. `expires_at` 非空且已过期 → `RedeemError("这个兑换码已经过期了")`；
3. `used_count >= max_uses` → `RedeemError("这个兑换码已经被用完了")`；
4. `SELECT 1 FROM redeem_log WHERE code = ? AND user_id = ?` 有行 → `RedeemError("你已经兑换过这个码了")`；
5. `UPDATE redeem_code SET used_count = used_count + 1 WHERE code = ? AND used_count < max_uses`，`rowcount == 0` → 回滚并抛同样的「用完了」；
6. 插 `redeem_log`；
7. `credit` → 调 `_grant_locked(conn, user_id, value, "redeem", code)`；`member` → 在**同一个连接**里做 Task 1 的 upsert 逻辑（把 `set_tier` 的 SQL 抽成 `_set_tier_locked(conn, ...)`，`set_tier` 与 `redeem` 共用）；
8. `commit`，组装 `RedeemResult`（`message` 分别是「已获得 N 条额外额度」/「会员已延长到 X」）。

`recent_codes(limit)`：`SELECT ... ORDER BY created_at DESC, rowid DESC LIMIT ?`，返回 dict 列表（含 `code`、`kind`、`value`、`max_uses`、`used_count`、`expires_at`、`note`、`created_at`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_credit_store.py -q`
Expected: 18 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/credits.py backend/tests/test_credit_store.py
git commit -m "feat(credits): 兑换码生成、兑换与防重放"
```

---

### Task 4: 额度核心接上存量与全站硬上限

**Files:**
- Modify: `backend/app/quota.py`
- Test: `backend/tests/test_quota.py`（追加）

**Interfaces:**
- Consumes: `app.credits.CREDIT_SCHEMA_SQL`
- Produces:
  - `QuotaReservation.source: str`（`"admin" | "daily" | "credit"`）
  - `QuotaReservation.credit_balance: int | None`
  - `QuotaStatus.credit_balance: int`
  - `QuotaStore.reserve(user_id: int, limit: int, *, is_admin: bool = False, global_limit: int = 0, allow_credits: bool = True, day: str | None = None) -> QuotaReservation | None`

- [ ] **Step 1: 写失败测试**

```python
def credit_store(tmp_path):
    from app.credits import CreditStore

    return CreditStore(str(tmp_path / "quota.sqlite3"), "Asia/Shanghai", max_credits=1000)


def test_credit_used_after_daily_exhausted(tmp_path) -> None:
    store = make_store(tmp_path)
    credit_store(tmp_path).grant_credits(7, 2, "admin_grant")

    first = store.reserve(7, limit=1)
    second = store.reserve(7, limit=1)
    third = store.reserve(7, limit=1)
    fourth = store.reserve(7, limit=1)

    assert first.source == "daily"
    assert second.source == "credit" and second.credit_balance == 1
    assert third.source == "credit" and third.credit_balance == 0
    assert fourth is None


def test_credit_survives_new_day(tmp_path) -> None:
    store = make_store(tmp_path)
    credit_store(tmp_path).grant_credits(7, 1, "admin_grant")

    assert store.reserve(7, limit=1, day="2026-10-30") is not None
    assert store.reserve(7, limit=1, day="2026-10-30") is not None
    # 第二天每日额度刷新，但存量已经在昨天用掉了
    assert store.reserve(7, limit=1, day="2026-10-31") is not None
    assert store.reserve(7, limit=1, day="2026-10-31") is None


def test_global_breaker_blocks_credit_holders_and_rolls_back(tmp_path) -> None:
    store = make_store(tmp_path)
    credits = credit_store(tmp_path)
    credits.grant_credits(7, 5, "admin_grant")

    assert store.reserve(7, limit=10, global_limit=1) is not None

    with pytest.raises(GlobalQuotaExceeded):
        store.reserve(7, limit=10, global_limit=1)

    # 被拒的那次不能偷偷扣掉每日额度
    assert store.status(7, limit=10).used == 1
    assert credits.balance_of(7) == 5


def test_zero_global_limit_means_unlimited(tmp_path) -> None:
    store = make_store(tmp_path)
    for _ in range(5):
        assert store.reserve(7, limit=10, global_limit=0) is not None


def test_refund_returns_credit_and_global(tmp_path) -> None:
    store = make_store(tmp_path)
    credits = credit_store(tmp_path)
    credits.grant_credits(7, 1, "admin_grant")

    assert store.reserve(7, limit=0, global_limit=10, allow_credits=False) is None
    store.reserve(7, limit=1, global_limit=10)
    credit_reservation = store.reserve(7, limit=1, global_limit=10)
    assert credit_reservation.source == "credit"

    store.refund(credit_reservation)

    assert credits.balance_of(7) == 1
    assert store.status(7, limit=1, global_limit=10).global_used == 1


def test_concurrent_reserve_never_overspends_credits(tmp_path) -> None:
    store = make_store(tmp_path)
    credit_store(tmp_path).grant_credits(7, 10, "admin_grant")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: store.reserve(7, limit=5), range(40)))

    assert sum(1 for item in results if item is not None) == 15
    assert credit_store(tmp_path).balance_of(7) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_quota.py -q`
Expected: FAIL，`AttributeError: 'QuotaReservation' object has no attribute 'source'`

- [ ] **Step 3: 实现**

1. `_init_schema` 里 `conn.executescript(CREDIT_SCHEMA_SQL)`（在现有两条建表之后），保证 `reserve` 碰到 `credit_balance` 时表一定存在。
2. `QuotaReservation` 加字段 `source: str = "daily"`、`credit_balance: int | None = None`；`QuotaStatus` 加 `credit_balance: int = 0`。
3. `reserve` 按设计文档 7.1 的伪代码改造：管理员分支 `source="admin"`；`limit <= 0` 且 `day` 保持不变时，**直接返回 None**（`DAILY_QUOTA=0` 仍表示不允许聊天，存量也不能绕过）；每日占不到且 `allow_credits` 为真时，在**同一个事务**里 `UPDATE credit_balance SET balance = balance - 1, updated_at = ? WHERE user_id = ? AND balance > 0 RETURNING balance`，成功则写一条 `-1 "chat"` 流水；**最后**再占全站额度，占不到就 `rollback` 并抛 `GlobalQuotaExceeded`。
4. `refund` 按 `source` 分流：`credit` 加回余额并写 `+1 "chat_refund"` 流水；`daily` 走原来的减法；只要 `global_counted` 为真就退全站。
5. `status` 多查一次 `credit_balance`，填进 `QuotaStatus.credit_balance`（管理员也返回真实值，便于后台展示）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_quota.py -q`
Expected: 原有用例 + 6 个新用例全部 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/quota.py backend/tests/test_quota.py
git commit -m "feat(quota): 每日额度用尽后消耗额外额度，全站额度改为硬上限"
```

---

### Task 5: 会员额度与存量上限进运行时设置

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/runtime_settings.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_runtime_settings.py`（追加）

**Interfaces:**
- Consumes: 现有 `RuntimeSettings` / `RuntimeSettingsStore` / `RuntimeSettingsUpdate`
- Produces:
  - `Settings.member_daily_quota: int = 60`
  - `Settings.credit_max_per_user: int = 10000`
  - `RuntimeSettings.member_daily_quota`、`RuntimeSettings.credit_max_per_user`
  - `_INT_RANGES["member_daily_quota"] = (0, 1000)`、`_INT_RANGES["credit_max_per_user"] = (0, 1_000_000)`
  - `RuntimeSettingsUpdate.member_daily_quota`、`RuntimeSettingsUpdate.credit_max_per_user`

- [ ] **Step 1: 写失败测试**

```python
def test_member_quota_and_credit_cap_have_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMBER_DAILY_QUOTA", "80")
    monkeypatch.setenv("CREDIT_MAX_PER_USER", "500")
    get_settings.cache_clear()
    runtime = _fresh_store(tmp_path).load()

    assert runtime.member_daily_quota == 80
    assert runtime.credit_max_per_user == 500


def test_member_quota_out_of_range_falls_back(tmp_path) -> None:
    store = _fresh_store(tmp_path)
    store.update({"member_daily_quota": 50})
    store.path.write_text('{"member_daily_quota": -1}', encoding="utf-8")

    assert store.load().member_daily_quota == store.defaults.member_daily_quota
```

`_fresh_store` 沿用该文件已有的临时目录构造写法；若文件里已经有同类 helper，直接复用。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runtime_settings.py -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'member_daily_quota'`

- [ ] **Step 3: 实现**

三处各加两个字段，命名必须完全一致：`member_daily_quota`、`credit_max_per_user`。`.env.example` 同步加两行（`MEMBER_DAILY_QUOTA=60`、`CREDIT_MAX_PER_USER=10000`），注释说明「会员每日额度」与「单用户存量上限，防误操作发错位数」。

同时把 Task 1 里 `get_credit_store()` 的 `max_credits=10_000` 改成 `max_credits=settings.credit_max_per_user`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_runtime_settings.py -q`
Expected: passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/app/runtime_settings.py backend/app/schemas.py backend/.env.example backend/tests/test_runtime_settings.py
git commit -m "feat(settings): 新增会员每日额度与单用户存量上限"
```

---

### Task 6: 聊天按等级取限额

**Files:**
- Modify: `backend/app/api/chat.py`
- Test: `backend/tests/test_chat_api.py`（追加）

**Interfaces:**
- Consumes: `CreditStore.tier_of`（Task 1）、`RuntimeSettings.member_daily_quota`（Task 5）
- Produces: 无新符号；`POST /api/chat` 对 `tier == "member"` 使用 `runtime.member_daily_quota`

- [ ] **Step 1: 写失败测试**

```python
def test_member_uses_member_quota(client, ...) -> None:
    # 默认 DAILY_QUOTA=1、MEMBER_DAILY_QUOTA=3（由 fixture 设置）
    from app.credits import get_credit_store

    get_credit_store().set_tier(1, "member", days=7)

    statuses = [_chat_once(client) for _ in range(4)]

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
```

沿用该文件已有的「发一次聊天并取状态码」的辅助函数与 fixture；新增 `MEMBER_DAILY_QUOTA` 环境变量、并在 fixture 结尾 `get_credit_store.cache_clear()`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_chat_api.py -q`
Expected: FAIL，第 2 次请求返回 429（仍在用普通用户额度）

- [ ] **Step 3: 实现**

在 `reserve` 之前取等级：

```python
tier, _expires_at = await asyncio.to_thread(store.tier_of, user.id)
limit = runtime.member_daily_quota if tier == "member" else runtime.daily_quota
```

`store = get_quota_store()` 已存在；`get_credit_store()` 的导入放在模块顶部。两处顺序不能颠倒（先建好 `store` 再取等级），否则异常分支拿不到 `store`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_chat_api.py -q`
Expected: passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/chat.py backend/tests/test_chat_api.py
git commit -m "feat(chat): 会员按更高额度计数"
```

---

### Task 7: 额度接口与响应头带上等级和存量

**Files:**
- Modify: `backend/app/api/quota.py`
- Modify: `backend/app/api/chat.py`（只改响应头）
- Modify: `backend/app/main.py`（`expose_headers` 加两项）
- Test: `backend/tests/test_chat_api.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `QuotaStatus.credit_balance`、Task 1 的 `tier_of`
- Produces:
  - `GET /api/quota` 响应新增 `tier: str`、`tier_expires_at: str | null`、`credit_balance: int`
  - 聊天响应头新增 `X-Quota-Credit`（存量余额）、`X-Quota-Tier`（`user`/`member`/`admin`）

- [ ] **Step 1: 写失败测试**

```python
def test_quota_endpoint_reports_tier_and_credit(client, ...) -> None:
    from app.credits import get_credit_store

    store = get_credit_store()
    store.set_tier(1, "member", days=7)
    store.grant_credits(1, 4, "admin_grant")

    data = client.get("/api/quota", headers=auth_header()).json()

    assert data["tier"] == "member"
    assert data["credit_balance"] == 4
    assert data["tier_expires_at"] is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_chat_api.py -q -k quota_endpoint_reports`
Expected: FAIL，`KeyError: 'tier'`

- [ ] **Step 3: 实现**

`api/quota.py`：多取一次 `tier_of`，`limit` 按等级选（和 Task 6 同样的两行）；响应字典补三个键。管理员返回 `tier="admin"`。

`api/chat.py`：在设置 `X-Quota-*` 的那一段后追加 `headers["X-Quota-Credit"] = str(...)`、`headers["X-Quota-Tier"] = tier`（管理员写 `"admin"`）。

`main.py`：`expose_headers` 列表加 `"X-Quota-Credit"`、`"X-Quota-Tier"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_chat_api.py -q`
Expected: passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/quota.py backend/app/api/chat.py backend/app/main.py backend/tests/test_chat_api.py
git commit -m "feat(api): 额度接口与响应头返回等级和额外额度"
```

---

### Task 8: 用户兑换接口

**Files:**
- Create: `backend/app/api/redeem.py`
- Modify: `backend/app/main.py`（挂路由）
- Modify: `backend/app/schemas.py`（`RedeemRequest`）
- Test: `backend/tests/test_redeem_api.py`

**Interfaces:**
- Consumes: `CreditStore.redeem` / `RedeemError`（Task 3）
- Produces:
  - `POST /api/redeem`，请求体 `{"code": str}`，响应 `{"kind", "message", "tier", "tier_expires_at", "credit_balance"}`
  - `class RedeemRequest(BaseModel): code: str = Field(min_length=4, max_length=64)`

- [ ] **Step 1: 写失败测试**

```python
def test_redeem_credit_code(client, make_token) -> None:
    from app.credits import get_credit_store

    code = get_credit_store().create_codes(kind="credit", value=3)[0]
    response = client.post(
        "/api/redeem",
        json={"code": code},
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "credit"
    assert response.json()["credit_balance"] == 3


def test_redeem_unknown_code_is_400(client) -> None:
    response = client.post("/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"}, headers=auth_header())
    assert response.status_code == 400


def test_redeem_requires_login(client) -> None:
    assert client.post("/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"}).status_code == 401


def test_redeem_failure_is_rate_limited(client, ...) -> None:
    for _ in range(5):
        client.post("/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"}, headers=auth_header())

    response = client.post("/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"}, headers=auth_header())
    assert response.status_code == 429
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_redeem_api.py -q`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 实现**

`redeem.py` 用 `require_user`，`asyncio.to_thread` 包住 `store.redeem`；`RedeemError` → `HTTPException(400, str(exc))`。失败计数放模块级 `dict[int, list[float]]`：`_record_failure` 追加 `time.monotonic()`，`_too_many` 先剔除 600 秒以前的记录再比较 `>= 5`；成功时清空该用户记录。被限流返回 429 + `Retry-After: 600`。

`main.py` 导入并 `include_router(redeem_router, prefix="/api")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_redeem_api.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/redeem.py backend/app/schemas.py backend/app/main.py backend/tests/test_redeem_api.py
git commit -m "feat(api): 用户兑换码接口与防爆破"
```

---

### Task 9: 管理员用户与兑换码接口

**Files:**
- Create: `backend/app/api/admin_users.py`
- Modify: `backend/app/main.py`（挂路由）
- Modify: `backend/app/schemas.py`（三个请求模型）
- Test: `backend/tests/test_admin_api.py`（追加）

**Interfaces:**
- Consumes: `Profile` / `profile_snapshot` / `set_tier` / `grant_credits` / `create_codes` / `recent_codes`（Task 2、3）
- Produces:
  - `CreditStore.known_user_ids() -> list[int]`（`user_profile` 与 `credit_balance` 的并集）
  - `GET /api/admin/users` → `{"users": [{"user_id","tier","tier_expires_at","credit","daily_used","note","qq_uin"}], "day": str, "limits": {"user": int, "member": int}}`
  - `PATCH /api/admin/users/{user_id}` 体 `{"tier"?: "user"|"member", "duration_days"?: int, "note"?: str}`
  - `POST /api/admin/users/{user_id}/credits` 体 `{"delta": int, "reason": str}` → `{"user_id","credit"}`
  - `POST /api/admin/codes` 体 `{"kind","value","count","max_uses","expires_at"?,  "note"?}` → `{"codes": [str]}`
  - `GET /api/admin/codes` → `{"codes": [ {...} ]}`
  - schemas：`UserPatch`、`CreditGrant`、`CodeCreate`

- [ ] **Step 1: 写失败测试**

```python
def test_admin_users_snapshot_has_no_email(client) -> None:
    client.post(
        "/api/admin/users/1/credits",
        json={"delta": 2, "reason": "测试"},
        headers=auth_header(),
    )

    response = client.get("/api/admin/users", headers=auth_header())
    body = response.json()

    assert response.status_code == 200
    assert body["users"][0]["user_id"] == 1
    assert body["users"][0]["credit"] == 2
    assert body["users"][0]["tier"] == "user"
    assert body["users"][0]["daily_used"] == 0
    assert "member" in body["limits"]
    # 后端只在 WordPress 列表里才看得到邮箱，接口不能把它带出来
    assert "email" not in response.text


def test_admin_row_is_listed_like_any_user(client) -> None:
    # 管理员也会出现在档案里，不应因此报错或改变不限量语义
    client.post(
        "/api/admin/users/1/credits",
        json={"delta": 1, "reason": "测试"},
        headers=auth_header(),
    )
    response = client.patch(
        "/api/admin/users/1", json={"note": "站长本人"}, headers=auth_header()
    )

    assert response.status_code == 200
    assert response.json()["tier"] == "user"


def test_patch_member_requires_duration(client) -> None:
    assert client.patch(
        "/api/admin/users/1", json={"tier": "member"}, headers=auth_header()
    ).status_code == 422


def test_patch_member_then_snapshot(client) -> None:
    response = client.patch(
        "/api/admin/users/1",
        json={"tier": "member", "duration_days": 7},
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert response.json()["tier"] == "member"


def test_grant_credits_and_read_back(client) -> None:
    response = client.post(
        "/api/admin/users/1/credits",
        json={"delta": 5, "reason": "手动发放"},
        headers=auth_header(),
    )
    assert response.json()["credit"] == 5


def test_code_creation_and_listing(client) -> None:
    created = client.post(
        "/api/admin/codes",
        json={"kind": "credit", "value": 3, "count": 2},
        headers=auth_header(),
    )
    assert len(created.json()["codes"]) == 2

    listed = client.get("/api/admin/codes", headers=auth_header()).json()["codes"]
    assert len(listed) == 2


def test_normal_user_cannot_touch_admin_users(client) -> None:
    assert client.get("/api/admin/users", headers=auth_header(admin=False)).status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_admin_api.py -q -k admin_users`
Expected: FAIL，404

- [ ] **Step 3: 实现**

全部依赖 `require_admin`。`GET /api/admin/users` 直接调 `profile_snapshot`；由于后端不知道完整用户列表，**返回的是「后端有记录的档案」**，插件侧再和 `WP_User_Query` 合并——所以 `user_ids` 从 `SELECT user_id FROM user_profile` 和 `credit_balance` 的并集里取（在 `CreditStore` 加 `known_user_ids() -> list[int]`）。`limits` 取自 `get_runtime_store().load()`。

`daily_used` 用 `QuotaStore.today()` 查 `daily_usage` 的当日行（`WHERE day = ? AND user_id IN (...)`，一次查完），没有行就是 0；后台页靠它显示「今日已用 / 今日剩余」，所以**必须和 `credit` 一起返回**，否则 Task 12 只能显示「—」。

`PATCH`：`tier == "member"` 且 `duration_days` 为空 → 422，文案「会员必须给出天数」；`note` 最长 200 字。

`POST .../credits`：`delta == 0` → 422；`ValueError` → 422。

`POST /api/admin/codes`：`expires_at` 用 ISO 字符串原样透传；`GET` 限定 50 条。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_admin_api.py -q`
Expected: passed

- [ ] **Step 5: 全量回归 + lint + 提交**

Run: `cd backend && uv run pytest -q && uv run ruff check app tests`
Expected: 全部 passed，ruff 无输出

```bash
git add backend/app/api/admin_users.py backend/app/schemas.py backend/app/main.py backend/tests/test_admin_api.py
git commit -m "feat(admin): 用户档案、等级调整、额度发放与兑换码管理接口"
```

---

### Task 10: 插件注册防护

**Files:**
- Create: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-guard.php`
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`（`require_once` + `L2D_AI_Guard::init()`）

**Interfaces:**
- Consumes: 现有 `L2D_AI_Assistant::api_base()`、`::request_backend()`（需把 `private static function request_backend` 改成 `public static`）
- Produces:
  - `L2D_AI_Guard::init()`：挂 `registration_errors`、`user_register`
  - 选项 `l2d_ai_blocked_email_domains`（字符串，一行一个）、`l2d_ai_register_limit`（数组 `per_ip_hour`/`per_ip_day`/`min_interval_seconds`）、`l2d_ai_block_log`（数组，最多 50 条）
  - 用户元数据 `l2d_ai_reg_ip`、`l2d_ai_reg_at`

- [ ] **Step 1: 实现过滤与记录**

```php
public static function check_registration( $errors, $login, $email ) {
    // 1) 域名黑名单（忽略大小写，包含子域）
    // 2) 每分钟/每小时/每天的 IP 计数（transient）
    // 3) 命中任一条件：$errors->add( 'l2d_ai_blocked', '该邮箱暂不支持注册。' )
    //    并写一条 l2d_ai_block_log（保留最近 50 条）
    return $errors;
}
```

要点（实现者按此写，不要自己发挥）：

- 域名匹配：取 `@` 之后的部分 `strtolower`，对黑名单每个域名判断 `$domain === $blocked || str_ends_with($domain, '.' . $blocked)`。
- IP：`$_SERVER['REMOTE_ADDR']`，`filter_var(..., FILTER_VALIDATE_IP)` 不通过时用 `'unknown'`；IP 为 `unknown` 时只做全天计数。
- 计数键：`l2d_ai_reg_{md5($ip)}_h`（1 小时）、`_d`（1 天），`get_transient` / `set_transient`；命中上限就不加时间、只记录。
- 文案对用户保持中立，不出现「机器人」「垃圾」这类字样。
- `record_registration($user_id)`：写 `l2d_ai_reg_ip`、`l2d_ai_reg_at`（`current_time('mysql')`）。

- [ ] **Step 2: 语法检查**

Run: `php -l wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-guard.php`
Expected: `No syntax errors detected`（本机没有 PHP 就跳过，改为逐行核对括号与分号）

- [ ] **Step 3: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-guard.php wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php
git commit -m "feat(plugin): 注册域名黑名单与 IP 频率限制"
```

---

### Task 11: 注册防护后台页

**Files:**
- Modify: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-guard.php`

**Interfaces:**
- Consumes: Task 10 的三个选项
- Produces: `add_options_page('Live2D 小助手 · 注册防护', ..., 'l2d-ai-guard', ...)`，`admin_post_l2d_ai_save_guard`

- [ ] **Step 1: 实现页面与保存**

- 表单：黑名单文本域（`esc_textarea`）、三个数字输入（`absint`，默认 3 / 10 / 30）、`wp_nonce_field('l2d_ai_guard')`。
- 保存回调校验 `check_admin_referer` + `current_user_can('manage_options')`，`update_option` 后 `wp_safe_redirect` 回本页并带 `l2d_ai_notice=saved`。
- 拦截记录表：`l2d_ai_block_log` 倒序渲染，列「时间 / 邮箱（`esc_html`）/ IP / 原因」；空表显示「暂无拦截记录」。
- 页面底部一行提示：删除用户请到「用户 → 所有用户」。

- [ ] **Step 2: 语法检查**

Run: `php -l wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-guard.php`
Expected: `No syntax errors detected`

- [ ] **Step 3: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-guard.php
git commit -m "feat(plugin): 注册防护设置页与拦截记录"
```

---

### Task 12: 用户与额度后台页

**Files:**
- Create: `wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-users.php`
- Modify: `wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`（`require_once` + `L2D_AI_Users::init()`）

**Interfaces:**
- Consumes: `L2D_AI_Assistant::request_backend('GET', '/api/admin/users')`、`('PATCH', '/api/admin/users/{id}')`、`('POST', '/api/admin/users/{id}/credits')`、`('POST', '/api/admin/codes')`
- Produces: `add_options_page('Live2D 小助手 · 用户与额度', ..., 'l2d-ai-users', ...)`；`admin_post_l2d_ai_set_tier`、`admin_post_l2d_ai_grant_credits`、`admin_post_l2d_ai_create_codes`

- [ ] **Step 1: 实现列表与合并**

- `WP_User_Query` 分页（每页 30），`search` 走 `search_columns => ['user_login','user_email','display_name']`；等级筛选：先把后端快照里的 `member` ID 取出，`include` 它（会员）或 `exclude` 它（普通）。
- 后端快照失败时（`WP_Error`），表格仍然渲染 WordPress 部分，额度列显示「—」，页面顶部给一条 `notice-error`「后端不可达，额度信息暂不可用」。
- 后端没有档案的用户按 `tier=user`、`credit=0` 显示。
- 表格列：ID / 昵称 / 邮箱 / 注册时间（`user_registered`）/ 注册 IP（`l2d_ai_reg_ip`）/ 等级 / 会员到期 / 今日已用 / 今日剩余 / 额外额度 / QQ（固定「—」）/ 操作。
- 「今日已用」直接用 Task 9 返回的 `daily_used`；「今日剩余」= `limits[tier] - daily_used`（管理员显示「不限量」）。

- [ ] **Step 2: 实现操作**

- 等级表单：下拉（普通/会员）+ 天数输入 + 备注；提交到 `admin_post_l2d_ai_set_tier`，`check_admin_referer`，调 `PATCH`。
- 发额度表单：数字（可负）+ 原因；空原因默认填「后台发放」。
- 兑换码区块：类型（额度/会员）、面值、数量（默认 1）、可用次数（默认 1）、有效期（`<input type="date">`，留空为永久）、备注；成功后把生成的码渲染在一个只读 `<textarea>` 里方便复制。
- 最近 50 个码的表格：码 / 类型 / 面值 / 已用 / 上限 / 有效期。

- [ ] **Step 3: 语法检查**

Run: `php -l wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-users.php && php -l wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php`
Expected: 两个文件都输出 `No syntax errors detected`

- [ ] **Step 4: 提交**

```bash
git add wordpress-plugin/wp-live2d-ai/includes/class-l2d-ai-users.php wordpress-plugin/wp-live2d-ai/wp-live2d-ai.php
git commit -m "feat(plugin): 用户与额度后台页、兑换码管理"
```

---

### Task 13: 前端额度条与兑换入口

**Files:**
- Modify: `frontend/src/api/quota.ts`
- Modify: `frontend/src/ui/assistant.ts`
- Modify: `frontend/src/style.css`（兑换按钮的次要样式）

**Interfaces:**
- Consumes: Task 7 的 `GET /api/quota` 新字段与 `X-Quota-Credit` 头
- Produces:
  - `QuotaInfo` 增加 `credit: number`、`tier: 'user' | 'member' | 'admin'`
  - `redeemCode(apiBase: string, auth: AssistantAuthConfig, code: string): Promise<{ ok: boolean; message: string }>`

- [ ] **Step 1: 扩展 `quota.ts`**

`QuotaResponse` 加 `tier?: string`、`credit_balance?: number`；`quotaFromHeaders` 读 `x-quota-credit`、`x-quota-tier`；`fetchQuota` 映射同名字段；默认值 `credit: 0`、`tier: 'user'`。

新增 `redeemCode`：POST `${apiBase}/redeem`，带 `Authorization`，返回后端 `message`；网络错误返回 `{ ok: false, message: '兑换失败，请稍后再试' }`。

- [ ] **Step 2: 扩展 `assistant.ts`**

`renderQuota()` 在非管理员分支改为：

```ts
const parts = [`今日剩余 ${remaining} 条`];
if (quota.credit > 0) parts.push(`额外 ${quota.credit} 条`);
this.quotaEl.textContent = parts.join(' · ');
```

并在额度文本后追加一个 `<button class="l2d-chat__redeem">兑换</button>`（首次渲染时创建，之后只切换文案）；点击时用 `window.prompt` 取码，调用 `redeemCode`，成功后 `await this.refreshQuota()` 并 `window.alert(result.message)`。管理员不显示这个按钮。

`style.css` 加 `.l2d-chat__redeem` 的次要按钮样式（沿用现有 `l2d-chat__quota` 变量，透明背景、下划线）。

- [ ] **Step 3: 构建验证**

Run: `cd frontend && pnpm build`
Expected: `tsc --noEmit` 无报错，`vite build` 输出 `✓ built`

- [ ] **Step 4: 提交**

```bash
git add frontend/src/api/quota.ts frontend/src/ui/assistant.ts frontend/src/style.css
git commit -m "feat(web): 额度条显示额外额度并提供兑换入口"
```

---

### Task 14: 文档批次

**Files:**
- Modify: `docs/architecture.md`（额度分层、身份、注册防护各一节）
- Modify: `docs/tutorial.md`（后台新增两页的用法）
- Modify: `docs/deployment-walkthrough.md`（新表无需迁移、`.env` 两个新键）
- Modify: `backend/README.md`（`app/credits.py` 加进文件地图；测试清单加 `test_credit_store.py` / `test_redeem_api.py`）
- Modify: `README.md`（当前状态勾选）

**Interfaces:** 无代码接口。

- [ ] **Step 1: 写文档**

每处只写「读者需要知道什么」：为什么额外额度不刷新、为什么全站额度是硬上限、会员到期是怎么降级的、注册防护的三个参数含义、后台两页在「设置」下的位置。命令和字段名必须和代码一致。

- [ ] **Step 2: 核对**

Run: `cd backend && uv run pytest -q`
Expected: passed（确认文档里提到的接口与测试目录真实存在，没有过期链接）

- [ ] **Step 3: 提交**

```bash
git add README.md docs/architecture.md docs/tutorial.md docs/deployment-walkthrough.md backend/README.md
git commit -m "docs: 补充身份分级、额外额度与注册防护说明"
```

---

## 部署（不在本计划内，单独征得同意后执行）

服务器上需要：`git pull`、`backend/.env` 增加 `MEMBER_DAILY_QUOTA` 与 `CREDIT_MAX_PER_USER`、重启 `l2d-ai-backend`、重新 `pnpm build` 并上传插件。**新表由代码自动创建，不需要 SQL 脚本，也不需要清空 `quota.sqlite3`。**
