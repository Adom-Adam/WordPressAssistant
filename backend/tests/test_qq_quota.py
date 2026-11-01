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


# ----------------------------------------------------------------------
# 额度门面：共享 / 独立两种模式
# ----------------------------------------------------------------------
from app.qq_quota import QQQuotaGate
from app.quota import QuotaStore
from app.runtime_settings import RuntimeSettings


def make_runtime(**overrides) -> RuntimeSettings:
    base = dict(
        daily_quota=10,
        member_daily_quota=30,
        global_daily_quota=1000,
        agent_max_model_calls=5,
        agent_max_tool_calls=5,
        agent_max_seconds=30.0,
        model="deepseek-chat",
        credit_max_per_user=100,
        qq_daily_quota=2,
        qq_member_daily_quota=4,
        qq_global_daily_quota=0,
        qq_quota_shared=False,
        qq_unlimited=False,
    )
    base.update(overrides)
    return RuntimeSettings(**base)


def make_gate(tmp_path, runtime):
    quota = QuotaStore(str(tmp_path / "quota.sqlite3"))
    qq = QQQuotaStore(str(tmp_path / "quota.sqlite3"))
    return QQQuotaGate(quota, qq, lambda: runtime), quota, qq


def test_admin_is_not_counted(tmp_path) -> None:
    gate, _, _ = make_gate(tmp_path, make_runtime(qq_global_daily_quota=1))

    status, usage = gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=True)

    assert status == "ok"
    assert usage.kind == "admin"
    assert usage.web is None and usage.qq is None


def test_independent_mode_uses_qq_quota(tmp_path) -> None:
    gate, quota, _ = make_gate(tmp_path, make_runtime())

    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert (
        gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0]
        == "user_exhausted"
    )
    # 网页额度完全没动
    assert quota.usage_snapshot([7])[7] == 0


def test_independent_member_uses_member_limit(tmp_path) -> None:
    gate, _, _ = make_gate(
        tmp_path, make_runtime(qq_daily_quota=1, qq_member_daily_quota=3)
    )

    for _ in range(3):
        assert gate.reserve(qq_uin="10001", user_id=7, tier="member", is_admin=False)[0] == "ok"
    assert (
        gate.reserve(qq_uin="10001", user_id=7, tier="member", is_admin=False)[0]
        == "user_exhausted"
    )


def test_unlimited_skips_per_user_but_keeps_global(tmp_path) -> None:
    gate, _, _ = make_gate(
        tmp_path,
        make_runtime(qq_daily_quota=1, qq_unlimited=True, qq_global_daily_quota=2),
    )

    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert gate.reserve(qq_uin="10002", user_id=8, tier="user", is_admin=False)[0] == "ok"
    assert (
        gate.reserve(qq_uin="10003", user_id=9, tier="user", is_admin=False)[0]
        == "global_exhausted"
    )


def test_shared_mode_consumes_web_quota_and_ignores_qq_per_user(tmp_path) -> None:
    gate, quota, _ = make_gate(
        tmp_path,
        make_runtime(qq_quota_shared=True, qq_daily_quota=1, qq_global_daily_quota=0),
    )

    status, usage = gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)
    assert status == "ok"
    assert usage.kind == "shared" and usage.web is not None
    assert quota.usage_snapshot([7])[7] == 1
    # 按人额度（qq_daily_quota=1）在共享模式下不生效
    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"


def test_shared_mode_qq_global_failure_refunds_web(tmp_path) -> None:
    gate, quota, _ = make_gate(
        tmp_path,
        make_runtime(
            qq_quota_shared=True, global_daily_quota=1000, qq_global_daily_quota=1
        ),
    )

    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
    assert quota.usage_snapshot([7])[7] == 1
    assert (
        gate.reserve(qq_uin="20002", user_id=7, tier="user", is_admin=False)[0]
        == "global_exhausted"
    )
    # 网页计数被回滚到只剩第一次那一条，而不是 2
    assert quota.usage_snapshot([7])[7] == 1


def test_refund_restores_both_modes(tmp_path) -> None:
    gate, _, _ = make_gate(tmp_path, make_runtime(qq_daily_quota=1))

    status, usage = gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)
    assert status == "ok"

    gate.refund(usage)

    assert gate.reserve(qq_uin="10001", user_id=7, tier="user", is_admin=False)[0] == "ok"
