"""后台表情库接口：上传、列表、改标签、删除、出处管理。"""

from __future__ import annotations

import base64
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import get_credit_store
from app.quota import get_quota_store
from app.runtime_settings import get_runtime_store

SECRET = "stickers-secret-" + "k" * 48

PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode()


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


def _headers(admin: bool = True) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(99, admin=admin)}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("L2D_AI_JWT_SECRET", SECRET)
    monkeypatch.setenv("QUOTA_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    monkeypatch.setenv("STICKER_DIR", str(tmp_path / "stickers"))
    get_settings.cache_clear()
    get_quota_store.cache_clear()
    get_credit_store.cache_clear()
    get_runtime_store.cache_clear()
    import app.api.admin_stickers  # noqa: F401
    import app.main as main

    with TestClient(main.app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_non_admin_is_rejected(client) -> None:
    assert client.get("/api/admin/stickers", headers=_headers(False)).status_code == 403


def test_upload_list_update_and_delete(client) -> None:
    response = client.post(
        "/api/admin/stickers",
        headers=_headers(),
        json={
            "content_base64": PNG,
            "filename": "cat.png",
            "description": "一只猫",
            "tone": "warm",
            "origin": "原神",
            "tags": ["开心", "猫"],
        },
    )
    assert response.status_code == 200
    sticker = response.json()["sticker"]
    sticker_id = sticker["id"]
    assert sticker["tone"] == "warm"
    assert sticker["origin"] == "原神"
    assert set(sticker["tags"]) == {"开心", "猫"}

    image = client.get(f"/api/stickers/{sticker_id}/image")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")

    listing = client.get("/api/admin/stickers", headers=_headers()).json()
    assert listing["count"] == 1
    assert listing["origins"][0]["name"] == "原神"
    assert "开心" in listing["tags"]

    updated = client.put(
        f"/api/admin/stickers/{sticker_id}",
        headers=_headers(),
        json={"tone": "cold", "origin": "鸣潮", "tags": ["冷漠"], "enabled": False},
    )
    assert updated.status_code == 200
    changed = updated.json()["sticker"]
    assert changed["tone"] == "cold"
    assert changed["origin"] == "鸣潮"
    assert changed["enabled"] is False

    deleted = client.delete(
        f"/api/admin/stickers/{sticker_id}", headers=_headers()
    )
    assert deleted.status_code == 200
    assert client.get("/api/admin/stickers", headers=_headers()).json()["count"] == 0


def test_upload_same_image_twice_is_deduplicated(client) -> None:
    payload = {"content_base64": PNG, "tone": "neutral"}
    first = client.post("/api/admin/stickers", headers=_headers(), json=payload)
    second = client.post("/api/admin/stickers", headers=_headers(), json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert client.get("/api/admin/stickers", headers=_headers()).json()["count"] == 1


def test_origin_management(client) -> None:
    created = client.post(
        "/api/admin/sticker-origins",
        headers=_headers(),
        json={"name": "星穹铁道", "aliases": ["崩铁", "Star Rail"]},
    )
    assert created.status_code == 200
    origin = created.json()["origins"][0]
    assert origin["name"] == "星穹铁道"

    updated = client.put(
        f"/api/admin/sticker-origins/{origin['id']}",
        headers=_headers(),
        json={"name": "崩坏：星穹铁道", "aliases": ["崩铁"]},
    )
    assert updated.status_code == 200
    assert updated.json()["origins"][0]["name"] == "崩坏：星穹铁道"

    deleted = client.delete(
        f"/api/admin/sticker-origins/{origin['id']}", headers=_headers()
    )
    assert deleted.status_code == 200
    assert deleted.json()["origins"] == []


def test_invalid_image_is_rejected(client) -> None:
    response = client.post(
        "/api/admin/stickers",
        headers=_headers(),
        json={"content_base64": base64.b64encode(b"not an image").decode()},
    )
    assert response.status_code == 400
