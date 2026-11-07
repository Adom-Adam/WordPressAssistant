from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.memory.extract import Extractor, Turn, parse_candidates

TURN = Turn(
    person_key="wp:12",
    channel="qq_private",
    user_text="我最近在学 ROS2，下周要答辩",
    assistant_text="答辩加油，我记着这件事。",
    user_event_id=1,
    assistant_event_id=2,
)


def test_parse_valid_json_builds_drafts() -> None:
    raw = """
    [
      {"type": "fact", "text": "阿东在学 ROS2", "importance": 0.8, "confidence": 0.9, "subject": "current"},
      {"type": "promise", "text": "下周答辩后要问结果", "importance": 0.6, "confidence": 0.7}
    ]
    """
    drafts = parse_candidates(raw, TURN)
    assert len(drafts) == 2
    assert drafts[0].person_key == "wp:12"
    assert drafts[0].scope == "personal"
    assert drafts[0].audience == ["wp:12"]
    assert drafts[0].source_event_ids == [1, 2]
    assert drafts[1].type == "promise"


def test_code_fence_and_invalid_json_are_tolerated() -> None:
    assert parse_candidates('```json\n[{"type":"fact","text":"x"}]\n```', TURN)[0].text == "x"
    assert parse_candidates("抱歉我不会输出 JSON", TURN) == []
    assert parse_candidates('{"type":"fact"}', TURN) == []


def test_bad_items_are_dropped() -> None:
    raw = """
    [
      {"type": "gossip", "text": "未知类型"},
      {"type": "fact", "text": ""},
      {"type": "fact", "text": "%s"},
      {"type": "fact", "text": "别人的私事", "subject": "other"}
    ]
    """ % ("长" * 400)
    assert parse_candidates(raw, TURN) == []


def test_sensitive_text_is_marked_secret() -> None:
    raw = '[{"type": "fact", "text": "我的密码是 abc12345"}]'
    drafts = parse_candidates(raw, TURN)
    assert drafts[0].sensitivity == "secret"


def test_group_turn_uses_group_audience() -> None:
    group_turn = Turn(
        person_key="wp:12",
        channel="qq_group",
        group_id=789,
        user_text="群里在聊 ROS2",
        assistant_text="嗯。",
    )
    drafts = parse_candidates('[{"type": "episode", "text": "群里聊了 ROS2"}]', group_turn)
    assert drafts[0].scope == "group"
    assert drafts[0].audience == ["group:789"]


def test_values_are_clamped() -> None:
    raw = '[{"type": "fact", "text": "x", "importance": 9, "confidence": -3}]'
    draft = parse_candidates(raw, TURN)[0]
    assert draft.importance == 1.0
    assert draft.confidence == 0.0


class FakeModel:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=self.content)


def test_extractor_calls_model_once() -> None:
    model = FakeModel('[{"type": "fact", "text": "阿东在学 ROS2"}]')
    drafts = asyncio.run(Extractor(model=model).extract(TURN))
    assert model.calls == 1
    assert drafts[0].text == "阿东在学 ROS2"


def test_extractor_swallows_model_errors() -> None:
    class Broken:
        async def ainvoke(self, messages):
            raise RuntimeError("boom")

    assert asyncio.run(Extractor(model=Broken()).extract(TURN)) == []
