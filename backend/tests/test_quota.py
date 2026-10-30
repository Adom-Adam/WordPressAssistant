"""每日额度的单元测试。

重点验证三件事：
1. 次数到上限就拒绝；
2. 并发请求不会多占；
3. 退款和“第二天重置”符合预期。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.quota import GlobalQuotaExceeded, QuotaStore


def make_store(tmp_path) -> QuotaStore:
    # tmp_path 是 pytest 提供的临时目录，每个测试独立，互不污染。
    return QuotaStore(str(tmp_path / "quota.sqlite3"), "Asia/Shanghai")


def test_reserve_until_limit_then_reject(tmp_path) -> None:
    store = make_store(tmp_path)

    first = store.reserve(7, limit=2)
    second = store.reserve(7, limit=2)
    third = store.reserve(7, limit=2)

    assert first is not None and first.remaining == 1
    assert second is not None and second.remaining == 0
    assert third is None


def test_users_are_independent(tmp_path) -> None:
    store = make_store(tmp_path)

    assert store.reserve(1, limit=1) is not None
    assert store.reserve(1, limit=1) is None
    # 用户 2 不受用户 1 的影响。
    assert store.reserve(2, limit=1) is not None


def test_new_day_resets_quota(tmp_path) -> None:
    store = make_store(tmp_path)

    assert store.reserve(7, limit=1, day="2026-10-26") is not None
    assert store.reserve(7, limit=1, day="2026-10-26") is None
    assert store.reserve(7, limit=1, day="2026-10-27") is not None


def test_admin_is_unlimited_and_not_counted(tmp_path) -> None:
    store = make_store(tmp_path)

    for _ in range(10):
        reservation = store.reserve(1, limit=0, is_admin=True)
        assert reservation is not None
        assert reservation.counted is False
        assert reservation.remaining is None

    # 管理员不应该在数据库里留下计数行。
    conn = sqlite3.connect(store.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_refund_returns_a_slot(tmp_path) -> None:
    store = make_store(tmp_path)

    reservation = store.reserve(7, limit=1)
    assert reservation is not None
    assert store.reserve(7, limit=1) is None

    store.refund(reservation)
    again = store.reserve(7, limit=1)

    assert again is not None
    assert again.remaining == 0


def test_refund_is_safe_to_call_twice(tmp_path) -> None:
    store = make_store(tmp_path)

    reservation = store.reserve(7, limit=1)
    assert reservation is not None

    store.refund(reservation)
    store.refund(reservation)
    store.refund(None)

    # 多退一次不能把计数减成负数，也不能凭空增加额度。
    assert store.reserve(7, limit=1) is not None
    assert store.reserve(7, limit=1) is None


def test_concurrent_reservations_do_not_overshoot(tmp_path) -> None:
    store = make_store(tmp_path)
    attempts = 40
    limit = 5

    # 用线程池同时发起 40 次占位，模拟多个用户/标签页同时点击发送。
    # 如果实现不是原子的，结果会超过 5。
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: store.reserve(99, limit=limit), range(attempts)))

    granted = [item for item in results if item is not None]
    assert len(granted) == limit
    # 剩余次数从 4 递减到 0，不能出现重复或负数。
    assert sorted(item.remaining for item in granted) == [0, 1, 2, 3, 4]


def test_seconds_until_reset_is_within_one_day(tmp_path) -> None:
    store = make_store(tmp_path)
    seconds = store.seconds_until_reset()

    assert 1 <= seconds <= 86_400


def test_global_quota_blocks_everyone_after_limit(tmp_path) -> None:
    store = make_store(tmp_path)

    first = store.reserve(1, limit=10, global_limit=2)
    second = store.reserve(2, limit=10, global_limit=2)

    assert first is not None and first.global_remaining == 1
    assert second is not None and second.global_remaining == 0

    with pytest.raises(GlobalQuotaExceeded):
        store.reserve(3, limit=10, global_limit=2)

    # 全站额度失败时，个人计数必须一起回滚，不能白扣用户 3 的次数。
    status = store.status(3, limit=10, global_limit=2)
    assert status.used == 0
    assert status.remaining == 10


def test_refund_returns_both_personal_and_global(tmp_path) -> None:
    store = make_store(tmp_path)

    reservation = store.reserve(7, limit=1, global_limit=2)
    assert reservation is not None
    assert store.status(7, 1, global_limit=2).remaining == 0

    store.refund(reservation)

    status = store.status(7, 1, global_limit=2)
    assert status.used == 0
    assert status.remaining == 1
    assert status.global_used == 0
    assert status.global_remaining == 2


def test_admin_does_not_consume_global_quota(tmp_path) -> None:
    store = make_store(tmp_path)

    for _ in range(5):
        reservation = store.reserve(1, limit=0, is_admin=True, global_limit=1)
        assert reservation is not None
        assert reservation.global_counted is False

    # 普通用户仍能拿到全站唯一的那一个名额。
    assert store.reserve(2, limit=1, global_limit=1) is not None


def test_status_does_not_consume_quota(tmp_path) -> None:
    store = make_store(tmp_path)
    reservation = store.reserve(5, limit=3, global_limit=10)
    assert reservation is not None

    for _ in range(3):
        status = store.status(5, limit=3, global_limit=10)
        assert status.used == 1
        assert status.remaining == 2
        assert status.global_used == 1
        assert status.global_remaining == 9


def test_admin_status_is_unlimited(tmp_path) -> None:
    store = make_store(tmp_path)

    status = store.status(1, limit=0, is_admin=True)

    assert status.unlimited is True
    assert status.remaining is None
    assert status.used is None


# ---------------------------------------------------------------------------
# 额外额度与全站硬上限
# ---------------------------------------------------------------------------


def credit_store(tmp_path):
    """和 QuotaStore 共用同一个 SQLite 文件，模拟真实部署。"""
    from app.credits import CreditStore

    return CreditStore(str(tmp_path / "quota.sqlite3"), "Asia/Shanghai", max_credits=1000)


def test_credit_used_after_daily_exhausted(tmp_path) -> None:
    store = make_store(tmp_path)
    credit_store(tmp_path).grant_credits(7, 2, "admin_grant")

    first = store.reserve(7, limit=1)
    second = store.reserve(7, limit=1)
    third = store.reserve(7, limit=1)
    fourth = store.reserve(7, limit=1)

    assert first is not None and first.source == "daily"
    assert second is not None and second.source == "credit" and second.credit_balance == 1
    assert third is not None and third.source == "credit" and third.credit_balance == 0
    assert fourth is None


def test_credit_survives_new_day(tmp_path) -> None:
    store = make_store(tmp_path)
    credit_store(tmp_path).grant_credits(7, 1, "admin_grant")

    assert store.reserve(7, limit=1, day="2026-10-30") is not None
    assert store.reserve(7, limit=1, day="2026-10-30") is not None
    # 第二天每日额度刷新，但昨天已经花掉的存量不会回来
    assert store.reserve(7, limit=1, day="2026-10-31") is not None
    assert store.reserve(7, limit=1, day="2026-10-31") is None


def test_global_breaker_blocks_credit_holders_and_rolls_back(tmp_path) -> None:
    store = make_store(tmp_path)
    credits = credit_store(tmp_path)
    credits.grant_credits(7, 5, "admin_grant")

    assert store.reserve(7, limit=10, global_limit=1) is not None

    with pytest.raises(GlobalQuotaExceeded):
        store.reserve(7, limit=10, global_limit=1)

    # 被拒的那次不能偷偷扣掉每日额度，也不能动存量
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

    # 每日额度为 0 时，存量也不能绕过（DAILY_QUOTA=0 表示不允许聊天）
    assert store.reserve(7, limit=0, global_limit=10, allow_credits=False) is None

    store.reserve(7, limit=1, global_limit=10)
    reservation = store.reserve(7, limit=1, global_limit=10)
    assert reservation is not None and reservation.source == "credit"

    store.refund(reservation)

    assert credits.balance_of(7) == 1
    assert store.status(7, limit=1, global_limit=10).global_used == 1


def test_concurrent_reserve_never_overspends_credits(tmp_path) -> None:
    store = make_store(tmp_path)
    credit_store(tmp_path).grant_credits(7, 10, "admin_grant")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: store.reserve(7, limit=5), range(40)))

    assert sum(1 for item in results if item is not None) == 15
    assert credit_store(tmp_path).balance_of(7) == 0
