"""每轮私聊评估接入后的端到端行为。

重点验证“评估是增强，不是前提”：
- 评估在掷骰子前发生，决策和事件日志都留下证据；
- 超时/低置信/关闭时回退到规则，回复照常；
- 评估成功时不再叠加通用聊天加成；
- 概率沉默仍然落评估账。
"""

from __future__ import annotations

import asyncio
import functools
import random
from typing import Any

import pytest

from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.qq_quota import QQUsage
from app.relationship.appraisal import AppraisalCall, parse_appraisal
from app.relationship.appraisal_budget import local_day
from app.runtime_settings import get_runtime_store


@pytest.fixture(autouse=True)
def memory_isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json"))
    get_settings.cache_clear()
    get_runtime_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_runtime_store.cache_clear()


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


class FakeAppraiser:
    def __init__(self, call: AppraisalCall) -> None:
        self.call = call
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    async def appraise(self, **kwargs: Any) -> AppraisalCall:
        self.calls += 1
        self.kwargs = kwargs
        return self.call


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
    *, floor: float = 0.02, ceiling: float = 0.95
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
                "reply": {"base": 0.95, "floor": floor, "ceiling": ceiling},
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


def make_adapter(
    tmp_path: Any,
    *,
    appraiser: Any = None,
    policy: str = "probabilistic",
    persona: PersonaVersion | None = None,
    rng: random.Random | None = None,
) -> tuple[QQAdapter, FakeSender]:
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
        stream=stream_ok(),
        credits=store,
        gate=AllowAllGate(),
        persona=persona or make_persona(),
        rng=rng or random.Random(1),
        appraiser=appraiser,
    )
    return adapter, sender


def call_for(payload: str, status: str = "ok") -> AppraisalCall:
    return AppraisalCall(parse_appraisal(payload), status, payload, 5)


def care_call(confidence: float = 1.0) -> AppraisalCall:
    return call_for(
        '{"event":"care","intensity":1,"confidence":%s,"reply_bias":0.2,"reason":"关心"}'
        % confidence
    )


def fetch_all(query: str) -> list[Any]:
    conn = memory_db.connect()
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


@async_test
async def test_private_round_appraises_before_decision(tmp_path: Any) -> None:
    fake = FakeAppraiser(care_call())
    adapter, _ = make_adapter(tmp_path, appraiser=fake)

    await adapter.handle_event(private_event(10001, "你今天辛苦了"))

    assert fake.calls == 1
    row = fetch_all("SELECT * FROM decision_log ORDER BY id DESC LIMIT 1")[0]
    assert row["appraisal_event"] == "care"
    assert row["reply_bias"] == pytest.approx(0.2)
    assert row["appraisal_confidence"] == pytest.approx(1.0)

    log = fetch_all("SELECT * FROM appraisal_log ORDER BY id DESC LIMIT 1")[0]
    assert log["status"] == "ok"
    assert log["event"] == "care"
    assert log["applied_affection"] > 0


@async_test
async def test_appraisal_timeout_falls_back_to_rules(tmp_path: Any) -> None:
    fake = FakeAppraiser(AppraisalCall(None, "timeout", "", 1200))
    adapter, sender = make_adapter(tmp_path, appraiser=fake)

    result = await adapter.handle_event(private_event(10001, "在吗"))

    assert result.get("reason") is None
    assert sender.private == [(10001, "我在")]
    events = fetch_all("SELECT kind, d_affection FROM relationship_events")
    assert [row["kind"] for row in events] == ["chat"]
    assert events[0]["d_affection"] == pytest.approx(0.3)
    log = fetch_all("SELECT * FROM appraisal_log ORDER BY id DESC LIMIT 1")[0]
    assert log["status"] == "timeout"


@async_test
async def test_appraisal_skipped_when_disabled(tmp_path: Any) -> None:
    get_runtime_store().update({"appraisal_enabled": False})
    fake = FakeAppraiser(care_call())
    adapter, _ = make_adapter(tmp_path, appraiser=fake)

    await adapter.handle_event(private_event(10001, "在吗"))

    assert fake.calls == 0
    assert fetch_all("SELECT * FROM appraisal_log") == []


@async_test
async def test_appraisal_skipped_when_policy_silent(tmp_path: Any) -> None:
    fake = FakeAppraiser(care_call())
    adapter, sender = make_adapter(tmp_path, appraiser=fake, policy="silent")

    result = await adapter.handle_event(private_event(10001, "在吗"))

    assert fake.calls == 0
    assert result["reason"] == "silent"
    assert sender.private == []


@async_test
async def test_crisis_skips_appraisal(tmp_path: Any) -> None:
    fake = FakeAppraiser(care_call())
    adapter, sender = make_adapter(tmp_path, appraiser=fake, policy="silent")

    await adapter.handle_event(private_event(10001, "救命"))

    assert fake.calls == 0
    assert sender.private == [(10001, "我在")]


@async_test
async def test_silent_round_still_applies_appraisal(tmp_path: Any) -> None:
    fake = FakeAppraiser(care_call())
    adapter, sender = make_adapter(
        tmp_path,
        appraiser=fake,
        persona=make_persona(floor=0.0, ceiling=0.0),
    )

    result = await adapter.handle_event(private_event(10001, "在吗"))

    assert result["reason"] == "silent"
    assert sender.private == []
    events = fetch_all("SELECT kind, d_affection FROM relationship_events")
    assert [row["kind"] for row in events] == ["ignored", "appraisal"]
    assert events[1]["d_affection"] == pytest.approx(0.6)


@async_test
async def test_successful_appraisal_replaces_chat_affection(tmp_path: Any) -> None:
    fake = FakeAppraiser(care_call())
    adapter, sender = make_adapter(tmp_path, appraiser=fake)

    await adapter.handle_event(private_event(10001, "谢谢你"))

    assert sender.private == [(10001, "我在")]
    state = fetch_all(
        "SELECT * FROM relationship_state WHERE person_key = 'wp:7'"
    )[0]
    # care 事件 1.0 × 角色成长 0.6 = 0.6；不再叠加 chat 的 0.3。
    assert state["affection"] == pytest.approx(0.6)
    events = fetch_all("SELECT kind FROM relationship_events")
    assert [row["kind"] for row in events] == ["appraisal", "chat"]


@async_test
async def test_low_confidence_keeps_baseline_affection(tmp_path: Any) -> None:
    fake = FakeAppraiser(care_call(confidence=0.4))
    adapter, sender = make_adapter(tmp_path, appraiser=fake)

    await adapter.handle_event(private_event(10001, "谢谢你"))

    assert sender.private == [(10001, "我在")]
    events = fetch_all("SELECT kind FROM relationship_events")
    assert [row["kind"] for row in events] == ["chat"]
    state = fetch_all(
        "SELECT * FROM relationship_state WHERE person_key = 'wp:7'"
    )[0]
    assert state["affection"] == pytest.approx(0.3)
    log = fetch_all("SELECT * FROM appraisal_log ORDER BY id DESC LIMIT 1")[0]
    assert log["status"] == "ok"
    assert log["skipped_reason"] == "low_confidence"


@async_test
async def test_appraisal_skipped_when_over_budget(tmp_path: Any) -> None:
    get_runtime_store().update({"appraisal_global_daily_budget": 1})
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            "INSERT INTO appraisal_usage(scope, day, used) VALUES ('global', ?, 1)",
            (local_day(None, "Asia/Shanghai"),),
        )
        conn.commit()
    finally:
        conn.close()

    fake = FakeAppraiser(care_call())
    adapter, sender = make_adapter(tmp_path, appraiser=fake)

    await adapter.handle_event(private_event(10001, "在吗"))

    assert fake.calls == 0
    assert sender.private == [(10001, "我在")]
    log = fetch_all("SELECT * FROM appraisal_log ORDER BY id DESC LIMIT 1")[0]
    assert log["status"] == "over_budget"
