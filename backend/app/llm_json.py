"""模型 JSON 输出的小工具。

DeepSeek 等模型有时会把 JSON 包在 ```json 代码块里，有时又直接返回
内容块列表。记忆提炼和关系评估都要处理这两种情况，所以抽到这里共用，
避免两套实现慢慢跑偏。
"""

from __future__ import annotations

from typing import Any


def strip_fence(raw: str) -> str:
    """去掉首尾的 Markdown 代码围栏，保留中间内容。"""
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def text_of(result: Any) -> str:
    """兼容 `str` 与内容块列表两种消息格式。"""
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""
