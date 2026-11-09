"""把念头写成一句真的说得出的话。

只做一件事：给模型一段足够的上下文，拿回一条短消息。发不发、发给谁、
什么时候发都由调度器决定，所以这里的所有失败都只是“这轮不发了”，
绝不抛异常给上层。

私聊和群聊的提示词分开写。群聊那一路接收 `recent` 参数只是为了保持
签名一致，**并不会把它拼进提示词**——群里的公开场合不能出现私聊内容，
这一点靠参数隔离不够，必须靠不渲染。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.llm_json import text_of
from app.persona.models import PersonaVersion

logger = logging.getLogger(__name__)

# 念头类型 → 一句说明，告诉模型这条消息的由头是什么。
KIND_HINTS = {
    "event_followup": "追问之前没聊完的那件事，像突然想起来一样自然",
    "missing": "表达想念，但别黏人，一句就好",
    "care": "关心对方的状态，语气放软，不要像客服",
    "spontaneous": "分享一个刚想到的念头或小事，语气轻松",
    "companionship": "单纯想聊两句，没有具体的事",
}


@dataclass(frozen=True, slots=True)
class ComposeResult:
    """一次生成的结果。`status` 直接写进 `proactive_log`。"""

    text: str
    status: str
    latency_ms: int = 0


def build_proactive_prompt(
    *,
    persona: PersonaVersion,
    kind: str,
    state_context: str,
    recent: list[str],
    topic_hint: str = "",
    channel: str = "private",
    group_topic: str = "",
) -> str:
    """拼主动消息的提示词。群聊不渲染 `recent`。"""
    lines = [f"你是{persona.name}。", persona.core.strip(), ""]
    lines.append(f"【这次想说话的由头】{KIND_HINTS.get(kind, KIND_HINTS['companionship'])}")

    if topic_hint:
        lines.append(f"【具体的事】{topic_hint}")

    if state_context.strip():
        lines.append("")
        lines.append("【当前状态】（只读背景，不要复述）")
        lines.append(state_context.strip())

    if channel == "group":
        lines.append("")
        if group_topic.strip():
            lines.append(f"【群里正在聊】{group_topic.strip()}")
        lines.append(
            "【场合】这是群聊：说给在场所有人听，不要提及任何私聊内容，"
            "不要 @ 任何人，不要求别人单独回复。"
        )
    else:
        lines.append("")
        if recent:
            lines.append("【最近的对话】（从旧到新）")
            for item in recent[-4:]:
                lines.append(f"- {item}")
        lines.append(
            "【要求】像给熟人发消息：一句话，不超过 80 字；"
            "不要写旁白、括号动作或引号；不要问“在吗”这类空话。"
        )

    lines.append("")
    lines.append("直接输出这条消息本身，不要任何解释。")
    return "\n".join(lines)


class ProactiveWriter:
    """默认用和聊天同一个模型，但温度高一点、只给 200 token。"""

    def __init__(self, model: Any = None) -> None:
        self.model = model

    def _model(self) -> Any:
        if self.model is not None:
            return self.model
        from app.agent.model import build_chat_model
        from app.runtime_settings import get_runtime_store

        runtime = get_runtime_store().load()
        return build_chat_model(runtime.model, temperature=0.9, max_tokens=200)

    async def compose(
        self,
        *,
        persona: PersonaVersion,
        kind: str,
        state_context: str,
        recent: list[str],
        topic_hint: str = "",
        channel: str = "private",
        group_topic: str = "",
        timeout: float = 10.0,
    ) -> ComposeResult:
        """生成一条主动消息；超时、报错、空文本都返回对应的 status。"""
        prompt = build_proactive_prompt(
            persona=persona,
            kind=kind,
            state_context=state_context,
            recent=recent,
            topic_hint=topic_hint,
            channel=channel,
            group_topic=group_topic,
        )
        started = time.monotonic()

        def _elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            response = await asyncio.wait_for(
                self._model().ainvoke(prompt), timeout=max(1.0, float(timeout))
            )
        except asyncio.TimeoutError:
            logger.warning("主动消息生成超时 kind=%s", kind)
            return ComposeResult(text="", status="timeout", latency_ms=_elapsed())
        except Exception:  # noqa: BLE001 - 生成失败只是这轮不发
            logger.warning("主动消息生成失败 kind=%s", kind, exc_info=True)
            return ComposeResult(text="", status="error", latency_ms=_elapsed())

        text = text_of(response).strip()
        if not text:
            return ComposeResult(text="", status="empty", latency_ms=_elapsed())
        return ComposeResult(text=text, status="ok", latency_ms=_elapsed())
