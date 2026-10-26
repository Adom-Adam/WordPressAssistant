"""管理员设置接口测试。

这些接口会改变全站行为，所以重点验证：
- 普通用户进不来（403）；
- 管理员能读写，并且立即生效；
- 非法值被拒绝，不会落盘；
- 只读信息里不会出现 API Key。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "admin-test-secret-" + "z" * 48


def make_token(*, admin: bool = True, sub: str = "1") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "l2d-ai-wordpress",
            "aud": "l2d-ai-backend",
            "sub": sub,
            "name": "管理员",
            "admin": admin,
            "iat": now,
            "exp": now + 900,
        },
        SECRET,
        algorithm="HS256",
    )


def auth_header(*, admin: bool = True) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(admin=admin)}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("DAILY_QUOTA", "20")
    monkeypatch.setenv("GLOBAL_DAILY_QUOTA", "0")
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()


def test_guest_cannot_read_settings(client: TestClient) -> None:
    response = client.get("/api/admin/settings", headers=auth_header(admin=False))

    assert response.status_code == 403


def test_missing_token_is_401(client: TestClient) -> None:
    response = client.get("/api/admin/settings")

    assert response.status_code == 401


def test_admin_can_read_settings(client: TestClient) -> None:
    response = client.get("/api/admin/settings", headers=auth_header())

    assert response.status_code == 200
    data = response.json()
    assert data["settings"]["daily_quota"] == 20
    assert data["defaults"]["daily_quota"] == 20
    # 只暴露“有没有 Key”，绝不返回 Key 内容。
    assert data["backend"]["api_key_configured"] is False
    assert "deepseek_api_key" not in str(data)


def test_admin_update_takes_effect_immediately(client: TestClient) -> None:
    response = client.put(
        "/api/admin/settings",
        json={
            "daily_quota": 7,
            "global_daily_quota": 50,
            "agent_max_model_calls": 3,
            "agent_max_tool_calls": 4,
            "agent_max_seconds": 30,
            "model": "deepseek-chat",
        },
        headers=auth_header(),
    )

    assert response.status_code == 200
    assert response.json()["settings"]["daily_quota"] == 7

    # 不重启，直接再读：值已经变了。
    again = client.get("/api/admin/settings", headers=auth_header())
    assert again.json()["settings"]["global_daily_quota"] == 50

    # health 里的 model 也来自运行时设置。
    health = client.get("/api/health")
    assert health.json()["model"] == "deepseek-chat"


def test_out_of_range_value_is_rejected(client: TestClient) -> None:
    response = client.put(
        "/api/admin/settings",
        json={"daily_quota": 9999},
        headers=auth_header(),
    )

    assert response.status_code == 422
    # 校验失败不应该改掉原值。
    current = client.get("/api/admin/settings", headers=auth_header())
    assert current.json()["settings"]["daily_quota"] == 20


def test_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.put(
        "/api/admin/settings",
        json={"global_quota": 10},
        headers=auth_header(),
    )

    assert response.status_code == 422


def test_empty_update_is_rejected(client: TestClient) -> None:
    response = client.put("/api/admin/settings", json={}, headers=auth_header())

    assert response.status_code == 400
