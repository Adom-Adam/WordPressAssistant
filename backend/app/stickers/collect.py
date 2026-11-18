"""梅林自己收藏表情。

触发条件（全部满足才收藏）：

- 消息里的图是表情包（mface，或带 emoji_id 的 image），不是照片/截图；
- 她当时的评估结果是正面/玩闹（smalltalk / tease / flirt / joke / care）；
- 视觉模型给出的 collect_score 过阈值；
- 表情库没到容量上限。

收藏时下载原图、按内容哈希去重、打标签入库。出处匹配不到词表就记进
`proposed_origin`，等后台确认。
"""

from __future__ import annotations

import hashlib
import logging

from app.channels.qq.image_understanding import (
    allowed_url,
    release_image_budget,
    reserve_image_budget,
)
from app.config import get_settings
from app.memory import db as memory_db
from app.stickers import files, store
from app.stickers.tagging import analyze_bytes
from app.vision_common import download_image

logger = logging.getLogger(__name__)

# 她觉得“值得收藏”的阈值。低于这个分数的普通图不进库。
COLLECT_THRESHOLD = 0.7

# 只有这些评估结果才可能触发收藏——“她心情不错”是收藏的前提。
POSITIVE_EVENTS = {"smalltalk", "tease", "flirt", "joke", "care"}


def maybe_collect(
    url: str,
    *,
    source_person: str,
    appraisal_event: str | None,
    library_max: int,
    day: str,
    global_limit: int,
    user_limit: int,
) -> int | None:
    """尝试收藏一张表情；返回新素材 id，跳过或失败返回 None。"""
    if appraisal_event not in POSITIVE_EVENTS:
        return None
    if not url or not allowed_url(url):
        return None

    settings = get_settings()
    try:
        data, mime = download_image(
            url,
            timeout=settings.kb_request_timeout,
            max_bytes=settings.kb_image_max_bytes,
            user_agent=settings.kb_user_agent,
            follow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 - 收藏失败不影响聊天
        logger.info("收藏表情下载失败：%s（%s）", url[:80], type(exc).__name__)
        return None

    content_hash = hashlib.sha256(data).hexdigest()
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        if store.get_by_hash(conn, content_hash) is not None:
            return None
        if library_max > 0 and store.count_stickers(conn) >= library_max:
            return None
        origin_names: list[str] = []
        for item in store.list_origins(conn):
            origin_names.append(str(item["name"]))
            origin_names.extend(str(alias) for alias in item["aliases"])
    finally:
        conn.close()

    if not reserve_image_budget(source_person, day, global_limit, user_limit):
        return None

    analysis = analyze_bytes(data, mime, known_origins=origin_names)
    if analysis is None:
        release_image_budget(source_person, day)
        return None
    if analysis.get("kind") != "sticker":
        return None
    try:
        score = float(analysis.get("collect_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score < COLLECT_THRESHOLD:
        return None

    origin = str(analysis.get("known_origin") or "")
    proposed = "" if origin else str(analysis.get("origin") or "")
    tags = list(analysis.get("emotions") or [])
    try:
        content_hash_saved, file_name, detected = files.save_bytes(data, mime)
    except (OSError, ValueError):
        logger.warning("保存收藏表情文件失败", exc_info=True)
        return None

    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        sticker_id = store.add_sticker(
            conn,
            content_hash=content_hash_saved,
            file_name=file_name,
            format=detected,
            description=str(analysis.get("description") or ""),
            tone=str(analysis.get("tone") or "neutral"),
            origin=origin or None,
            proposed_origin=proposed,
            tags=tags,
            source="collected",
            source_person=source_person,
        )
        conn.commit()
        logger.info(
            "收藏表情 #%s person=%s score=%.2f origin=%s",
            sticker_id,
            source_person,
            score,
            origin or proposed or "-",
        )
        return sticker_id
    except Exception:  # noqa: BLE001
        logger.warning("写入收藏表情失败", exc_info=True)
        return None
    finally:
        conn.close()
