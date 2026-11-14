"""后台关系角色名单。

角色只影响好感上限和成长速度，不决定“能不能聊天”。后台要做三件事：
列出当前角色、改一个人的角色、把一个人恢复成 normal。

一个容易写错的点：管理员自己必须是 owner。否则站长一进后台就看到
自己“上限 60”，调完参数却对自己不生效。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store
from app.memory import db as memory_db
from app.memory import identity
from app.quota import get_quota_store
from app.relationship.state import role_for
from app.runtime_settings import get_runtime_store

SECRET = "roles-secret-" + "k" * 48


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


def _headers(user_id: int = 99, *, admin: bool = False) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user_id, admin=admin)}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_credit_store.cache_clear()
    get_runtime_store.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    get_credit_store.cache_clear()
    get_runtime_store.cache_clear()


def _role_of(person_key: str) -> str:
    conn = memory_db.connect()
    try:
        return role_for(conn, person_key)
    finally:
        conn.close()


def test_list_shows_caps_and_auto_owns_admin(client) -> None:
    response = client.get("/api/admin/roles", headers=_headers(99, admin=True))

    assert response.status_code == 200
    data = response.json()
    assert data["role_caps"] == {
        "owner": 100,
        "exception": 85,
        "special": 75,
        "normal": 60,
    }
    me = next(item for item in data["roles"] if item["wp_user_id"] == 99)
    assert me["role"] == "owner"


def test_set_and_reset_role(client) -> None:
    response = client.put(
        "/api/admin/roles",
        headers=_headers(99, admin=True),
        json={"wp_user_id": 7, "role": "exception"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "exception"
    assert response.json()["cap"] == 85
    assert _role_of("wp:7") == "exception"

    response = client.delete("/api/admin/roles/7", headers=_headers(99, admin=True))

    assert response.status_code == 204
    assert _role_of("wp:7") == "normal"


def test_special_keeps_its_own_cap(client) -> None:
    response = client.put(
        "/api/admin/roles",
        headers=_headers(99, admin=True),
        json={"wp_user_id": 7, "role": "special"},
    )

    assert response.json()["cap"] == 75


def test_search_filters_by_display_name(client) -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        identity.touch_person(conn, "wp:7", display_name="阿东", wp_user_id=7)
    finally:
        conn.close()

    data = client.get(
        "/api/admin/roles?query=阿东", headers=_headers(99, admin=True)
    ).json()

    assert [item["wp_user_id"] for item in data["roles"]] == [7]


def test_list_includes_affection_and_role_initials(client) -> None:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        identity.touch_person(conn, "wp:7", display_name="阿东", wp_user_id=7)
        conn.execute(
            """INSERT INTO relationship_state
                   (persona_key, person_key, affection, updated_at)
               VALUES ('merlin', 'wp:7', 12.5, '2026-11-10T00:00:00+00:00')"""
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/admin/roles", headers=_headers(99, admin=True)).json()

    assert data["role_initials"] == {
        "owner": 60,
        "exception": 0,
        "special": 0,
        "normal": 0,
    }
    entry = next(item for item in data["roles"] if item["wp_user_id"] == 7)
    assert entry["affection"] == pytest.approx(12.5)
    assert entry["affection_initial"] == 0
    # 管理员自己只有 persons 行、还没有关系状态：好感要给“初始”，
    # 否则后台看起来像好感被清零了。
    me = next(item for item in data["roles"] if item["wp_user_id"] == 99)
    assert me["affection"] is None
    assert me["affection_initial"] == 60


def test_non_admin_is_rejected(client) -> None:
    assert client.get("/api/admin/roles", headers=_headers(7)).status_code == 403
    assert (
        client.put(
            "/api/admin/roles",
            headers=_headers(7),
            json={"wp_user_id": 7, "role": "owner"},
        ).status_code
        == 403
    )
