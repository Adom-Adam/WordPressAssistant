"""私聊概率回复接入后的端到端行为。

覆盖四件事：
- 掷骰子失败时真的不说话，但用户消息仍在时间线里、回复债上升；
- 回复债会抬高下一条的 p；
- 生成中被新消息打断，第二次回复拿到合并后的内容；
- 决策/关系库故障时 fail-open，危机词在 silent 策略下仍然回复。
"""

from __future__ import annotations

import asyncio
import functools
import random
from dataclasses import replace
from typing import Any

import pytest

from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.qq_quota import QQUsage
from app.relationship.appraisal import AppraisalCall, parse_appraisal
from app.relationship.state import get_or_create_state, save_state


@pytest.fixture(autouse=True)
def memory_isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def async_test(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSender:
    def __init__(self) -> None:
        self.group: list[tuple[int, str]] = []
        self.private: list[tuple[int, str]] = []

    async def send_group_message(self, group_id: int, message: str) -> Any:
        self.group.append((group_id, message))

    async def send_private_message(self, user_id: int, message: str) -> Any:
        self.private.append((user_id, message))


class AllowAllGate:
    def reserve(self, **kwargs: Any) -> tuple[str, Any]:
        return "ok", QQUsage("admin", None, None)

    def refund(self, usage: Any) -> None:
        return None


class SequenceRandom(random.Random):
    """按给定序列返回随机数，让“沉默 / 回复”可以确定地复现。"""

    def __init__(self, values: list[float]) -> None:
        super().__init__(0)
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0) if self._values else 0.0


def stream_ok(text: str = "我在") -> Any:
    async def _stream(messages: Any, **kwargs: Any) -> Any:
        yield {"type": "delta", "content": text}

    return _stream


def private_event(qq_uin: int, text: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": qq_uin,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def make_persona(
    *, floor: float = 0.02, ceiling: float = 0.95, base: float = 0.95
) -> PersonaVersion:
    return PersonaVersion.model_validate(
        {
            "schema": "l2d.persona/v1",
            "id": "merlin",
            "version": 1,
            "name": "梅林",
            "core": "C",
            "dynamics": {
                "namespace": {"state": "merlin"},
                "reply": {"base": base, "floor": floor, "ceiling": ceiling},
                "pacing": {
                    "debounce_seconds": 0.0,
                    "debounce_max_seconds": 0.0,
                    "min_gap_ms": 0,
                    "max_gap_ms": 0,
                    "chars_per_second": 50.0,
                },
            },
        }
    )


class FakeAppraiser:
    def __init__(self, payload: str, *, status: str = "ok") -> None:
        self.call = AppraisalCall(parse_appraisal(payload), status, payload, 1)

    async def appraise(self, **kwargs: Any) -> AppraisalCall:
        return self.call


def make_adapter(
    tmp_path: Any,
    *,
    stream: Any = None,
    policy: str = "probabilistic",
    persona: PersonaVersion | None = None,
    rng: random.Random | None = None,
    appraiser: Any = None,
) -> tuple[QQAdapter, FakeSender, CreditStore]:
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_mock_mode=True,
        qq_private_reply_policy=policy,
    )
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    sender = FakeSender()
    adapter = QQAdapter(
        settings,
        sender,
        stream=stream or stream_ok(),
        credits=store,
        gate=AllowAllGate(),
        persona=persona or make_persona(),
        rng=rng or random.Random(0),
        appraiser=appraiser,
    )
    return adapter, sender, store


@async_test
async def test_silent_decision_records_event_and_debt(tmp_path: Any) -> None:
    adapter, sender, _ = make_adapter(
        tmp_path,
        policy="probabilistic",
        persona=make_persona(floor=0.0, ceiling=0.0),
    )

    result = await adapter.handle_event(private_event(10001, "在吗"))

    assert result["reason"] == "silent"
    assert sender.private == []

    conn = memory_db.connect()
    try:
        assert memory_db.stats(conn)["events"] == 1
        decision = conn.execute("SELECT * FROM decision_log").fetchone()
        assert decision["verdict"] == "silent"
        state = conn.execute(
            "SELECT * FROM relationship_state WHERE person_key = 'wp:7'"
        ).fetchone()
        assert state["reply_debt"] == pytest.approx(0.1)
    finally:
        conn.close()


@async_test
async def test_reply_debt_raises_next_probability(tmp_path: Any) -> None:
    rng = SequenceRandom([0.99, 0.0])
    adapter, sender, _ = make_adapter(
        tmp_path,
        persona=make_persona(floor=0.5, ceiling=0.5),
        rng=rng,
    )

    first = await adapter.handle_event(private_event(10001, "在吗", 1))
    second = await adapter.handle_event(private_event(10001, "还在吗", 2))

    assert first["reason"] == "silent"
    assert second.get("reason") is None
    assert sender.private == [(10001, "我在")]

    conn = memory_db.connect()
    try:
        rows = conn.execute("SELECT p FROM decision_log ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[1]["p"] > rows[0]["p"]
    finally:
        conn.close()


@async_test
async def test_new_message_interrupts_generation_and_merges(tmp_path: Any) -> None:
    started = asyncio.Event()
    seen: list[str] = []

    async def blocking_stream(messages: Any, **kwargs: Any) -> Any:
        seen.append(messages[-1].content)
        if len(seen) == 1:
            started.set()
            await asyncio.sleep(30)
        yield {"type": "delta", "content": "我在"}

    adapter, sender, _ = make_adapter(
        tmp_path, stream=blocking_stream, policy="always"
    )

    first = asyncio.create_task(adapter.handle_event(private_event(10001, "第一句", 1)))
    await started.wait()
    second = asyncio.create_task(adapter.handle_event(private_event(10001, "第二句", 2)))

    first_result, _ = await asyncio.gather(first, second)

    assert first_result["reason"] == "interrupted"
    assert seen == ["第一句", "第一句\n第二句"]
    assert sender.private == [(10001, "我在")]


@async_test
async def test_interrupted_partial_is_not_kept_in_history(tmp_path: Any) -> None:
    """打断时没发出去的那半句不能进历史。

    用户从没看到它；如果写进历史，下一轮模型会以为第一句已经答过，
    只接着最后一句说。
    """
    seen: list[list[tuple[str, str]]] = []
    started = asyncio.Event()

    async def partial_then_block(messages: Any, **kwargs: Any) -> Any:
        seen.append([(message.role, message.content) for message in messages])
        if len(seen) == 1:
            yield {"type": "delta", "content": "我在"}
            started.set()
            await asyncio.sleep(30)
        yield {"type": "delta", "content": "在的"}

    adapter, sender, _ = make_adapter(
        tmp_path, stream=partial_then_block, policy="always"
    )

    first = asyncio.create_task(adapter.handle_event(private_event(10001, "第一句", 1)))
    await started.wait()
    second = asyncio.create_task(adapter.handle_event(private_event(10001, "第二句", 2)))

    await asyncio.gather(first, second)

    assert seen[1] == [("user", "第一句\n第二句")]
    assert sender.private == [(10001, "在的")]


@async_test
async def test_relationship_failure_still_replies(tmp_path: Any, monkeypatch: Any) -> None:
    def broken_connect(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("memory db is down")

    monkeypatch.setattr(memory_db, "connect", broken_connect)
    adapter, sender, _ = make_adapter(tmp_path)

    result = await adapter.handle_event(private_event(10001, "在吗"))

    assert result["handled"] is True
    assert result.get("reason") is None
    assert sender.private == [(10001, "我在")]


@async_test
async def test_crisis_words_reply_even_when_policy_is_silent(tmp_path: Any) -> None:
    adapter, sender, _ = make_adapter(tmp_path, policy="silent")

    result = await adapter.handle_event(private_event(10001, "救命"))

    assert result.get("reason") is None
    assert sender.private == [(10001, "我在")]


def capturing_stream(captured: list[Any], text: str = "我在") -> Any:
    async def _stream(messages: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("state_context"))
        yield {"type": "delta", "content": text}

    return _stream


@async_test
async def test_betrayal_changes_same_round_prompt(tmp_path: Any) -> None:
    captured: list[Any] = []
    appraiser = FakeAppraiser(
        '{"event":"betrayal","intensity":0.9,"confidence":0.9,"reply_bias":0}'
    )
    adapter, sender, _ = make_adapter(
        tmp_path,
        stream=capturing_stream(captured),
        rng=random.Random(1),
        appraiser=appraiser,
    )

    await adapter.handle_event(private_event(10001, "我骗了你"))

    assert sender.private == [(10001, "我在")]
    assert captured and captured[0] is not None
    assert "受伤" in captured[0]


@async_test
async def test_withdrawing_still_replies_to_crisis(tmp_path: Any) -> None:
    persona = make_persona()
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        state = get_or_create_state(
            conn, "merlin", "wp:7", role="normal", dynamics=persona.dynamics
        )
        save_state(
            conn,
            replace(
                state,
                conflict_stage="withdrawing",
                stage_since=state.updated_at,
            ),
        )
    finally:
        conn.close()

    captured: list[Any] = []
    appraiser = FakeAppraiser(
        '{"event":"care","intensity":0.9,"confidence":0.9,"reply_bias":0}'
    )
    adapter, sender, _ = make_adapter(
        tmp_path,
        stream=capturing_stream(captured),
        policy="silent",
        persona=persona,
        appraiser=appraiser,
    )

    result = await adapter.handle_event(private_event(10001, "救命"))

    assert result["handled"] is True
    assert sender.private == [(10001, "我在")]
    assert captured and "有些退缩" in captured[0]
