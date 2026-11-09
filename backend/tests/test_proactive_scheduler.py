"""私聊主动调度器。

调度器只做决定和记账：谁值得想、这一轮发不发、发了怎么记。生成交给
`ProactiveWriter`，发送交给 OneBot 客户端，两边都可以替换。

这里最重要的一条是**一轮只发一条私聊**：主动消息是稀缺的注意力，
连着给好几个人发会把“想起你”变成群发。
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import replace

import pytest

from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.proactive.message import ComposeResult
from app.proactive.scheduler import ProactiveScheduler
from app.relationship.state import RelationshipState, save_state

NOW = "2026-11-09T04:00:00+00:00"  # 北京时间 12:00
BIND_CODE = hash_bind_code("1234-5678")


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "r.json"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    get_settings.cache_clear()
    from app.runtime_settings import get_runtime_store

    get_runtime_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_runtime_store.cache_clear()


class FakeSender:
    def __init__(self):
        self.private, self.group = [], []

    async def send_private_message(self, uid, text):
        self.private.append((uid, text))

    async def send_group_message(self, gid, text):
        self.group.append((gid, text))


class FailSender(FakeSender):
    async def send_private_message(self, uid, text):
        raise RuntimeError("network down")


class FakeWriter:
    def __init__(self, text="在忙吗？"):
        self.text = text
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        return ComposeResult(self.text, "ok", 1)


def persona():
    return PersonaVersion.model_validate(
        {
            "schema": "l2d.persona/v1",
            "id": "merlin",
            "version": 1,
            "name": "梅林",
            "core": "C",
            "dynamics": {"safety": {"quiet_hours": ["01:00-02:00"]}},
        }
    )


def enable_runtime(**overrides):
    from app.runtime_settings import get_runtime_store

    patch = {"proactive_enabled": True}
    patch.update(overrides)
    get_runtime_store().update(patch)


def seed_person(tmp_path, *, qq="10001", user_id=7, **state_overrides):
    store = CreditStore(str(tmp_path / "q.sqlite3"))
    store.set_bind_code(user_id, BIND_CODE, False)
    store.bind_qq(qq, BIND_CODE)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        base = RelationshipState(
            persona_key="merlin",
            person_key=f"wp:{user_id}",
            affection=80.0,
            missing=90.0,
            security=90.0,
            patience=90.0,
            valence=0.2,
            energy=0.9,
            fatigue=0.0,
            reply_debt=0.2,
            last_interaction_at="2026-11-06T04:00:00+00:00",
            last_proactive_at=None,
            last_reply_at=None,
            attention_confidence=1.0,
            updated_at=NOW,
            conflict_stage="normal",
            stage_since=NOW,
            scar_level=0.0,
        )
        save_state(conn, replace(base, **state_overrides))
        conn.execute(
            """INSERT INTO persons (person_key, wp_user_id, qq_uin,
                   first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            (f"wp:{user_id}", user_id, qq, NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    return store


def make_scheduler(tmp_path, *, sender=None, writer=None, store=None):
    settings = Settings(
        qq_enabled=True, qq_mock_mode=True, quota_timezone="Asia/Shanghai"
    )
    return ProactiveScheduler(
        settings,
        sender=sender or FakeSender(),
        credits=store or CreditStore(str(tmp_path / "q.sqlite3")),
        writer=writer or FakeWriter(),
        persona=persona(),
    )


@async_test
async def test_disabled_sends_nothing(tmp_path):
    store = seed_person(tmp_path)
    scheduler = make_scheduler(tmp_path, store=store)
    assert await scheduler.run_once(now=NOW) == []


@async_test
async def test_sends_one_private_message_and_records(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now=NOW)
    assert len(results) == 1 and results[0].status == "sent"
    assert sender.private == [(10001, "在忙吗？")]
    conn = memory_db.connect()
    try:
        log = conn.execute("SELECT * FROM proactive_log").fetchone()
        assert log["status"] == "sent" and log["target_key"] == "wp:7"
        event = conn.execute(
            "SELECT * FROM events WHERE source='proactive'"
        ).fetchone()
        assert event["channel_id"] == "private:10001"
        state = conn.execute(
            "SELECT last_proactive_at FROM relationship_state WHERE person_key='wp:7'"
        ).fetchone()
        assert state["last_proactive_at"] == NOW
    finally:
        conn.close()


@async_test
async def test_quiet_hours_skips(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(
        now="2026-11-08T17:30:00+00:00"
    )  # 北京 01:30
    assert all(r.status == "skipped" for r in results)
    assert sender.private == []


@async_test
async def test_recently_active_skips(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO events (occurred_at, person_key, platform, channel_id,
                   scope, audience, audience_key, source, content, created_at)
               VALUES (?, 'wp:7', 'qq', 'private:10001', 'personal', '["wp:7"]',
                       'wp:7', 'user_message', '在吗', ?)""",
            (NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    scheduler = make_scheduler(tmp_path, store=store)
    results = await scheduler.run_once(now="2026-11-09T04:05:00+00:00")
    assert results and results[0].status == "skipped"
    assert results[0].reason == "recently_active"


@async_test
async def test_min_interval_skips(tmp_path):
    store = seed_person(
        tmp_path, last_proactive_at="2026-11-09T03:00:00+00:00"
    )
    enable_runtime()
    scheduler = make_scheduler(tmp_path, store=store)
    results = await scheduler.run_once(now=NOW)
    assert results[0].status == "skipped"
    assert results[0].reason == "min_interval"


@async_test
async def test_daily_limit_skips(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime(proactive_per_user_daily_limit=1)
    scheduler = make_scheduler(tmp_path, store=store)
    await scheduler.run_once(now=NOW)
    results = await scheduler.run_once(now="2026-11-09T07:00:00+00:00")
    assert results and all(r.reason == "daily_limit" for r in results)


@async_test
async def test_compose_failure_logs_skipped(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()

    class BadWriter:
        async def compose(self, **kwargs):
            return ComposeResult("", "error", 1)

    sender = FakeSender()
    scheduler = make_scheduler(
        tmp_path, sender=sender, store=store, writer=BadWriter()
    )
    results = await scheduler.run_once(now=NOW)
    assert results[0].status == "skipped"
    assert results[0].reason == "compose_error"
    assert sender.private == []


@async_test
async def test_send_failure_is_logged_not_raised(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    scheduler = make_scheduler(tmp_path, sender=FailSender(), store=store)
    results = await scheduler.run_once(now=NOW)
    assert results[0].status == "failed"
    conn = memory_db.connect()
    try:
        log = conn.execute("SELECT * FROM proactive_log").fetchone()
        assert log["status"] == "failed"
        state = conn.execute(
            "SELECT last_proactive_at FROM relationship_state WHERE person_key='wp:7'"
        ).fetchone()
        assert state["last_proactive_at"] is None
    finally:
        conn.close()


@async_test
async def test_rebound_qq_is_never_messaged(tmp_path):
    store = seed_person(tmp_path)
    enable_runtime()
    store.set_bind_code(8, BIND_CODE, False)
    store.unbind_user(7)
    store.bind_qq("10001", BIND_CODE)
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now=NOW)
    assert sender.private == []
    assert all(r.status == "skipped" for r in results)


@async_test
async def test_one_private_message_per_tick(tmp_path):
    store = seed_person(tmp_path)
    seed_person(tmp_path, qq="10002", user_id=8)
    enable_runtime()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender=sender, store=store)
    results = await scheduler.run_once(now=NOW)
    assert len([r for r in results if r.status == "sent"]) == 1
    assert len(sender.private) == 1
