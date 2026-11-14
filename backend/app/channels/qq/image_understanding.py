"""QQ 聊天图片理解：把表情包/图片变成一句可读的中文。

调用链：
    解析层留下 [图片] / [表情包] 占位符
        → 这里下载图片、查缓存、必要时调用视觉模型
        → 返回一句描述，由适配器替换回占位符

三条边界：
- 只接受 QQ 图片域的 https 地址，避免把任意 URL 当成图片抓取；
- 同一张图按内容哈希缓存，表情包复用率高，缓存能省掉大部分调用；
- 每日预算按“全站 + 个人”原子占位，缓存命中不计数，失败会退还名额。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.config import get_settings
from app.memory import db as memory_db
from app.vision_common import download_image, openai_client, parse_json_object

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = "global"
CACHE_MAX_ROWS = 2000

# 只允许 QQ 的图片 CDN。NapCat 给的 URL 通常长这样：
#   https://gchat.qpic.cn/gchatpic_new/...
#   https://multimedia.nt.qq.com.cn/download?appid=...
_ALLOWED_HOST_SUFFIXES = (".qq.com", ".qq.com.cn", ".qpic.cn")
_ALLOWED_HOSTS = {"qq.com", "qq.com.cn", "qpic.cn"}

_PROMPT = """你在看 QQ 聊天里对方发来的一张图片，可能是表情包、照片或截图。
只输出一个 JSON 对象：
{
  "description": "用一两句中文说明画面内容（是表情包、照片还是截图，画了什么）",
  "text": "图中清晰可见的文字，原样转录；没有就填空字符串",
  "mood": "对方发这张图想表达的情绪或态度，用几个字；判断不了就填空字符串"
}

要求：
- 只描述图片里真实存在的内容，不要评价、不要编造；
- text 保留原文，不要翻译、不要总结；
- 不要输出 JSON 以外的任何文字。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _allowed_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_HOST_SUFFIXES)


def _cache_get(key: str) -> str | None:
    try:
        conn = memory_db.connect()
    except Exception:  # noqa: BLE001 - 缓存不可用只是多花一次钱
        return None
    try:
        row = conn.execute(
            "SELECT description FROM qq_image_cache WHERE key = ?", (key,)
        ).fetchone()
        return str(row["description"]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _cache_put(key: str, description: str, model: str) -> None:
    try:
        conn = memory_db.connect()
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO qq_image_cache (key, description, model, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   description = excluded.description,
                   model = excluded.model,
                   created_at = excluded.created_at""",
            (key, description, model, _now()),
        )
        conn.execute(
            """DELETE FROM qq_image_cache WHERE key IN (
                   SELECT key FROM qq_image_cache
                   ORDER BY created_at ASC, key ASC
                   LIMIT MAX(0, (SELECT COUNT(*) FROM qq_image_cache) - ?)
               )""",
            (CACHE_MAX_ROWS,),
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning("写入图片描述缓存失败 key=%s", key[:12], exc_info=True)


def _take(conn: sqlite3.Connection, scope: str, day: str, limit: int) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO qq_image_usage(scope, day, used) VALUES (?, ?, 1)
        ON CONFLICT(scope, day) DO UPDATE SET used = used + 1
        WHERE ? = 0 OR used < ?
        """,
        (scope, day, max(0, int(limit)), max(0, int(limit))),
    )
    return cursor.rowcount > 0


def _release(conn: sqlite3.Connection, scope: str, day: str) -> None:
    conn.execute(
        """UPDATE qq_image_usage SET used = used - 1
            WHERE scope = ? AND day = ? AND used > 0""",
        (scope, day),
    )


def _reserve_budget(
    person_key: str, day: str, global_limit: int, user_limit: int
) -> bool:
    """同时占全站和个人名额；任一失败都回滚已占的部分。"""
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        if not _take(conn, GLOBAL_SCOPE, day, global_limit):
            conn.commit()
            return False
        if not _take(conn, f"person:{person_key}", day, user_limit):
            _release(conn, GLOBAL_SCOPE, day)
            conn.commit()
            return False
        conn.commit()
        return True
    except sqlite3.Error:
        logger.warning("图片理解预算占位失败 person=%s", person_key, exc_info=True)
        return False
    finally:
        conn.close()


def _release_budget(person_key: str, day: str) -> None:
    conn = memory_db.connect()
    try:
        _release(conn, GLOBAL_SCOPE, day)
        _release(conn, f"person:{person_key}", day)
        conn.commit()
    except sqlite3.Error:
        logger.warning("退还图片理解预算失败 person=%s", person_key, exc_info=True)
    finally:
        conn.close()


def _compose(description: str, text: str, mood: str) -> str:
    parts = [description.strip()]
    if text.strip():
        parts.append(f"图中文字「{text.strip()}」")
    if mood.strip():
        parts.append(f"语气：{mood.strip()}")
    return "；".join(part for part in parts if part)[:200]


def describe_image(
    url: str,
    *,
    person_key: str,
    day: str,
    global_limit: int = 0,
    user_limit: int = 0,
) -> str | None:
    """返回图片的一句话描述；任何失败都返回 None（调用方退回占位符）。"""
    settings = get_settings()
    if not _allowed_url(url):
        logger.info("跳过非 QQ 图片地址：%s", url[:80])
        return None

    try:
        data, mime = download_image(
            url,
            timeout=settings.kb_request_timeout,
            max_bytes=settings.kb_image_max_bytes,
            user_agent=settings.kb_user_agent,
            # 域名已经校验过，禁止跟随重定向，防止被跳到内网地址。
            follow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 - 单张图失败不能影响回复
        logger.warning("下载聊天图片失败：%s（%s: %s）", url[:80], type(exc).__name__, exc)
        return None

    key = hashlib.sha256(data).hexdigest()
    cached = _cache_get(key)
    if cached:
        return cached

    if not _reserve_budget(person_key, day, global_limit, user_limit):
        logger.info("图片理解预算用尽 person=%s", person_key)
        return None

    try:
        encoded = base64.b64encode(data).decode("ascii")
        response = openai_client().chat.completions.create(
            model=settings.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{encoded}",
                                "detail": settings.vision_detail,
                            },
                        },
                    ],
                }
            ],
            max_tokens=min(400, settings.vision_max_tokens),
            temperature=0.1,
            response_format={"type": "json_object"},
            # 与知识库同步保持一致：关闭思考模式，避免 max_tokens 被推理吃光。
            extra_body={"thinking": {"type": "disabled"}},
        )
        parsed = parse_json_object(response.choices[0].message.content or "")
        description = str(parsed.get("description") or "").strip()
        if not description:
            raise ValueError("视觉模型没有返回描述")
        result = _compose(
            description,
            str(parsed.get("text") or ""),
            str(parsed.get("mood") or ""),
        )
        _cache_put(key, result, settings.vision_model)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("理解聊天图片失败：%s（%s: %s）", url[:80], type(exc).__name__, exc)
        _release_budget(person_key, day)
        return None
