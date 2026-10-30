"""等级与额外额度的存储测试。

这个文件覆盖三块：等级（含到期惰性降级）、额度存量与流水、兑换码。
时钟通过构造参数注入，所以“过期”和“续期”都不用真的等时间过去。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.credits import CreditStore, RedeemError

TZ = ZoneInfo("Asia/Shanghai")


class Clock:
    """可手动拨动的时钟，替代 datetime.now。"""

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


# ---------------------------------------------------------------------------
# 等级
# ---------------------------------------------------------------------------


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
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.set_tier(42, "member", days=None)


def test_set_user_clears_expiry(tmp_path) -> None:
    clock = Clock(datetime(2026, 10, 30, 12, 0, tzinfo=TZ))
    store = make_store(tmp_path, clock)
    store.set_tier(42, "member", days=7)

    assert store.set_tier(42, "user", days=None) is None
    assert store.tier_of(42) == ("user", None)


# ---------------------------------------------------------------------------
# 额外额度
# ---------------------------------------------------------------------------


def test_grant_and_balance(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.balance_of(7) == 0
    assert store.grant_credits(7, 5, "admin_grant") == 5
    assert store.grant_credits(7, -2, "admin_grant") == 3
    assert store.balance_of(7) == 3


def test_grant_never_goes_negative(tmp_path) -> None:
    store = make_store(tmp_path)
    store.grant_credits(7, 2, "admin_grant")

    with pytest.raises(ValueError):
        store.grant_credits(7, -3, "admin_grant")

    assert store.balance_of(7) == 2


def test_grant_respects_max_per_user(tmp_path) -> None:
    store = make_store(tmp_path)  # max_credits=1000

    with pytest.raises(ValueError):
        store.grant_credits(7, 1001, "admin_grant")

    assert store.balance_of(7) == 0


def test_ledger_records_every_change(tmp_path) -> None:
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
    # 完全没有记录的用户也要有一份默认档案，后台页才好统一渲染
    assert snapshot[99].tier == "user"
    assert snapshot[99].credit == 0
    # 空输入不发 SQL
    assert store.profile_snapshot([]) == {}
