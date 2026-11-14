"""视觉调用的共用底层：下载、格式判断、JSON 解析与客户端。

知识库同步和 QQ 聊天图片理解共用这几件事。任务提示词和缓存策略不同，
所以 `kb/vision.py` 与 `channels/qq/image_understanding.py` 各自保留。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import httpx
from openai import OpenAI

from app.config import get_settings

ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@lru_cache(maxsize=1)
def openai_client() -> OpenAI:
    """复用 OpenAI 客户端（DeepSeek 接口与 OpenAI 兼容）。"""
    settings = get_settings()
    return OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=settings.request_timeout,
    )


def guess_mime(data: bytes, content_type: str = "") -> str:
    """按文件头判断图片类型，其次看响应头。

    官方文档说明格式由文件实际内容决定，而不是扩展名。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    header = (content_type or "").split(";")[0].strip().lower()
    return header if header in ALLOWED_MIME else ""


def download_image(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    user_agent: str = "",
    accept: str = "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
    follow_redirects: bool = True,
) -> tuple[bytes, str]:
    """下载并校验图片，返回 (内容, MIME)。异常交给调用方处理。

    QQ 聊天里的 URL 来自消息，调用方应传 `follow_redirects=False`，
    避免校验过域名后被重定向到内网地址。
    """
    headers = {"Accept": accept}
    if user_agent:
        headers["User-Agent"] = user_agent

    with httpx.Client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=headers,
    ) as client:
        response = client.get(url)
        response.raise_for_status()

        data = response.content
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > max_bytes:
            raise ValueError(f"图片太大（{len(data)} 字节，上限 {max_bytes}）")

        mime = guess_mime(data, response.headers.get("content-type", ""))
        if not mime:
            raise ValueError("不是受支持的图片格式（JPEG/PNG/GIF/WebP）")
        return data, mime


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里解析 JSON；前后有代码围栏或解释文字也能兜底。"""
    text = (text or "").strip()
    if not text:
        return {}

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass

    return {"description": text}
