from __future__ import annotations

import asyncio
import functools
from typing import Any

from langchain_core.messages import AIMessage

from app.relationship.appraisal import Appraiser


def async_test(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeModel:
    def __init__(self, content: str | list[Any]) -> None:
        self.content = content
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: Any) -> AIMessage:
        self.prompts.append(prompt)
        return AIMessage(content=self.content)


@async_test
async def test_appraiser_returns_result():
    model = FakeModel(
        '{"event":"care","intensity":1,"confidence":1,"reply_bias":0.5}'
    )
    call = await Appraiser(model).appraise(
        persona_name="梅林",
        user_text="你辛苦了",
        recent=[],
        state_context="平静",
        timeout=1.0,
    )
    assert call.status == "ok"
    assert call.result.event == "care"
    assert "只输出一个 JSON 对象" in model.prompts[0]


@async_test
async def test_appraiser_accepts_content_blocks():
    model = FakeModel([{"type": "text", "text": '{"event":"apology"}'}])
    call = await Appraiser(model).appraise(
        persona_name="梅林", user_text="对不起", recent=[],
        state_context="平静", timeout=1.0,
    )
    assert call.status == "ok"
    assert call.result.event == "apology"


@async_test
async def test_appraiser_times_out():
    class SlowModel:
        async def ainvoke(self, prompt):
            await asyncio.sleep(1)
            return AIMessage(content='{"event":"care"}')

    call = await Appraiser(SlowModel()).appraise(
        persona_name="梅林", user_text="在吗", recent=[],
        state_context="平静", timeout=0.01,
    )
    assert call.status == "timeout"
    assert call.result is None


@async_test
async def test_appraiser_reports_parse_error():
    call = await Appraiser(FakeModel("我不知道")).appraise(
        persona_name="梅林", user_text="嗯", recent=[],
        state_context="平静", timeout=1.0,
    )
    assert call.status == "parse_error"
    assert call.result is None
    assert call.raw_output == "我不知道"


@async_test
async def test_appraiser_reports_model_error():
    class BrokenModel:
        async def ainvoke(self, prompt):
            raise RuntimeError("boom")

    call = await Appraiser(BrokenModel()).appraise(
        persona_name="梅林", user_text="嗯", recent=[],
        state_context="平静", timeout=1.0,
    )
    assert call.status == "error"
    assert call.result is None
