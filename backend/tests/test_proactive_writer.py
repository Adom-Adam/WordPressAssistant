"""主动消息的生成。

生成器只负责“把念头写成一句话”。发不发、发给谁、什么时候发，都是
调度器的事——这样模型写得不好时最多是这轮不发，不会连着发错。

群聊那一路有一条硬要求：不能带任何私聊内容进提示词。所以在群聊
提示词里，`recent` 会被整个丢掉，只用群里的公共话题。
"""

from __future__ import annotations

import asyncio
import functools

from app.persona.models import PersonaVersion
from app.proactive.message import ProactiveWriter, build_proactive_prompt


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def persona() -> PersonaVersion:
    return PersonaVersion.model_validate(
        {
            "schema": "l2d.persona/v1",
            "id": "merlin",
            "version": 1,
            "name": "梅林",
            "core": "你在博客里做小助手，说话简短。",
        }
    )


class FakeModel:
    def __init__(self, content, *, exc=None):
        self.content = content
        self.exc = exc

    async def ainvoke(self, prompt):
        if self.exc:
            raise self.exc
        return type("Msg", (), {"content": self.content})()


@async_test
async def test_compose_returns_text() -> None:
    writer = ProactiveWriter(FakeModel("晚上好呀，今天累不累？"))

    result = await writer.compose(
        persona=persona(),
        kind="care",
        state_context="心情一般",
        recent=["今天加班"],
        timeout=1.0,
    )

    assert result.status == "ok"
    assert result.text == "晚上好呀，今天累不累？"


@async_test
async def test_compose_empty_text() -> None:
    writer = ProactiveWriter(FakeModel("   "))

    result = await writer.compose(
        persona=persona(),
        kind="missing",
        state_context="",
        recent=[],
        timeout=1.0,
    )

    assert result.status == "empty"
    assert result.text == ""


@async_test
async def test_compose_error_is_fail_open() -> None:
    writer = ProactiveWriter(FakeModel("", exc=RuntimeError("boom")))

    result = await writer.compose(
        persona=persona(),
        kind="missing",
        state_context="",
        recent=[],
        timeout=1.0,
    )

    assert result.status == "error"
    assert result.text == ""


@async_test
async def test_compose_timeout_is_recorded() -> None:
    class SlowModel:
        async def ainvoke(self, prompt):
            await asyncio.sleep(5)
            return type("Msg", (), {"content": "太慢了"})()

    writer = ProactiveWriter(SlowModel())

    result = await writer.compose(
        persona=persona(),
        kind="missing",
        state_context="",
        recent=[],
        timeout=1.0,
    )

    assert result.status == "timeout"
    assert result.text == ""


def test_prompt_includes_persona_core_and_kind() -> None:
    prompt = build_proactive_prompt(
        persona=persona(),
        kind="missing",
        state_context="有点想念",
        recent=["今天加班"],
        topic_hint="",
        channel="private",
    )

    assert "你在博客里做小助手" in prompt
    assert "有点想念" in prompt
    assert "今天加班" in prompt
    assert "想念" in prompt


def test_prompt_truncates_recent_to_four() -> None:
    prompt = build_proactive_prompt(
        persona=persona(),
        kind="companionship",
        state_context="",
        recent=["第1句", "第2句", "第3句", "第4句", "第5句"],
        topic_hint="",
        channel="private",
    )

    # 只留最近四句：更早的第 1 句被丢掉。
    assert "第1句" not in prompt
    assert "第5句" in prompt


def test_group_prompt_uses_topic_and_drops_private_recent() -> None:
    prompt = build_proactive_prompt(
        persona=persona(),
        kind="spontaneous",
        state_context="",
        recent=["私聊里说过的秘密"],
        topic_hint="",
        channel="group",
        group_topic="大家在聊新番",
    )

    assert "大家在聊新番" in prompt
    assert "私聊里说过的秘密" not in prompt
    assert "不要提及任何私聊内容" in prompt
