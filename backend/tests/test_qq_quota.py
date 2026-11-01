"""QQ 独立额度存储的测试。

重点：
1. 按人计数与 QQ 全站计数是两个独立的桶；
2. 全站失败必须把按人计数一起回滚（宁可少扣，不能多扣）；
3. 0 与"不限"是两件不同的事（0 是封死，非 0 才是限制）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.qq_quota import QQQuotaStore


def make_store(tmp_path, now=None) -> QQQuotaStore:
    return QQQuotaStore(str(tmp_path / "quota.sqlite3"), now=now)


def test_reserve_counts_per_user(tmp_path) -> None:
    store = make_store(tmp_path)

    first = store.reserve("10001", 2)
    second = store.reserve("10001", 2)
    third = store.reserve("10001", 2)

    assert first.status == "ok"
    assert first.free is not None and first.free.counted is True
    assert first.free.remaining == 1
    assert second.status == "ok"
    assert second.free is not None and second.free.remaining == 0
    assert third.status == "user_exhausted"
    assert third.free is None


def test_zero_limit_blocks_chat(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.reserve("10001", 0).status == "user_exhausted"


def test_limit_none_skips_per_user(tmp_path) -> None:
    store = make_store(tmp_path)
    for _ in range(5):
        assert store.reserve("10001", None).status == "ok"


def test_global_limit_blocks_everyone(tmp_path) -> None:
    store = make_store(tmp_path)

    assert store.reserve("10001", None, global_limit=2).status == "ok"
    assert store.reserve("20002", None, global_limit=2).status == "ok"
    blocked = store.reserve("30003", None, global_limit=2)

    assert blocked.status == "global_exhausted"
    assert blocked.free is None


def test_global_zero_means_unlimited(tmp_path) -> None:
    store = make_store(tmp_path)
    for _ in range(50):
        assert store.reserve("10001", 100, global_limit=0).status == "ok"


def test_global_failure_rolls_back_per_user(tmp_path) -> None:
    store = make_store(tmp_path)

    assert store.reserve("10001", 5, global_limit=1).status == "ok"
    assert store.reserve("10001", 5, global_limit=1).status == "global_exhausted"

    # 第二次的按人计数不该留下痕迹：剩余是 3 而不是 2。
    again = store.reserve("10001", 5, global_limit=0)
    assert again.free is not None and again.free.remaining == 3


def test_refund_returns_both_buckets(tmp_path) -> None:
    store = make_store(tmp_path)
    reserved = store.reserve("10001", 2, global_limit=2)
    assert reserved.free is not None

    store.refund(reserved.free)

    assert store.reserve("10001", 2, global_limit=2).status == "ok"
    assert store.reserve("20002", 2, global_limit=2).status == "ok"


def test_resets_next_day(tmp_path) -> None:
    clock = {"now": datetime(2026, 11, 1, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))}
    store = make_store(tmp_path, now=lambda: clock["now"])

    assert store.reserve("10001", 1).status == "ok"
    assert store.reserve("10001", 1).status == "user_exhausted"

    clock["now"] = datetime(2026, 11, 2, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert store.reserve("10001", 1).status == "ok"
