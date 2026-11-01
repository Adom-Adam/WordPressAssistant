"""绑定的 HTTP 接口。

绑定的第一步是 WordPress 把用户看到的绑定码同步给后端。这里验证四件事：
- 合法绑定码能写入，GET 能读到当前 QQ；
- 格式不对的绑定码直接被拒（400），不会被存成半截；
- 管理员身份来自 JWT，而不是请求体（否则谁都能给自己开无限额度）；
- 管理员可以强制解绑，普通用户不能。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store, hash_bind_code
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "bind-test-secret-" + "z" * 48


def _token(user_id: int, *, admin: bool = False) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "l2d-ai-wordpress",
            "aud": "l2d-ai-backend",
            "sub": str(user_id),
            "name": "测试用户",
            "admin": admin,
            "iat": now,
            "exp": now + 900,
        },
        SECRET,
        algorithm="HS256",
    )


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
    get_credit_store.cache_clear()


def test_put_bind_code_then_get_and_delete(client):
    token = _token(7)
    response = client.put(
        "/api/me/bind-code",
        json={"code": "1234-5678"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204
    assert client.get(
        "/api/me/bind", headers={"Authorization": f"Bearer {token}"}
    ).json() == {"qq_uin": None}

    # 绑定后 GET 反映出来
    get_credit_store().bind_qq("10001", hash_bind_code("1234-5678"))
    assert client.get(
        "/api/me/bind", headers={"Authorization": f"Bearer {token}"}
    ).json() == {"qq_uin": "10001"}

    assert (
        client.delete(
            "/api/me/bind", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 204
    )
    assert get_credit_store().qq_uin_of(7) is None


def test_put_bind_code_rejects_bad_format(client):
    response = client.put(
        "/api/me/bind-code",
        json={"code": "abc"},
        headers={"Authorization": f"Bearer {_token(7)}"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "绑定码应为 8 位数字"


def test_is_admin_comes_from_token(client):
    client.put(
        "/api/me/bind-code",
        json={"code": "1234-5678"},
        headers={"Authorization": f"Bearer {_token(7, admin=True)}"},
    )
    get_credit_store().bind_qq("10001", hash_bind_code("1234-5678"))
    assert get_credit_store().binding_of_qq("10001").is_admin is True


def test_admin_can_force_unbind(client):
    store = get_credit_store()
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    response = client.delete(
        "/api/admin/users/7/bind",
        headers={"Authorization": f"Bearer {_token(1, admin=True)}"},
    )
    assert response.status_code == 204
    assert store.qq_uin_of(7) is None


def test_non_admin_cannot_force_unbind(client):
    response = client.delete(
        "/api/admin/users/7/bind",
        headers={"Authorization": f"Bearer {_token(7, admin=False)}"},
    )
    assert response.status_code == 403
