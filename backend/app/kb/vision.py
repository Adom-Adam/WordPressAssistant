"""用 DeepSeek 视觉模型把图片变成可检索的文字。

核心思路：
    FTS5 只能搜文字，不能理解像素。
    所以在“同步文章”阶段，把每张图片交给 `deepseek-flash`：
        - 生成一句中文描述；
        - 转录图片里的文字（OCR）；
        - 提取检索关键词。
    然后把这些文字当成一个“图片文本块”写进 FTS5。
    用户搜索“DDS 流程图”“报错截图”时，就能命中图片描述。

几个工程细节：
- 图片先由后端下载，再以 base64 发给模型：
  这样不受外链防盗链、下载超时（官方 URL 方式有 60 秒限制）影响；
- 结果按图片 URL 缓存到 SQLite：
  重复同步、同一张图出现在多篇文章时，不会重复花钱；
- 关闭“思考模式”：
  DeepSeek V4.1 默认先生成思维链。图片描述是简单任务，
  开着思考模式会把 max_tokens 全部用在推理上，正文反而为空。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from functools import lru_cache
from typing import Any

import httpx
from openai import OpenAI

from app.config import get_settings
from app.kb import db
from app.kb.text import ImageRef

logger = logging.getLogger(__name__)

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# 给模型的任务说明。要求输出 JSON，方便程序稳定解析；
# 明确“不要编造”可以减少模型对模糊图片的幻觉。
_VISION_PROMPT = """你在为一个中文技术博客建立图片索引。请仔细观察图片，并只输出一个 JSON 对象：
{
  "description": "用一句中文说明图片内容（是截图、流程图、架构图、照片还是图表）",
  "ocr_text": "逐字转录图片中清晰可见的文字、代码、表格内容；没有就填空字符串",
  "keywords": ["3 到 8 个中英文检索关键词"]
}

要求：
- 只描述图片里真实存在的内容，不要猜测、不要补充背景知识；
- ocr_text 保留原文，不要翻译、不要总结；
- keywords 要覆盖图片主题、图中文字和技术名词；
- 不要输出 JSON 以外的任何文字。"""


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    """复用 OpenAI 客户端。

    DeepSeek 的接口与 OpenAI 兼容，所以可以直接用官方 `openai` SDK，
    只需要把 `base_url` 指向 https://api.deepseek.com。
    客户端内部维护连接池，复用比每次新建更高效。
    """

    settings = get_settings()
    return OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=settings.request_timeout,
    )


def _guess_mime(data: bytes, content_type: str) -> str:
    """根据文件头判断图片类型。

    官方文档说格式由“文件实际内容”决定，而不是扩展名或声明的 MIME，
    所以这里优先看 magic bytes（文件头），再看响应头。
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
    return header if header in _ALLOWED_MIME else ""


def _download_image(url: str) -> tuple[bytes, str]:
    """下载图片并返回 (二进制内容, MIME)。

    抛出的异常由调用方捕获；这里不吞掉，方便上层记录具体原因。
    """

    settings = get_settings()
    headers = {
        # 一些站点会拒绝没有 User-Agent 的请求。
        "User-Agent": settings.kb_user_agent,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
    }

    with httpx.Client(
        timeout=settings.kb_request_timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = client.get(url)
        response.raise_for_status()

        data = response.content
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > settings.kb_image_max_bytes:
            raise ValueError(
                f"图片太大（{len(data)} 字节，上限 {settings.kb_image_max_bytes}）"
            )

        mime = _guess_mime(data, response.headers.get("content-type", ""))
        if not mime:
            raise ValueError("不是受支持的图片格式（JPEG/PNG/GIF/WebP）")
        return data, mime


def _parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里解析 JSON。

    即使开了 JSON Output，仍然做一层兜底：
    有的模型会在 JSON 前后加 ```json 代码围栏或解释文字。
    """

    text = (text or "").strip()
    if not text:
        return {}

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    # 截取第一个 { 到最后一个 } 之间的内容再试一次。
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass

    # 完全解析不了时，至少把原文当作描述，避免丢弃模型给出的信息。
    return {"description": text}


def _normalize_keywords(value: Any) -> list[str]:
    """把模型返回的 keywords 统一成字符串列表。"""

    if isinstance(value, str):
        # 有些模型会返回 "a, b, c" 而不是数组。
        items = re.split(r"[,，、;；\n]+", value)
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        items = []

    cleaned: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned[:12]


def describe_image(image: ImageRef, title: str = "") -> dict[str, Any] | None:
    """返回一张图片的描述；失败时返回 None。

    返回结构：
        {
            "url": "...",
            "description": "...",
            "ocr_text": "...",
            "keywords": ["..."],
            "model": "deepseek-flash",
        }
    """

    settings = get_settings()

    # 关闭图片理解时，仍然可以把 alt/标题/URL 当作弱文本索引，
    # 这部分由 sync.py 负责，这里直接返回 None。
    if not settings.vision_enabled:
        return None

    key = db.image_key(image.url)
    cached = db.get_image_cache(key)
    if cached is not None:
        logger.info("图片命中缓存：%s", image.url)
        return cached

    try:
        data, mime = _download_image(image.url)

        # 把 WordPress 自带的 alt/title/caption 一起交给模型，
        # 这些信息有时能帮助模型更准确地理解图片。
        hints = []
        if image.alt:
            hints.append(f"alt 文本：{image.alt}")
        if image.title:
            hints.append(f"标题：{image.title}")
        if image.caption:
            hints.append(f"图注：{image.caption}")
        if title:
            hints.append(f"所在文章：{title}")

        user_text = _VISION_PROMPT
        if hints:
            user_text += "\n\n文章提供的线索（仅供参考，若与图片不符以图片为准）：\n" + "\n".join(hints)

        encoded = base64.b64encode(data).decode("ascii")
        response = _client().chat.completions.create(
            model=settings.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{encoded}",
                                # original 保留截图里的文字细节；low 更快但会缩到 512×512。
                                "detail": settings.vision_detail,
                            },
                        },
                    ],
                }
            ],
            max_tokens=settings.vision_max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
            # DeepSeek 特有参数：关闭思考模式，否则 max_tokens 可能全被思维链吃掉。
            extra_body={"thinking": {"type": "disabled"}},
        )

        message = response.choices[0].message
        parsed = _parse_json_object(message.content or "")

        description = str(parsed.get("description") or "").strip()
        ocr_text = str(parsed.get("ocr_text") or "").strip()
        keywords = _normalize_keywords(parsed.get("keywords"))

        # 模型什么都没说时，用 alt 兜底，至少索引里有一条可读文本。
        if not description:
            description = image.alt or image.caption or image.title or "文章中的一张图片"

        usage = response.usage
        if usage is not None:
            logger.info(
                "图片描述完成：输入 %s tokens，输出 %s tokens（%s）",
                usage.prompt_tokens,
                usage.completion_tokens,
                image.url,
            )

        result = {
            "url": image.url,
            "description": description,
            "ocr_text": ocr_text,
            "keywords": keywords,
            "model": settings.vision_model,
        }
        db.save_image_cache(
            key=key,
            url=image.url,
            description=description,
            ocr_text=ocr_text,
            keywords=keywords,
            model=settings.vision_model,
        )
        return result

    except Exception as exc:  # noqa: BLE001 - 单张图失败不应该中断整篇同步
        logger.warning("图片理解失败：%s（%s: %s）", image.url, type(exc).__name__, exc)
        # 记录失败原因，方便排查；下次同步会重试（get_image_cache 会忽略 error 行）。
        try:
            db.save_image_cache(
                key=key,
                url=image.url,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            logger.exception("写入图片失败缓存时出错")
        return None
