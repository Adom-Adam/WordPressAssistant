"""主动消息也要照顾白名单里没绑定账号的人。

条件只有一条但很关键：**至少私聊过一次**。没有 `last_reply_at` 就主动
发消息，等于梅林先跟一个从没说过话的人搭话——她不会这么做。

发送前还要再查一次名单：候选人算出来之后、消息发出去之前，站长可能刚好
把人移出名单，这时这句话就不该再发出去了。
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db
from app.persona.models import PersonaVersion
from app.proactive import scheduler as scheduler_module
from app.proactive.candidates import candidates as list_candidates
from app.proactive.message import ComposeResult
from app.proactive.scheduler import ProactiveScheduler
from app.qq_whitelist import add_entry, remove_entry
from app.relationship.state import RelationshipState, save_state
from app.runtime_settings import get_runtime_store

_STAMP = datetime.now(timezone.utc).replace(microsecond=0)
NOW = _STAMP.isoformat()
LAST_INTERACTION = (_STAMP - timedelta(days=3)).isoformat()
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
    async def compose(self, **kwargs):
        return ComposeResult("在忙吗？", "ok", 1)


def persona() -> PersonaVersion:
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


def enable_runtime(**overrides) -> None:
    patch = {"proactive_enabled": True}
    patch.update(overrides)
    get_runtime_store().update(patch)


def seed_whitelisted_qq(
    tmp_path, *, qq: str = "12345", last_reply_at: str | None = LAST_INTERACTION
):
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        add_entry(conn, qq, added_by="99")
        conn.execute(
            """INSERT INTO persons (person_key, wp_user_id, qq_uin,
                   first_seen_at, last_seen_at)
               VALUES (?, NULL, ?, ?, ?)""",
            (f"qq:{qq}", qq, NOW, NOW),
        )
        save_state(
            conn,
            RelationshipState(
                persona_key="merlin",
                person_key=f"qq:{qq}",
                affection=80.0,
                missing=90.0,
                security=90.0,
                patience=90.0,
                valence=0.2,
                energy=0.9,
                fatigue=0.0,
                reply_debt=0.2,
                last_interaction_at=LAST_INTERACTION,
                last_proactive_at=None,
                last_reply_at=last_reply_at,
                attention_confidence=1.0,
                updated_at=NOW,
                conflict_stage="normal",
                stage_since=NOW,
                scar_level=0.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def make_scheduler(tmp_path, *, sender=None, store=None):
    settings = Settings(
        qq_enabled=True, qq_mock_mode=True, quota_timezone="Asia/Shanghai"
    )
    return ProactiveScheduler(
        settings,
        sender=sender or FakeSender(),
        credits=store or CreditStore(str(tmp_path / "q.sqlite3")),
        writer=FakeWriter(),
        persona=persona(),
    )


def _candidates():
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        return list_candidates(
            conn,
            persona_key="merlin",
            dynamics=persona().dynamics,
            think_threshold=0.0,
            speak_threshold=0.0,
            now=NOW,
        )
    finally:
        conn.close()


def test_whitelisted_qq_with_private_chat_is_candidate(tmp_path) -> None:
    seed_whitelisted_qq(tmp_path)

    found = _candidates()

    assert [c.person_key for c in found] == ["qq:12345"]
    # 没有博客账号：消息要按 QQ 号发出去。
    assert found[0].wp_user_id is None


def test_whitelisted_qq_without_reply_is_not_candidate(tmp_path) -> None:
    seed_whitelisted_qq(tmp_path, last_reply_at=None)

    assert _candidates() == []


@async_test
async def test_scheduler_sends_to_whitelisted_unbound(tmp_path) -> None:
    seed_whitelisted_qq(tmp_path)
    enable_runtime()
    sender = FakeSender()

    results = await make_scheduler(tmp_path, sender=sender).run_once(now=NOW)

    assert [r.status for r in results] == ["sent"]
    assert sender.private == [(12345, "在忙吗？")]


@async_test
async def test_scheduler_skips_removed_whitelist_before_send(
    tmp_path, monkeypatch
) -> None:
    """名单在“算出候选人”和“真的发出去”之间被改掉，也不能发。"""
    seed_whitelisted_qq(tmp_path)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        stale = list_candidates(
            conn,
            persona_key="merlin",
            dynamics=persona().dynamics,
            think_threshold=0.0,
            speak_threshold=0.0,
            now=NOW,
        )
        remove_entry(conn, "12345")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(scheduler_module, "list_candidates", lambda *a, **kw: stale)

    enable_runtime()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender=sender).run_once(now=NOW)

    assert sender.private == []
    assert [r.reason for r in results] == ["not_whitelisted"]


@async_test
async def test_scheduler_skips_when_whitelisted_qq_gets_bound(tmp_path) -> None:
    """名单里的号后来绑了博客账号：身份变了，别再按 qq: 发。"""
    seed_whitelisted_qq(tmp_path)
    store = CreditStore(str(tmp_path / "q.sqlite3"))
    store.set_bind_code(7, BIND_CODE, False)
    store.bind_qq("12345", BIND_CODE)

    enable_runtime()
    sender = FakeSender()
    results = await make_scheduler(tmp_path, sender=sender, store=store).run_once(
        now=NOW
    )

    assert sender.private == []
    assert [r.reason for r in results] == ["binding_changed"]
