"""匿名网页聊天：不登录也能聊，但什么都不留。

和 QQ 匿名是同一套原则，只是身份换成“浏览器”。三件事必须同时成立：
1. 不写用户事件、不检索记忆、不写助手事件；
2. 用独立额度（按 IP 哈希 + 全站上限），不占登录用户的份；
3. 文案是梅林的口气，不出现“额度”这类后台用语。

IP 只存哈希：数据库里留下原文等于给自己攒了一份访客日志。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import jwt
import pytest
from fastapi.testclient import TestClient

from app.anonymous_quota import get_anonymous_quota_store
from app.api import chat as chat_module
from app.config import get_settings
from app.credits import get_credit_store
from app.memory import db as memory_db
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "anon-test-secret-" + "a" * 48
PAYLOAD = {"messages": [{"role": "user", "content": "你好"}]}


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


def auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token()}"}


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()
    get_credit_store.cache_clear()
    get_anonymous_quota_store.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("DAILY_QUOTA", "2")
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    # 站点地址留空：本机 .env 里的真实域名不该漏进断言。
    monkeypatch.setenv("WP_SITE_URL", "")
    _clear_caches()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    _clear_caches()


@pytest.fixture
def enable_anonymous(client):
    get_runtime_store().update(
        {
            "anonymous_web_enabled": True,
            "anonymous_web_daily_quota": 2,
            "anonymous_global_daily_quota": 10,
        }
    )


@pytest.fixture(autouse=True)
def fake_stream(monkeypatch):
    async def _stream(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "delta", "content": "你好"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_module, "stream_reply", _stream)


def _from(ip: str) -> dict[str, str]:
    return {"X-Forwarded-For": ip}


def test_anonymous_chat_disabled_returns_403(client: TestClient) -> None:
    response = client.post("/api/chat/anonymous", json=PAYLOAD, headers=_from("1.1.1.1"))

    assert response.status_code == 403


def test_anonymous_chat_streams_without_memory(
    client: TestClient, enable_anonymous
) -> None:
    response = client.post("/api/chat/anonymous", json=PAYLOAD, headers=_from("1.1.1.1"))

    assert response.status_code == 200
    assert "data:" in response.text
    conn = memory_db.connect()
    try:
        # 匿名回合连库都不碰，所以表还没建过；先建表再数行数。
        memory_db.init_schema(conn)
        stats = memory_db.stats(conn)
    finally:
        conn.close()
    assert stats["events"] == 0
    assert stats["memories"] == 0


def test_anonymous_quota_is_per_ip(client: TestClient, enable_anonymous) -> None:
    for _ in range(2):
        assert (
            client.post(
                "/api/chat/anonymous", json=PAYLOAD, headers=_from("1.1.1.1")
            ).status_code
            == 200
        )

    response = client.post(
        "/api/chat/anonymous", json=PAYLOAD, headers=_from("1.1.1.1")
    )

    assert response.status_code == 429
    # 同一个故事的另一个主角不该看到“额度”两个字。
    assert "额度" not in response.json()["detail"]
    # 换个 IP 还有自己的份。
    assert (
        client.post(
            "/api/chat/anonymous", json=PAYLOAD, headers=_from("2.2.2.2")
        ).status_code
        == 200
    )


def test_anonymous_quota_global_limit(client: TestClient, enable_anonymous) -> None:
    get_runtime_store().update({"anonymous_global_daily_quota": 1})

    first = client.post("/api/chat/anonymous", json=PAYLOAD, headers=_from("1.1.1.1"))
    second = client.post("/api/chat/anonymous", json=PAYLOAD, headers=_from("2.2.2.2"))

    assert first.status_code == 200
    assert second.status_code == 429


def test_missing_forwarded_for_uses_client_host(
    client: TestClient, enable_anonymous
) -> None:
    response = client.post("/api/chat/anonymous", json=PAYLOAD)

    assert response.status_code == 200


def test_ip_is_stored_only_as_hash(client: TestClient, enable_anonymous) -> None:
    client.post("/api/chat/anonymous", json=PAYLOAD, headers=_from("203.0.113.7"))

    store = get_anonymous_quota_store()
    conn = store._connection_row(
        "SELECT scope FROM anonymous_usage", ()
    )
    assert conn is not None
    assert conn["scope"].startswith("web:")
    assert "203.0.113.7" not in conn["scope"]


def test_anonymous_reports_remaining_in_headers(
    client: TestClient, enable_anonymous
) -> None:
    response = client.post("/api/chat/anonymous", json=PAYLOAD, headers=_from("1.1.1.1"))

    assert response.headers["x-anonymous-remaining"] == "1"
    assert response.headers["x-anonymous-limit"] == "2"


def test_logged_in_chat_still_works(client: TestClient) -> None:
    response = client.post("/api/chat", json=PAYLOAD, headers=auth_header())

    assert response.status_code == 200
    assert response.headers["x-quota-remaining"] == "1"
