"""管理员群策略接口。

只允许操作白名单里的群；没写过策略的群返回由 `QQ_TRIGGER` 推导的
默认值，写过的返回 `source='db'`。非法值一律 422，不落盘。
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


def _clear():
    for fn in (
        get_settings.cache_clear,
        get_quota_store.cache_clear,
        get_runtime_store.cache_clear,
        get_credit_store.cache_clear,
    ):
        fn()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_ALLOWED_GROUPS", "888,999")
    monkeypatch.setenv("QQ_TRIGGER", "at")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    _clear()
    from app.main import app

    with TestClient(app) as c:
        yield c
    _clear()


def test_lists_whitelisted_groups_with_defaults(client):
    response = client.get("/api/admin/qq-groups", headers=auth_header())
    assert response.status_code == 200
    groups = {g["group_id"]: g for g in response.json()["groups"]}
    assert set(groups) == {888, 999}
    assert groups[888]["trigger"] == "mention"
    assert groups[888]["source"] == "default"


def test_put_then_get_shows_db_source(client):
    body = {"trigger": "all", "reply_policy": "silent", "proactive": True}
    response = client.put("/api/admin/qq-groups/888", json=body, headers=auth_header())
    assert response.status_code == 200
    assert response.json()["source"] == "db"

    listed = client.get("/api/admin/qq-groups", headers=auth_header()).json()["groups"]
    groups = {g["group_id"]: g for g in listed}
    assert groups[888] == {
        "group_id": 888,
        "trigger": "all",
        "reply_policy": "silent",
        "proactive": True,
        "source": "db",
    }


def test_put_rejects_group_outside_whitelist(client):
    body = {"trigger": "all", "reply_policy": "always", "proactive": False}
    response = client.put("/api/admin/qq-groups/777", json=body, headers=auth_header())
    assert response.status_code == 422


def test_put_rejects_unknown_trigger(client):
    body = {"trigger": "sometimes", "reply_policy": "always", "proactive": False}
    response = client.put("/api/admin/qq-groups/888", json=body, headers=auth_header())
    assert response.status_code == 422


def test_non_admin_cannot_read(client):
    response = client.get("/api/admin/qq-groups", headers=auth_header(admin=False))
    assert response.status_code == 403


def test_add_and_remove_group_from_admin(client):
    response = client.post(
        "/api/admin/qq-groups",
        headers=auth_header(),
        json={"group_id": 555},
    )
    assert response.status_code == 200

    groups = client.get("/api/admin/qq-groups", headers=auth_header()).json()["groups"]
    assert any(item["group_id"] == 555 for item in groups)

    assert (
        client.delete("/api/admin/qq-groups/555", headers=auth_header()).status_code
        == 204
    )
    groups = client.get("/api/admin/qq-groups", headers=auth_header()).json()["groups"]
    assert all(item["group_id"] != 555 for item in groups)


def test_policy_check_follows_runtime_allowlist(client):
    """策略接口的“在不在名单里”看运行时名单，不看环境变量。"""
    body = {"trigger": "mention", "reply_policy": "always", "proactive": False}
    assert (
        client.put("/api/admin/qq-groups/777", json=body, headers=auth_header()).status_code
        == 422
    )

    client.post("/api/admin/qq-groups", json={"group_id": 777}, headers=auth_header())
    assert (
        client.put("/api/admin/qq-groups/777", json=body, headers=auth_header()).status_code
        == 200
    )

    client.delete("/api/admin/qq-groups/777", headers=auth_header())
    assert (
        client.put("/api/admin/qq-groups/777", json=body, headers=auth_header()).status_code
        == 422
    )


def test_add_group_rejects_zero(client):
    response = client.post(
        "/api/admin/qq-groups", headers=auth_header(), json={"group_id": 0}
    )
    assert response.status_code == 422
