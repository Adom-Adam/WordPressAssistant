"""用视觉模型给表情打标签。

输出结构化结果，导入脚本和自动收藏共用：

- `kind`：sticker / photo / screenshot。只有表情包才允许自动收藏；
- `tone`：warm / neutral / cold。cold 是“真正的冷漠”，不是可爱的白眼；
- `emotions`：只能从受控词表里选；
- `origin`：作品/话题出处。匹配不到出处词表时由调用方存进 proposed_origin；
- `collect_score`：0–1，表示“这张图值不值得收进表情库”。

结果按内容哈希缓存在 `sticker_analysis_cache`，同一张图只调用一次。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.memory import db as memory_db
from app.stickers.tags import EMOTION_TAGS, normalize_emotions
from app.vision_common import openai_client, parse_json_object

logger = logging.getLogger(__name__)

VALID_KINDS = {"sticker", "photo", "screenshot"}
VALID_TONES = {"warm", "neutral", "cold"}

_PROMPT = """你在整理一个聊天表情库。仔细看这张图，只输出一个 JSON 对象：
{
  "description": "用一两句中文说明画面内容和图中文字",
  "kind": "sticker | photo | screenshot",
  "tone": "warm | neutral | cold",
  "emotions": ["从下面的情绪词表里选 1 到 4 个"],
  "origin": "如果这张图明显出自某个游戏/动漫/网络梗，写出处；判断不了就填空字符串",
  "text": "图中清晰可见的文字，原样转录；没有就填空字符串",
  "collect_score": 0.0
}

情绪词表（只能用这些词）：
{emotions}

已知出处（优先从这里选；没有合适的就写你判断的名字）：
{origins}

tone 的含义：
- warm：可爱、亲切、卖萌、鼓励；
- neutral：普通吐槽、日常表情，没有明显温度；
- cold：真正的冷漠、无语、疏离、带点攻击性，不是可爱的“白眼”。

collect_score 表示这张图作为聊天表情是否好用：0.9 以上是通用百搭梗图，
0.5 左右是特定场景可用，0.2 以下是私人照片、模糊截图或没什么表达力的图。

要求：只描述真实可见的内容，不要编造；不要输出 JSON 以外的文字。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_get(content_hash: str) -> dict[str, Any] | None:
    try:
        conn = memory_db.connect()
        memory_db.init_schema(conn)
        row = conn.execute(
            "SELECT payload FROM sticker_analysis_cache WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["payload"]))
        return value if isinstance(value, dict) else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def _cache_put(content_hash: str, payload: dict[str, Any], model: str) -> None:
    try:
        conn = memory_db.connect()
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO sticker_analysis_cache
                   (content_hash, payload, model, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(content_hash) DO UPDATE SET
                   payload = excluded.payload,
                   model = excluded.model,
                   created_at = excluded.created_at""",
            (
                content_hash,
                json.dumps(payload, ensure_ascii=False),
                model,
                _now(),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning("写入表情识别缓存失败 hash=%s", content_hash[:12], exc_info=True)


def _normalize(
    parsed: dict[str, Any], *, data: bytes, known_origins: list[str]
) -> dict[str, Any]:
    description = str(parsed.get("description") or "").strip()
    kind = str(parsed.get("kind") or "sticker").strip().lower()
    if kind not in VALID_KINDS:
        kind = "sticker"
    tone = str(parsed.get("tone") or "neutral").strip().lower()
    if tone not in VALID_TONES:
        tone = "neutral"

    emotions = parsed.get("emotions")
    emotion_values = emotions if isinstance(emotions, list) else []
    origin = str(parsed.get("origin") or "").strip()
    text = str(parsed.get("text") or "").strip()
    try:
        collect_score = float(parsed.get("collect_score") or 0.0)
    except (TypeError, ValueError):
        collect_score = 0.0
    collect_score = max(0.0, min(1.0, collect_score))

    if not description:
        description = "一张表情包" if kind == "sticker" else "一张图片"

    return {
        "content_hash": hashlib.sha256(data).hexdigest(),
        "description": description,
        "kind": kind,
        "tone": tone,
        "emotions": normalize_emotions(emotion_values),
        "origin": origin,
        "known_origin": _match_origin(origin, known_origins),
        "text": text,
        "collect_score": collect_score,
    }


def _match_origin(origin: str, known_origins: list[str]) -> str:
    target = origin.casefold()
    if not target:
        return ""
    for name in known_origins:
        if name.casefold() == target:
            return name
    return ""


def analyze_bytes(
    data: bytes,
    mime: str,
    *,
    known_origins: list[str] | None = None,
) -> dict[str, Any] | None:
    """返回结构化识别结果；失败返回 None（调用方按占位符处理）。"""
    if not data:
        return None
    content_hash = hashlib.sha256(data).hexdigest()
    cached = _cache_get(content_hash)
    if cached is not None:
        return cached

    settings = get_settings()
    origins = list(known_origins or [])
    prompt = _PROMPT.format(
        emotions="、".join(EMOTION_TAGS),
        origins="、".join(origins) if origins else "（没有已知出处，可自由提议）",
    )
    encoded = base64.b64encode(data).decode("ascii")
    model = settings.vision_model
    try:
        response = openai_client().chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
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
            max_tokens=min(500, settings.vision_max_tokens),
            temperature=0.1,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        parsed = parse_json_object(response.choices[0].message.content or "")
        if not parsed:
            return None
        result = _normalize(parsed, data=data, known_origins=origins)
        _cache_put(content_hash, result, model)
        return result
    except Exception as exc:  # noqa: BLE001 - 单张失败不影响导入/收藏
        logger.warning("表情识别失败（%s: %s）", type(exc).__name__, exc)
        return None
