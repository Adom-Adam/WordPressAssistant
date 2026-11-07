"""从一轮问答里提炼候选记忆。

设计约束：

- 模型只允许把内容记到**当前说话人**名下（`subject` 必须是 `current`），
  这样“A 的私事被记到 B 名下”在解析层就被堵死；
- 输出必须是 JSON 数组；不是数组、类型未知、正文过长、`subject` 不是
  `current` 的条目整条丢弃，不做“猜一个”的修补；
- 提取结果落库前还要过 `sensitivity.detect_sensitivity`，命中敏感规则
  的标成 `secret`；
- 模型报错/超时只返回空列表，绝不影响已经发出的回复。

这里只做“候选”，真正的去重与写入在 `store.add_memory`。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.memory.sensitivity import detect_sensitivity
from app.memory.store import VALID_MEMORY_TYPES, MemoryDraft

logger = logging.getLogger(__name__)

MAX_ITEM_TEXT = 300

EXTRACTION_PROMPT = """你在帮一个长期陪伴型角色整理记忆。请从下面这轮对话中，
提取值得长期记住的信息，只输出 JSON 数组，不要输出其他任何文字。

每一项的格式：
{{"type": "fact|preference|episode|promise|relationship|reflection",
 "text": "第三人称、简短、不超过 80 字",
 "importance": 0.0-1.0,
 "confidence": 0.0-1.0}}

规则：
1. subject 只能是 "current"（当前说话人），不要记录其他人的私事；
2. 只记长期有用的事：稳定的偏好、正在做的事、约定、重要经历；
   寒暄、玩笑、临时情绪不要记；
3. 没有值得记的内容就输出 []；
4. text 用第三人称描述说话人，不要写成对话原文。

对话频道：{channel}
说话人：{display_name}
用户：{user_text}
梅林：{assistant_text}
"""


@dataclass(frozen=True)
class Turn:
    """一轮完整的对话，以及它在时间线里的事件 id。"""

    person_key: str
    channel: str
    user_text: str
    assistant_text: str
    group_id: int | None = None
    user_event_id: int | None = None
    assistant_event_id: int | None = None
    display_name: str | None = None


def build_prompt(turn: Turn) -> str:
    channel = turn.channel
    if turn.channel == "qq_group" and turn.group_id is not None:
        channel = f"{turn.channel}（群 {turn.group_id}）"
    return EXTRACTION_PROMPT.format(
        channel=channel,
        display_name=turn.display_name or turn.person_key,
        user_text=(turn.user_text or "")[:1500],
        assistant_text=(turn.assistant_text or "")[:1500],
    )


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _clamp(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _source_ids(turn: Turn) -> list[int]:
    return [
        int(event_id)
        for event_id in (turn.user_event_id, turn.assistant_event_id)
        if event_id is not None
    ]


def parse_candidates(raw: str, turn: Turn) -> list[MemoryDraft]:
    """把模型输出解析成候选记忆；任何不合法条目直接丢弃。"""
    try:
        payload = json.loads(_strip_fence(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []

    if turn.channel == "qq_group" and turn.group_id is not None:
        scope = "group"
        audience = [f"group:{turn.group_id}"]
    else:
        scope = "personal"
        audience = [turn.person_key]

    drafts: list[MemoryDraft] = []
    seen: set[tuple[str, str]] = set()

    for item in payload:
        if not isinstance(item, dict):
            continue

        memory_type = item.get("type")
        if memory_type not in VALID_MEMORY_TYPES:
            continue

        subject = item.get("subject", "current")
        if subject != "current":
            continue

        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text or len(text) > MAX_ITEM_TEXT:
            continue

        key = (memory_type, text)
        if key in seen:
            continue
        seen.add(key)

        drafts.append(
            MemoryDraft(
                person_key=turn.person_key,
                type=memory_type,
                text=text,
                scope=scope,
                audience=audience,
                sensitivity=detect_sensitivity(text),
                importance=_clamp(item.get("importance"), 0.5),
                confidence=_clamp(item.get("confidence"), 0.6),
                source_event_ids=_source_ids(turn),
            )
        )

    return drafts


def _text_of(result: Any) -> str:
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


def _default_model() -> Any:
    from app.agent.model import build_chat_model
    from app.runtime_settings import get_runtime_store

    runtime = get_runtime_store().load()
    return build_chat_model(runtime.model)


class Extractor:
    """默认用和聊天同一个模型；测试可以注入任何带 `ainvoke` 的对象。"""

    def __init__(self, model: Any = None) -> None:
        self.model = model

    async def extract(self, turn: Turn) -> list[MemoryDraft]:
        try:
            model = self.model or _default_model()
            result = await model.ainvoke(build_prompt(turn))
        except Exception:  # noqa: BLE001 - 提炼失败不影响回复
            logger.warning("记忆提炼失败，跳过本轮", exc_info=True)
            return []
        return parse_candidates(_text_of(result), turn)
