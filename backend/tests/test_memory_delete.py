"""注销账号时删除记忆。

WordPress 账号是唯一身份：QQ 只是登录方式，可以换绑；注销 WP 账号
则必须把这个人名下的记忆、事件和身份关联一起删干净，不能留下孤儿数据。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store
from app.memory import db as memory_db
from app.memory import identity, store
from app.memory.store import MemoryDraft
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "memory-delete-secret-" + "k" * 48


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


def _headers(user_id: int = 7, *, admin: bool = False) -> dict[str, str]:
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


def _seed(user_id: int = 7, qq_uin: str = "10001") -> None:
    """写入一条记忆、两条事件和身份关联，模拟一个聊过天的账号。"""
    memory_db.init_schema()
    conn = memory_db.connect()
    try:
        identity.link_identity(
            conn,
            [identity.person_key_web(user_id), identity.person_key_qq(qq_uin)],
            wp_user_id=user_id,
            qq_uin=qq_uin,
        )
        store.add_memory(
            conn,
            MemoryDraft(
                person_key=f"wp:{user_id}",
                type="fact",
                text="阿东在学 ROS2",
                scope="personal",
                audience=[f"wp:{user_id}"],
            ),
            embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
        )
        identity.touch_person(conn, f"wp:{user_id}", wp_user_id=user_id)
        for source in ("user_message", "assistant_message"):
            conn.execute(
                """
                INSERT INTO events (occurred_at, person_key, platform, channel_id,
                                    scope, audience, audience_key, sensitivity,
                                    source, content, created_at)
                VALUES ('2026-11-07T00:00:00+00:00', ?, 'web', 'web',
                        'personal', ?, ?, 'normal', ?, '内容',
                        '2026-11-07T00:00:00+00:00')
                """,
                (f"wp:{user_id}", f'["wp:{user_id}"]', f"wp:{user_id}", source),
            )
        conn.commit()
    finally:
        conn.close()


def test_delete_my_memory_removes_all_traces(client) -> None:
    _seed()

    response = client.delete("/api/me/memory", headers=_headers())

    assert response.status_code == 204
    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn) == {"events": 0, "memories": 0, "persons": 0}
    finally:
        conn.close()


def test_delete_my_memory_keeps_other_accounts(client) -> None:
    _seed(7)
    _seed(8, "20002")

    response = client.delete("/api/me/memory", headers=_headers(7))

    assert response.status_code == 204
    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn) == {"events": 2, "memories": 1, "persons": 2}
    finally:
        conn.close()


def test_delete_my_memory_requires_login(client) -> None:
    assert client.delete("/api/me/memory").status_code == 401


def test_admin_can_purge_another_users_memory(client) -> None:
    _seed()

    response = client.delete("/api/admin/users/7/memory", headers=_headers(99, admin=True))

    assert response.status_code == 204
    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn)["memories"] == 0
    finally:
        conn.close()


def test_non_admin_cannot_purge_another_users_memory(client) -> None:
    _seed()

    response = client.delete("/api/admin/users/7/memory", headers=_headers(8))

    assert response.status_code == 403


def test_delete_failure_is_reported(client, monkeypatch) -> None:
    """删除失败不能假装成功：WordPress 侧要靠 503 阻止注销。"""
    from app.memory import pipeline

    def boom(user_id: int) -> dict[str, int]:
        raise RuntimeError("模拟数据库故障")

    monkeypatch.setattr(pipeline, "delete_user_memory", boom)

    response = client.delete("/api/me/memory", headers=_headers())

    assert response.status_code == 503


def test_delete_my_memory_clears_relationship_rows(client) -> None:
    """关系状态、决策日志、待续话题也必须一起删。

    它们是“她怎么看你”的另一半：只删记忆不删关系，账号注销后
    主动消息还能按旧好感找上门。
    """
    _seed()
    conn = memory_db.connect()
    try:
        conn.execute(
            """INSERT INTO relationship_state (persona_key, person_key, updated_at)
               VALUES ('merlin', 'wp:7', '2026-11-07T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO pending_topics (persona_key, person_key, topic,
                   created_at, expires_at)
               VALUES ('merlin', 'wp:7', '面试怎么样',
                       '2026-11-07T00:00:00+00:00', '2026-11-10T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO decision_log (persona_key, channel_id, person_key,
                   verdict, created_at)
               VALUES ('merlin', 'qq_private', 'wp:7', 'reply',
                       '2026-11-07T00:00:00+00:00')"""
        )
        conn.commit()
    finally:
        conn.close()

    assert client.delete("/api/me/memory", headers=_headers()).status_code == 204

    conn = memory_db.connect()
    try:
        for table in ("relationship_state", "pending_topics", "decision_log"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table
    finally:
        conn.close()
