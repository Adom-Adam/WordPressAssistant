"""对话接口的端到端测试（不调用真实模型）。

用 FastAPI 的 TestClient 直接发 HTTP 请求，覆盖：
- 没有 token → 401；
- 有效 token → 200 + SSE；
- 过期 token → 401；
- 额度用完 → 429；
- 管理员 → 不限量；
- 没配密钥 → 503；
- 模型还没输出就失败 → 额度退回。

为了不花钱、不依赖网络，测试里把 stream_reply 换成了假的异步生成器。
这不会影响被测代码：认证、额度、SSE 包装都是真实执行的。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import chat as chat_module
from app.config import get_settings
from app.credits import get_credit_store
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "api-test-secret-" + "q" * 48


def make_token(
    *,
    sub: str = "42",
    admin: bool = False,
    aud: str = "l2d-ai-backend",
    exp_delta: int = 900,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "l2d-ai-wordpress",
            "aud": aud,
            "sub": sub,
            "name": "测试用户",
            "admin": admin,
            "iat": now,
            "exp": now + exp_delta,
        },
        SECRET,
        algorithm="HS256",
    )


def auth_header(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or make_token()}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """创建配置好测试环境的 TestClient。

    环境变量必须写在 get_settings.cache_clear() 之前：
    Settings 用 lru_cache 缓存，改了 env 但不清理缓存，读到的还是旧值。
    """
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("DAILY_QUOTA", "2")
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()
    # 等级/存量库和额度库共用同一个 SQLite 文件，缓存必须一起清，
    # 否则下一个测试会继续用上一个测试的临时目录。
    get_credit_store.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_runtime_store.cache_clear()
    get_credit_store.cache_clear()


@pytest.fixture(autouse=True)
def fake_success_stream(monkeypatch):
    """把真实模型换成“立刻成功”的假流。"""

    async def fake_stream(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        yield {"type": "delta", "content": "你好"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_module, "stream_reply", fake_stream)


def test_missing_token_returns_401(client: TestClient) -> None:
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_token_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401


def test_expired_token_returns_401(client: TestClient) -> None:
    # 默认允许 30 秒时钟偏差，所以要让过期时间明显超过 30 秒。
    token = make_token(exp_delta=-60)

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 401


def test_wrong_audience_returns_401(client: TestClient) -> None:
    token = make_token(aud="another-service")

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 401


def test_valid_request_streams_and_reports_quota(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "delta" in response.text
    assert response.text.rstrip().endswith('"done"}')
    # DAILY_QUOTA=2，第一次用掉后还剩 1。
    assert response.headers["x-quota-remaining"] == "1"
    assert response.headers["x-quota-limit"] == "2"


def test_quota_exhausted_returns_429(client: TestClient) -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    assert client.post("/api/chat", json=payload, headers=auth_header()).status_code == 200
    assert client.post("/api/chat", json=payload, headers=auth_header()).status_code == 200

    response = client.post("/api/chat", json=payload, headers=auth_header())
    assert response.status_code == 429
    assert "额度" in response.json()["detail"]
    assert int(response.headers["retry-after"]) > 0


def test_admin_is_unlimited(client: TestClient) -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    headers = auth_header(make_token(admin=True))

    for _ in range(5):
        response = client.post("/api/chat", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.headers["x-quota-unlimited"] == "1"


def test_missing_secret_returns_503(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("L2D_AI_JWT_SECRET", "")
    get_settings.cache_clear()

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )

    assert response.status_code == 503


def test_quota_endpoint_reports_without_consuming(client: TestClient) -> None:
    headers = auth_header()

    first = client.get("/api/quota", headers=headers)
    assert first.status_code == 200
    assert first.json()["remaining"] == 2
    assert first.json()["unlimited"] is False

    # 连续查三次也不应该消耗额度。
    assert client.get("/api/quota", headers=headers).json()["remaining"] == 2
    assert client.get("/api/quota", headers=headers).json()["remaining"] == 2

    client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert client.get("/api/quota", headers=headers).json()["remaining"] == 1


def test_member_uses_member_quota(client: TestClient, monkeypatch) -> None:
    # 普通用户 1 条、会员 3 条：第 2 条起就能看出用的是哪套额度。
    monkeypatch.setenv("DAILY_QUOTA", "1")
    monkeypatch.setenv("MEMBER_DAILY_QUOTA", "3")
    get_settings.cache_clear()
    get_runtime_store.cache_clear()
    get_credit_store.cache_clear()

    # token 的 sub 默认是 "42"
    get_credit_store().set_tier(42, "member", days=7)

    payload = {"messages": [{"role": "user", "content": "hi"}]}
    responses = [
        client.post("/api/chat", json=payload, headers=auth_header())
        for _ in range(4)
    ]

    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    # 429 的提示要说会员的实际上限，否则用户看到“1 条”会以为额度算错了
    assert "3" in responses[-1].json()["detail"]


def test_quota_endpoint_reports_tier_and_credit(client: TestClient) -> None:
    store = get_credit_store()
    store.set_tier(42, "member", days=7)
    store.grant_credits(42, 4, "admin_grant")

    data = client.get("/api/quota", headers=auth_header()).json()

    assert data["tier"] == "member"
    assert data["credit_balance"] == 4
    assert data["tier_expires_at"] is not None
    # 会员的 limit 要是会员额度，不是普通用户额度
    assert data["limit"] == 60


def test_chat_headers_report_tier_and_credit(client: TestClient) -> None:
    store = get_credit_store()
    store.set_tier(42, "member", days=7)
    store.grant_credits(42, 4, "admin_grant")

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(),
    )

    assert response.status_code == 200
    assert response.headers["x-quota-tier"] == "member"
    assert response.headers["x-quota-credit"] == "4"


def test_quota_endpoint_admin_is_unlimited(client: TestClient) -> None:
    response = client.get("/api/quota", headers=auth_header(make_token(admin=True)))

    assert response.status_code == 200
    assert response.json()["unlimited"] is True
    assert response.json()["remaining"] is None


def test_global_quota_returns_429_for_second_user(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("GLOBAL_DAILY_QUOTA", "1")
    get_settings.cache_clear()
    get_runtime_store.cache_clear()

    payload = {"messages": [{"role": "user", "content": "hi"}]}

    first = client.post("/api/chat", json=payload, headers=auth_header())
    assert first.status_code == 200
    assert first.headers["x-global-quota-remaining"] == "0"

    second = client.post(
        "/api/chat",
        json=payload,
        headers=auth_header(make_token(sub="43")),
    )
    assert second.status_code == 429
    assert "全站" in second.json()["detail"]

    # 管理员不参与全站计数，仍然可以聊天。
    admin = client.post(
        "/api/chat",
        json=payload,
        headers=auth_header(make_token(sub="44", admin=True)),
    )
    assert admin.status_code == 200


def test_failure_before_output_refunds_quota(
    client: TestClient, monkeypatch
) -> None:
    """模型一个字都没输出就失败时，这次额度应该退回去。"""
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    # 先把额度调成 1，让“有没有退款”变得可观察。
    monkeypatch.setenv("DAILY_QUOTA", "1")
    get_settings.cache_clear()
    get_runtime_store.cache_clear()

    calls = {"count": 0}

    async def flaky_stream(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        calls["count"] += 1
        # 只有第一次失败；后续成功，方便观察额度是否被退回。
        if calls["count"] == 1:
            raise RuntimeError("模拟模型还没说话就崩了")
        yield {"type": "delta", "content": "恢复了"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_module, "stream_reply", flaky_stream)

    first = client.post("/api/chat", json=payload, headers=auth_header())
    assert first.status_code == 200
    assert '"error"' in first.text

    # 如果没退款，这里会是 429；退款成功才会有 200。
    second = client.post("/api/chat", json=payload, headers=auth_header())
    assert second.status_code == 200

    # 第三请求应当用尽额度。
    third = client.post("/api/chat", json=payload, headers=auth_header())
    assert third.status_code == 429
