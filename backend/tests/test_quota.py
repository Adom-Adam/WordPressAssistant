"""每日额度的单元测试。

重点验证三件事：
1. 次数到上限就拒绝；
2. 并发请求不会多占；
3. 退款和“第二天重置”符合预期。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.quota import QuotaStore


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
