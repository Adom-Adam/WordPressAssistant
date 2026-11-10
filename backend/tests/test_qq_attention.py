"""私聊注意力：用户这次说话了，就记“刚聊过”。

主动消息要判断“这个人现在是不是正在聊”，靠的就是这个字段。
它和心情、好感不同，不累加、不衰减到中间值：只要用户来了消息就置 1，
之后按时间衰减。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import pytest

from app.channels.qq.adapter import QQAdapter
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db
from app.qq_quota import QQUsage


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


def private_event(qq_uin: int = 10001, text: str = "在吗", message_id: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": qq_uin,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def make_adapter(tmp_path: Any) -> tuple[QQAdapter, FakeSender]:
    settings = Settings(
        qq_enabled=True,
        qq_mock_mode=True,
        qq_private_reply_policy="silent",
    )
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    sender = FakeSender()
    adapter = QQAdapter(settings, sender, credits=store, gate=AllowAllGate())
    return adapter, sender


def read_attention() -> float:
    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT attention_confidence FROM relationship_state WHERE person_key = 'wp:7'"
        ).fetchone()
        return float(row["attention_confidence"])
    finally:
        conn.close()


@async_test
async def test_private_message_sets_attention_one(tmp_path: Any) -> None:
    adapter, sender = make_adapter(tmp_path)

    result = await adapter.handle_event(private_event())

    # silent 策略下不说话，但“人刚来过”这件事必须记下来。
    assert result["reason"] == "silent"
    assert sender.private == []
    assert read_attention() == pytest.approx(1.0)


@async_test
async def test_tired_silence_leaves_a_pending_topic(tmp_path: Any) -> None:
    """累到没接话时，把这句话留成待续话题——以后的主动消息才有话说。"""
    from app.relationship.state import RelationshipState, now_iso, save_state

    # 时间戳必须取“现在”：疲劳按小时半衰，写死一个过去的时间点，
    # 隔一天再跑疲劳就已经衰减光了，测试会随日期变红。
    stamp = now_iso()

    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        save_state(
            conn,
            RelationshipState(
                persona_key="merlin",
                person_key="wp:7",
                affection=30.0,
                missing=40.0,
                security=70.0,
                patience=70.0,
                valence=0.0,
                energy=0.1,
                # 精力由“小时曲线 − 疲劳”算出，所以直接把疲劳拉满。
                fatigue=1.0,
                reply_debt=0.0,
                last_interaction_at=None,
                last_proactive_at=None,
                last_reply_at=None,
                attention_confidence=0.0,
                updated_at=stamp,
                conflict_stage="normal",
                stage_since=stamp,
                scar_level=0.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    adapter, sender = make_adapter(tmp_path)
    result = await adapter.handle_event(private_event(text="今天面试怎么样"))

    assert result["reason"] == "silent"
    assert sender.private == []
    conn = memory_db.connect()
    try:
        row = conn.execute("SELECT * FROM pending_topics").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["topic"] == "今天面试怎么样"
    assert row["reason"] == "tired"


@async_test
async def test_crisis_message_also_sets_attention(tmp_path: Any) -> None:
    """危机词会绕过 silent 策略；注意力同样要记，否则一句“救命”之后
    反而被当成“没人来过”，主动消息可能马上打扰。"""
    adapter, sender = make_adapter(tmp_path)

    result = await adapter.handle_event(private_event(text="救命"))

    assert result["handled"] is True
    assert sender.private  # 危机词必须回复
    # 回复路径会在置 1 之后再走一次 decay，落盘时会掉一点点，但仍是“刚刚聊过”。
    assert read_attention() > 0.99
