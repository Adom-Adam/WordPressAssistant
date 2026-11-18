"""后台表情库：素材增删改查、出处词表、图片预览。

素材文件在 `data/stickers/library/`，库里只存文件名和标签。
自动收藏也会写同一套表，所以后台看到的和梅林用的是同一份素材。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import require_admin
from app.auth.jwt import AuthUser
from app.config import get_settings
from app.memory import db as memory_db
from app.runtime_settings import get_runtime_store
from app.schemas import StickerOriginPayload, StickerUpdate, StickerUpload
from app.stickers import files, store

logger = logging.getLogger(__name__)

router = APIRouter()


def _connect():
    conn = memory_db.connect()
    memory_db.init_schema(conn)
    return conn


def _serialize(sticker: store.Sticker) -> dict[str, object]:
    return {
        "id": sticker.id,
        "content_hash": sticker.content_hash,
        "file_name": sticker.file_name,
        "format": sticker.format,
        "description": sticker.description,
        "tone": sticker.tone,
        "origin": sticker.origin_name,
        "origin_id": sticker.origin_id,
        "proposed_origin": sticker.proposed_origin,
        "enabled": sticker.enabled,
        "source": sticker.source,
        "source_person": sticker.source_person,
        "tags": list(sticker.tags),
        "image_url": f"/api/stickers/{sticker.id}/image",
        "created_at": sticker.created_at,
        "updated_at": sticker.updated_at,
    }


def _snapshot() -> dict[str, object]:
    conn = _connect()
    try:
        stickers = store.list_stickers(conn)
        origins = store.list_origins(conn)
        tags = store.sticker_tags(conn)
        count = store.count_stickers(conn)
    finally:
        conn.close()
    return {
        "stickers": [_serialize(item) for item in stickers],
        "origins": origins,
        "tags": tags,
        "count": count,
        "library_max": get_runtime_store().load().sticker_library_max,
    }


def _decode_upload(content_base64: str) -> bytes:
    raw = str(content_base64 or "").strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="图片内容不是合法的 base64") from exc


@router.get("/admin/stickers")
async def list_stickers(
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    return await asyncio.to_thread(_snapshot)


@router.post("/admin/stickers")
async def upload_sticker(
    payload: StickerUpload,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    data = _decode_upload(payload.content_base64)
    limit = get_settings().kb_image_max_bytes
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"图片太大（上限 {limit} 字节）")

    def _save() -> dict[str, object]:
        content_hash, file_name, mime = files.save_bytes(data)
        conn = _connect()
        try:
            existing = store.get_by_hash(conn, content_hash)
            if existing is not None:
                return {"duplicate": True, "sticker": _serialize(existing)}
            sticker_id = store.add_sticker(
                conn,
                content_hash=content_hash,
                file_name=file_name,
                format=mime,
                description=payload.description,
                tone=payload.tone,
                origin=payload.origin,
                tags=payload.tags,
                source="manual",
            )
            conn.commit()
            sticker = store.get_sticker(conn, sticker_id)
            if sticker is None:
                raise HTTPException(status_code=500, detail="素材写入后读取失败")
            return {"duplicate": False, "sticker": _serialize(sticker)}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_save)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/admin/stickers/{sticker_id}")
async def update_sticker(
    sticker_id: int,
    payload: StickerUpdate,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    def _update() -> dict[str, object]:
        conn = _connect()
        try:
            kwargs: dict[str, object] = {
                key: value for key, value in fields.items() if key != "origin"
            }
            if "origin" in fields:
                kwargs["origin"] = fields["origin"]
            if not store.update_sticker(conn, sticker_id, **kwargs):
                raise HTTPException(status_code=404, detail="没有这张表情")
            conn.commit()
            sticker = store.get_sticker(conn, sticker_id)
            if sticker is None:
                raise HTTPException(status_code=404, detail="没有这张表情")
            return {"sticker": _serialize(sticker)}
        finally:
            conn.close()

    return await asyncio.to_thread(_update)


@router.delete("/admin/stickers/{sticker_id}")
async def delete_sticker(
    sticker_id: int,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    def _delete() -> dict[str, object]:
        conn = _connect()
        try:
            sticker = store.get_sticker(conn, sticker_id)
            if sticker is None:
                raise HTTPException(status_code=404, detail="没有这张表情")
            store.delete_sticker(conn, sticker_id)
            conn.commit()
            files.delete_file(sticker.file_name)
            return {"deleted": sticker_id}
        finally:
            conn.close()

    return await asyncio.to_thread(_delete)


@router.get("/stickers/{sticker_id}/image")
async def sticker_image(sticker_id: int) -> FileResponse:
    """公开预览：表情包本身不是敏感数据，用 id 直接读。"""

    def _find() -> tuple[object, str]:
        conn = _connect()
        try:
            sticker = store.get_sticker(conn, sticker_id)
            if sticker is None:
                raise HTTPException(status_code=404, detail="没有这张表情")
            return sticker, sticker.format
        finally:
            conn.close()

    sticker, media_type = await asyncio.to_thread(_find)
    try:
        path = files.path_for(sticker.file_name)  # type: ignore[attr-defined]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="素材文件路径非法") from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="素材文件不存在")
    return FileResponse(path, media_type=media_type)


@router.post("/admin/sticker-origins")
async def create_origin(
    payload: StickerOriginPayload,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    def _create() -> dict[str, object]:
        conn = _connect()
        try:
            store.ensure_origin(conn, payload.name, payload.aliases)
            conn.commit()
            return {"origins": store.list_origins(conn)}
        finally:
            conn.close()

    return await asyncio.to_thread(_create)


@router.put("/admin/sticker-origins/{origin_id}")
async def update_origin(
    origin_id: int,
    payload: StickerOriginPayload,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    def _update() -> dict[str, object]:
        conn = _connect()
        try:
            if not store.update_origin(
                conn, origin_id, name=payload.name, aliases=payload.aliases
            ):
                raise HTTPException(status_code=404, detail="没有这个出处")
            conn.commit()
            return {"origins": store.list_origins(conn)}
        finally:
            conn.close()

    return await asyncio.to_thread(_update)


@router.delete("/admin/sticker-origins/{origin_id}")
async def delete_origin(
    origin_id: int,
    _admin: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    def _delete() -> dict[str, object]:
        conn = _connect()
        try:
            if not store.delete_origin(conn, origin_id):
                raise HTTPException(status_code=404, detail="没有这个出处")
            conn.commit()
            return {"origins": store.list_origins(conn)}
        finally:
            conn.close()

    return await asyncio.to_thread(_delete)
