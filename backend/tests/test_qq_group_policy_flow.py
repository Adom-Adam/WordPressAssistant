"""适配器按群策略决定触发、沉默与记录。

三个开关里最容易出事故的是“没触发就不回但要把消息记进时间线”，
以及策略行损坏时必须回退到 `QQ_TRIGGER` 推导的默认值。
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from app.channels.qq.adapter import QQAdapter
from app.channels.qq.group_policy import GroupPolicy, save_policy
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSender:
    def __init__(self):
        self.group: list[tuple[int, str]] = []
        self.private: list[tuple[int, str]] = []

    async def send_group_message(self, group_id, text):
        self.group.append((group_id, text))

    async def send_private_message(self, user_id, text):
        self.private.append((user_id, text))


class AllowAllGate:
    def reserve(self, **kwargs):
        return "ok", None

    def refund(self, usage):
        return None


class FixedRandom:
    """骰子恒为 0：这些用例只关心触发与策略，不关心概率。"""

    def random(self):
        return 0.0


class CountingStream:
    """记录被调了几次，用来分辨到底是哪一条消息触发了回复。"""

    def __init__(self, text="在的"):
        self.text = text
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1

        async def _gen():
            yield {"type": "delta", "content": self.text}

        return _gen()


def group_event(text, *, at=False, uid=10001, mid=1):
    segments = [{"type": "text", "data": {"text": text}}]
    if at:
        segments.insert(0, {"type": "at", "data": {"qq": "999"}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "group_id": 888,
        "user_id": uid,
        "message_id": mid,
        "message": segments,
    }


def make_adapter(tmp_path, *, policy: GroupPolicy | None = None):
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_mock_mode=True,
        qq_trigger="at",
        qq_prefix="梅林",
    )
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1234-5678"))
    sender = FakeSender()
    stream = CountingStream()
    adapter = QQAdapter(
        settings,
        sender,
        stream=stream,
        credits=store,
        gate=AllowAllGate(),
        rng=FixedRandom(),
    )
    if policy is not None:
        conn = memory_db.connect()
        try:
            memory_db.init_schema(conn)
            save_policy(conn, 888, policy)
        finally:
            conn.close()
    return adapter, sender, stream


def event_count() -> int:
    conn = memory_db.connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    finally:
        conn.close()


@async_test
async def test_mention_policy_ignores_plain_text_but_records_it(tmp_path):
    adapter, sender, _stream = make_adapter(tmp_path)
    result = await adapter.handle_event(group_event("今天吃什么"))
    assert result["reason"] == "not_triggered_or_empty"
    assert sender.group == []
    assert event_count() == 1


@async_test
async def test_mention_policy_accepts_at_or_prefix(tmp_path):
    adapter, sender, _stream = make_adapter(tmp_path)
    await adapter.handle_event(group_event("你好", at=True, mid=1))
    await adapter.handle_event(group_event("梅林 你好", mid=2))
    assert len(sender.group) == 2


@async_test
async def test_keyword_policy_requires_prefix(tmp_path):
    adapter, sender, stream = make_adapter(
        tmp_path, policy=GroupPolicy("keyword", "always", False)
    )
    await adapter.handle_event(group_event("你好", at=True, mid=1))
    await adapter.handle_event(group_event("梅林 你好", mid=2))
    assert stream.calls == 1
    assert sender.group == [(888, "在的")]


@async_test
async def test_silent_policy_records_without_reply(tmp_path):
    adapter, sender, _stream = make_adapter(
        tmp_path, policy=GroupPolicy("all", "silent", False)
    )
    result = await adapter.handle_event(group_event("大家晚上好"))
    assert result["reason"] == "silent"
    assert sender.group == []
    assert event_count() == 1


@async_test
async def test_corrupt_policy_row_falls_back_to_settings(tmp_path):
    adapter, sender, _stream = make_adapter(tmp_path)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute("INSERT INTO qq_group_policies VALUES (888, 'bad', 'bad', 0, 'x')")
        conn.commit()
    finally:
        conn.close()
    await adapter.handle_event(group_event("你好", at=True))
    assert len(sender.group) == 1
