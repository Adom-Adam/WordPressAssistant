"""注销后重新注册时的额度继承。

覆盖两件事：
- 存储层：每日已用、额外额度、会员、流水、兑换记录怎么合并，以及重复请求；
- 接口层：旧账号 ID 必须来自签名过的 token，普通 token 不能调用。
"""

from __future__ import annotations

import sqlite3
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import CreditStore, TransferResult, get_credit_store
from app.quota import QuotaStore, get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "identity-test-secret-" + "k" * 40


def make_stores(tmp_path) -> tuple[QuotaStore, CreditStore]:
    # 两个 store 共用同一个 SQLite 文件：额度扣减和等级/存量本来就在一起。
    db_path = str(tmp_path / "quota.sqlite3")
    return QuotaStore(db_path), CreditStore(db_path, max_credits=1000)


def _seed_source(quota: QuotaStore, credits: CreditStore) -> None:
    for _ in range(5):
        assert quota.reserve(11, 100) is not None
    credits.grant_credits(11, 7, "admin_grant")
    credits.set_tier(11, "member", days=30)


def _seed_target(quota: QuotaStore, credits: CreditStore) -> None:
    for _ in range(2):
        assert quota.reserve(22, 100) is not None
    credits.grant_credits(22, 3, "admin_grant")


def test_inherit_moves_quota_and_keeps_the_higher_daily_usage(tmp_path) -> None:
    quota, credits = make_stores(tmp_path)
    _seed_source(quota, credits)
    _seed_target(quota, credits)

    with sqlite3.connect(tmp_path / "quota.sqlite3") as conn:
        conn.execute(
            "INSERT INTO redeem_log (code, user_id, used_at) VALUES (?, ?, ?)",
            ("CODE-1", 11, "2026-11-02T12:00:00+08:00"),
        )

    result = credits.inherit_identity(22, [11])

    assert result.transferred == (11,)
    assert result.already == ()
    assert result.conflicts == ()

    # 每日已用取较大值，不是相加：5 和 2 合并后仍是 5。
    assert quota.usage_snapshot([11, 22]) == {11: 0, 22: 5}
    # 额外额度是存量，两边相加。
    assert credits.balance_of(22) == 10
    assert credits.balance_of(11) == 0
    # 会员等级和到期时间跟着走。
    assert credits.tier_of(22)[0] == "member"

    with sqlite3.connect(tmp_path / "quota.sqlite3") as conn:
        profile_count = conn.execute(
            "SELECT COUNT(*) FROM user_profile WHERE user_id = 11"
        ).fetchone()[0]
        ledger_owner = conn.execute(
            "SELECT COUNT(*) FROM credit_ledger WHERE user_id = 22"
        ).fetchone()[0]
        redeem_owner = conn.execute(
            "SELECT user_id FROM redeem_log WHERE code = 'CODE-1'"
        ).fetchone()[0]

    assert profile_count == 0
    assert ledger_owner >= 1
    assert redeem_owner == 22


def test_inherit_is_idempotent(tmp_path) -> None:
    quota, credits = make_stores(tmp_path)
    _seed_source(quota, credits)

    credits.inherit_identity(22, [11])
    balance_after_first = credits.balance_of(22)

    second = credits.inherit_identity(22, [11])

    assert second.transferred == ()
    assert second.already == (11,)
    assert credits.balance_of(22) == balance_after_first
    assert quota.usage_snapshot([22]) == {22: 5}


def test_inherit_refuses_to_move_from_an_already_claimed_account(tmp_path) -> None:
    quota, credits = make_stores(tmp_path)
    _seed_source(quota, credits)
    credits.inherit_identity(22, [11])
    balance = credits.balance_of(22)

    # 另一个新账号拿着同一个旧 ID 来认领：不能把 22 的额度再搬走。
    result = credits.inherit_identity(33, [11])

    assert result.transferred == ()
    assert result.already == ()
    assert result.conflicts == (11,)
    assert credits.balance_of(22) == balance
    assert credits.balance_of(33) == 0


def test_inherit_ignores_self_and_invalid_ids(tmp_path) -> None:
    _, credits = make_stores(tmp_path)

    result = credits.inherit_identity(22, [22, 0, -1, True, "abc"])

    assert result == TransferResult((), (), ())


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


def make_token(user_id: int, *, transfer_from: object = None, admin: bool = False) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": "l2d-ai-wordpress",
        "aud": "l2d-ai-backend",
        "sub": str(user_id),
        "name": "测试用户",
        "admin": admin,
        "iat": now,
        "exp": now + 900,
    }
    if transfer_from is not None:
        payload["transfer_from"] = transfer_from
    return jwt.encode(payload, SECRET, algorithm="HS256")


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
    get_quota_store.cache_clear()
    get_credit_store.cache_clear()
    get_runtime_store.cache_clear()


def test_api_inherits_when_token_has_transfer_claim(client) -> None:
    quota = get_quota_store()
    credits = get_credit_store()
    _seed_source(quota, credits)

    response = client.post(
        "/api/me/inherit-quota",
        headers={"Authorization": f"Bearer {make_token(22, transfer_from=[11])}"},
    )

    assert response.status_code == 200
    assert response.json()["transferred"] == [11]
    assert credits.balance_of(22) == 7


def test_api_initializes_quota_tables_when_inherit_is_the_first_request(
    client, tmp_path
) -> None:
    # 不调用 get_quota_store/get_credit_store：模拟进程第一次请求就是继承接口，
    # 这时 daily_usage 还没建表。接口必须先让 QuotaStore 初始化。
    quota, credits = make_stores(tmp_path)
    _seed_source(quota, credits)

    response = client.post(
        "/api/me/inherit-quota",
        headers={"Authorization": f"Bearer {make_token(22, transfer_from=[11])}"},
    )

    assert response.status_code == 200
    assert response.json()["transferred"] == [11]


def test_api_rejects_token_without_claim(client) -> None:
    response = client.post(
        "/api/me/inherit-quota",
        headers={"Authorization": f"Bearer {make_token(22)}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "没有可继承的账号记录"


def test_api_ignores_transfer_from_in_request_body(client) -> None:
    # 请求体里塞旧 ID 不算数：只有签名过的 claim 才能决定搬谁的额度。
    response = client.post(
        "/api/me/inherit-quota",
        json={"transfer_from": [11]},
        headers={"Authorization": f"Bearer {make_token(22)}"},
    )

    assert response.status_code == 403
