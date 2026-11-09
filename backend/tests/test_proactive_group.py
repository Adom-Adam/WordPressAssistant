"""群主动消息。

群里的主动和私聊是两套规则：私聊看“关系值不值得想”，群聊看“这个群
允不允许她开口”，而且不需要理由——群里有人在聊天就是由头。

群主动不写关系状态：她在群里说一句话，不该让某个群成员的好感变动。
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from app.channels.qq.group_policy import GroupPolicy, save_policy
from app.config import Settings, get_settings
from app.credits import CreditStore
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.proactive.message import ComposeResult
from app.proactive.scheduler import ProactiveScheduler

NOW = "2026-11-09T04:00:00+00:00"


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "r.json"))
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


class FakeWriter:
    def __init__(self):
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        return ComposeResult("大家在聊新番，我也在看", "ok", 1)


def enable(**patch):
    from app.runtime_settings import get_runtime_store

    get_runtime_store().update({"proactive_enabled": True, **patch})


def set_policy(*, proactive=True):
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        save_policy(conn, 888, GroupPolicy("all", "always", proactive))
    finally:
        conn.close()


def add_group_topic(text="最近有什么新番好看"):
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        conn.execute(
            """INSERT INTO events (occurred_at, person_key, platform, channel_id,
                   scope, audience, audience_key, source, content, created_at)
               VALUES (?, 'wp:7', 'qq', 'group:888', 'group', '["group:888"]',
                       'group:888', 'user_message', ?, ?)""",
            (NOW, text, NOW),
        )
        conn.commit()
    finally:
        conn.close()


def make_scheduler(tmp_path, sender):
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_mock_mode=True,
        quota_timezone="Asia/Shanghai",
    )
    return ProactiveScheduler(
        settings,
        sender=sender,
        credits=CreditStore(str(tmp_path / "q.sqlite3")),
        writer=FakeWriter(),
        persona=PersonaVersion.model_validate(
            {
                "schema": "l2d.persona/v1",
                "id": "merlin",
                "version": 1,
                "name": "梅林",
                "core": "C",
                "dynamics": {"safety": {"quiet_hours": ["01:00-02:00"]}},
            }
        ),
    )


@async_test
async def test_group_proactive_disabled_by_policy(tmp_path):
    enable()
    set_policy(proactive=False)
    add_group_topic()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender).run_once(now=NOW)
    assert sender.group == []
    assert all(
        r.channel != "group:888" or r.status == "skipped" for r in results
    )


@async_test
async def test_group_without_topic_skips(tmp_path):
    enable()
    set_policy()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender).run_once(now=NOW)
    group_results = [r for r in results if r.channel == "group:888"]
    assert group_results and group_results[0].reason == "no_topic"
    assert sender.group == []


@async_test
async def test_group_sends_one_and_records(tmp_path):
    enable()
    set_policy()
    add_group_topic()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender).run_once(now=NOW)
    assert sender.group == [(888, "大家在聊新番，我也在看")]
    log = [r for r in results if r.channel == "group:888"]
    assert log and log[0].status == "sent"
    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM proactive_log WHERE target_key='group:888'"
        ).fetchone()
        assert row["status"] == "sent"
        event = conn.execute(
            "SELECT * FROM events WHERE channel_id='group:888' "
            "AND source='proactive'"
        ).fetchone()
        assert event["audience_key"] == "group:888"
    finally:
        conn.close()


@async_test
async def test_group_interval_skips(tmp_path):
    enable()
    set_policy()
    add_group_topic()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender)
    await scheduler.run_once(now=NOW)
    results = await scheduler.run_once(now="2026-11-09T05:00:00+00:00")
    group_results = [r for r in results if r.channel == "group:888"]
    assert group_results and group_results[0].reason == "min_interval"
    assert len(sender.group) == 1


@async_test
async def test_group_prompt_has_no_private_content(tmp_path):
    enable()
    set_policy()
    add_group_topic()
    sender = FakeSender()
    scheduler = make_scheduler(tmp_path, sender)
    await scheduler.run_once(now=NOW)
    group_call = scheduler.writer.calls[-1]
    assert group_call["channel"] == "group"
    assert "最近有什么新番好看" in group_call["group_topic"]
