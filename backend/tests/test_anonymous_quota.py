"""匿名额度：和登录用户的额度完全分开。

匿名聊天没有账号，所以额度按“IP 哈希 / QQ 号 + 天”记，另有一份全站
总量兜底。两条规则的边界值最容易写错：

- 个人上限 0 = 关闭这条通道（不是“不限”）；
- 全站上限 0 = 不限全站。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.anonymous_quota import AnonymousQuotaStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def make_store(tmp_path) -> AnonymousQuotaStore:
    return AnonymousQuotaStore(
        str(tmp_path / "anonymous.sqlite3"),
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


def test_global_exhaustion_does_not_consume_personal_quota(tmp_path):
    store = make_store(tmp_path)
    store.reserve("web:a", 10, global_limit=1)
    store.reserve("web:b", 10, global_limit=1)  # 全站用满

    store.reserve("web:a", 10, global_limit=1)

    # 被全站上限拒掉的那次不能算进个人头上。
    assert store.used("web:a") == 1


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
