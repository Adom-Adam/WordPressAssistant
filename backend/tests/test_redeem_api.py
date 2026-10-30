"""兑换码接口测试。

重点：
- 登录用户能兑换，成功后余额立即变化；
- 无效兑换码返回 400，而不是 500；
- 没登录返回 401；
- 连续猜错会被限流（429），避免拿这个接口爆破兑换码。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "redeem-test-secret-" + "r" * 48


def make_token(*, sub: str = "42") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "l2d-ai-wordpress",
            "aud": "l2d-ai-backend",
            "sub": sub,
            "name": "测试用户",
            "admin": False,
            "iat": now,
            "exp": now + 900,
        },
        SECRET,
        algorithm="HS256",
    )


def auth_header(*, sub: str = "42") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=sub)}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("DAILY_QUOTA", "2")
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()
    get_credit_store.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()
    get_credit_store.cache_clear()


def test_redeem_credit_code(client: TestClient) -> None:
    code = get_credit_store().create_codes(kind="credit", value=3)[0]

    response = client.post(
        "/api/redeem",
        json={"code": code},
        headers=auth_header(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "credit"
    assert body["credit_balance"] == 3
    assert body["tier"] == "user"


def test_redeem_unknown_code_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/redeem",
        json={"code": "AAAA-BBBB-CCCC-DDDD"},
        headers=auth_header(sub="43"),
    )

    assert response.status_code == 400
    assert response.json()["detail"]


def test_redeem_requires_login(client: TestClient) -> None:
    response = client.post("/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"})

    assert response.status_code == 401


def test_redeem_failure_is_rate_limited(client: TestClient) -> None:
    # 用一个独立用户，避免和其他用例的失败计数串味
    headers = auth_header(sub="7")

    for _ in range(5):
        response = client.post(
            "/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"}, headers=headers
        )
        assert response.status_code == 400

    response = client.post(
        "/api/redeem", json={"code": "AAAA-BBBB-CCCC-DDDD"}, headers=headers
    )

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
