"""后台 QQ 白名单。

未绑定 QQ 默认只能匿名聊天（不记记忆、不涨好感）。管理员把某个号加进
白名单，等于提前替这个人“登记”，让它拿到完整功能；角色还可以指定，
决定它能涨到多少好感。

两个删除动作必须分开：
- 移出名单：她不再被特殊对待，但历史记忆休眠保留；
- 彻底删除：按《个人信息保护法》的要求真的删掉。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store, hash_bind_code
from app.memory import db as memory_db
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "qq-whitelist-secret-" + "k" * 48


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


def _add(client, qq_uin: str, role: str = "special", note: str | None = "老读者"):
    body: dict[str, object] = {"qq_uin": qq_uin, "role": role}
    if note is not None:
        body["note"] = note
    return client.post(
        "/api/admin/qq-whitelist", headers=_headers(99, admin=True), json=body
    )


def _count(sql: str, params: tuple = ()) -> int:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def test_add_and_list_unbound_qq(client) -> None:
    response = _add(client, "123456")

    assert response.status_code == 200
    data = client.get("/api/admin/qq-whitelist", headers=_headers(99, admin=True)).json()
    entry = next(item for item in data["entries"] if item["qq_uin"] == "123456")
    assert entry["role"] == "special"
    assert entry["cap"] == 75
    assert entry["bound_user_id"] is None
    assert entry["note"] == "老读者"


def test_added_role_reaches_persons_table(client) -> None:
    _add(client, "123456", role="exception")

    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT role FROM persons WHERE person_key = 'qq:123456'"
        ).fetchone()
    finally:
        conn.close()

    assert row["role"] == "exception"


def test_reject_already_bound_qq(client) -> None:
    store = get_credit_store()
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("123456", hash_bind_code("1234-5678"))

    response = _add(client, "123456")

    assert response.status_code == 409
    assert _count("SELECT COUNT(*) FROM qq_whitelist") == 0


def test_update_role_and_note(client) -> None:
    _add(client, "123456", role="special")

    response = client.put(
        "/api/admin/qq-whitelist/123456",
        headers=_headers(99, admin=True),
        json={"role": "owner", "note": "站长小号"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "owner"
    assert response.json()["cap"] == 100
    assert response.json()["note"] == "站长小号"


def test_update_rejects_qq_outside_the_list(client) -> None:
    response = client.put(
        "/api/admin/qq-whitelist/999999",
        headers=_headers(99, admin=True),
        json={"role": "owner"},
    )

    # 404 还不够：要能区分“路由不存在”和“这个号不在名单里”。
    assert response.status_code == 404
    assert response.json()["detail"] == "QQ 不在白名单里"


def test_remove_keeps_data_and_purge_deletes_it(client) -> None:
    _add(client, "123456", role="normal")

    # 手动种一条事件，模拟白名单期间聊过的内容。
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO events (occurred_at, person_key, platform, channel_id,
                   scope, audience, audience_key, source, content, created_at)
               VALUES ('2026-11-10T00:00:00+00:00', 'qq:123456', 'qq',
                       'private:123456', 'personal', '["qq:123456"]',
                       'qq:123456', 'user_message', '你好',
                       '2026-11-10T00:00:00+00:00')"""
        )
        conn.commit()
    finally:
        conn.close()

    assert (
        client.delete(
            "/api/admin/qq-whitelist/123456", headers=_headers(99, admin=True)
        ).status_code
        == 204
    )
    assert _count("SELECT COUNT(*) FROM qq_whitelist") == 0
    assert (
        _count("SELECT COUNT(*) FROM events WHERE person_key = 'qq:123456'") == 1
    )

    assert (
        client.delete(
            "/api/admin/qq-whitelist/123456/data", headers=_headers(99, admin=True)
        ).status_code
        == 204
    )
    assert (
        _count("SELECT COUNT(*) FROM events WHERE person_key = 'qq:123456'") == 0
    )
    assert _count("SELECT COUNT(*) FROM persons WHERE person_key = 'qq:123456'") == 0


def test_list_includes_affection(client) -> None:
    _add(client, "123456", role="special")

    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO relationship_state
                   (persona_key, person_key, affection, updated_at)
               VALUES ('merlin', 'qq:123456', 8.5, '2026-11-10T00:00:00+00:00')"""
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/admin/qq-whitelist", headers=_headers(99, admin=True)).json()
    entry = next(item for item in data["entries"] if item["qq_uin"] == "123456")
    assert entry["affection"] == pytest.approx(8.5)
    assert entry["affection_initial"] == 0


def test_invalid_qq_number_is_rejected(client) -> None:
    response = _add(client, "12ab")

    assert response.status_code == 422


def test_non_admin_is_rejected(client) -> None:
    assert client.get(
        "/api/admin/qq-whitelist", headers=_headers(7)
    ).status_code == 403
    assert _add(client, "123456").status_code == 200  # 管理员仍然可用
    assert (
        client.delete(
            "/api/admin/qq-whitelist/123456", headers=_headers(7)
        ).status_code
        == 403
    )
