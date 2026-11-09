"""群里被指名时，也走一次关系判断。

群聊和私聊的分界：只有“叫到梅林”的消息才进入判断（@ 或前缀），
普通聊天既不进概率模型，也不会被沉默。被指名时仍然用私聊的 p0 公式，
但把概率抬到 `dynamics.group.reply_floor`（默认 0.90）——群里有别人
看着，点名不回很难解释；只有极端状态才可能真的不回。

另一条硬规则：群聊不写关系、不加回复债。群里说一句话的分量和私聊
单独找她说话不一样，混在一起会把好感刷得很快。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import pytest

from app.channels.qq.adapter import QQAdapter
from app.channels.qq.group_policy import GroupPolicy, save_policy
from app.config import Settings, get_settings
from app.credits import CreditStore, hash_bind_code
from app.memory import db as memory_db
from app.memory.events import record_event
from app.relationship.state import RelationshipState, save_state

NOW = "2026-11-09T04:00:00+00:00"
CODE = "1234-5678"


@pytest.fixture(autouse=True)
def isolation(tmp_path: Any, monkeypatch: Any) -> Any:
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
        return "ok", None

    def refund(self, usage: Any) -> None:
        return None


class CapturingStream:
    """记住最后一次调用的消息和参数，用来检查群聊提示词里有什么。"""

    def __init__(self, text: str = "在呢") -> None:
        self.text = text
        self.messages: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, messages: Any, **kwargs: Any) -> Any:
        self.messages.append(messages)
        self.kwargs.append(kwargs)

        async def _gen():
            yield {"type": "delta", "content": self.text}

        return _gen()


class SequenceRandom:
    """按给定序列返回随机数，让“回 / 不回”可以确定地复现。"""

    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.index = 0

    def random(self) -> float:
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return value


def group_event(
    text: str, *, at: bool = False, uid: int = 10001, mid: int = 1
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = [{"type": "text", "data": {"text": text}}]
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


def seed_person(
    tmp_path: Any,
    *,
    affection: float = 60.0,
    valence: float = 0.3,
    energy: float = 0.7,
    conflict_stage: str = "normal",
    trigger: str = "mention",
) -> CreditStore:
    store = CreditStore(str(tmp_path / "quota.sqlite3"))
    store.set_bind_code(7, hash_bind_code(CODE), is_admin=False)
    store.bind_qq("10001", hash_bind_code(CODE))
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        save_state(
            conn,
            RelationshipState(
                persona_key="merlin",
                person_key="wp:7",
                affection=affection,
                missing=40.0,
                security=80.0,
                patience=80.0,
                valence=valence,
                energy=energy,
                fatigue=0.0,
                reply_debt=0.0,
                last_interaction_at=None,
                last_proactive_at=None,
                last_reply_at=None,
                attention_confidence=0.0,
                updated_at=NOW,
                conflict_stage=conflict_stage,
                stage_since=NOW,
                scar_level=0.0,
            ),
        )
        conn.execute(
            """INSERT INTO persons (person_key, wp_user_id, qq_uin,
                   first_seen_at, last_seen_at)
               VALUES ('wp:7', 7, '10001', ?, ?)""",
            (NOW, NOW),
        )
        save_policy(conn, 888, GroupPolicy(trigger, "always", False))
        conn.commit()
    finally:
        conn.close()
    return store


def make_adapter(
    tmp_path: Any,
    *,
    sender: FakeSender | None = None,
    stream: CapturingStream | None = None,
    rng: SequenceRandom | None = None,
    store: CreditStore | None = None,
) -> QQAdapter:
    settings = Settings(
        qq_enabled=True,
        qq_allowed_groups="888",
        qq_mock_mode=True,
        qq_trigger="at",
        qq_prefix="梅林",
        quota_timezone="Asia/Shanghai",
    )
    return QQAdapter(
        settings,
        sender or FakeSender(),
        stream=stream or CapturingStream(),
        credits=store or CreditStore(str(tmp_path / "quota.sqlite3")),
        gate=AllowAllGate(),
        rng=rng or SequenceRandom([0.0]),
    )


def decision_rows() -> list[Any]:
    conn = memory_db.connect()
    try:
        return conn.execute("SELECT * FROM decision_log ORDER BY id").fetchall()
    finally:
        conn.close()


@async_test
async def test_low_roll_replies_with_group_state_context(tmp_path: Any) -> None:
    store = seed_person(tmp_path)
    stream = CapturingStream("在呢，刚看到")
    adapter = make_adapter(
        tmp_path, stream=stream, store=store, rng=SequenceRandom([0.0])
    )

    await adapter.handle_event(group_event("你好", at=True))

    context = stream.kwargs[-1]["state_context"]
    assert context is not None and "群聊" in context
    row = decision_rows()[0]
    assert row["channel_id"] == "group:888"
    assert row["verdict"] == "reply"


@async_test
async def test_high_roll_stays_silent_but_records(tmp_path: Any) -> None:
    store = seed_person(tmp_path, affection=-20.0, valence=-0.5, energy=0.2)
    sender = FakeSender()
    adapter = make_adapter(
        tmp_path, sender=sender, store=store, rng=SequenceRandom([0.99])
    )

    await adapter.handle_event(group_event("你好", at=True))

    assert sender.group == []
    assert decision_rows()[0]["verdict"] == "silent"
    conn = memory_db.connect()
    try:
        contents = [row["content"] for row in conn.execute("SELECT content FROM events")]
    finally:
        conn.close()
    assert contents == ["你好"]


@async_test
async def test_warm_state_almost_always_replies(tmp_path: Any) -> None:
    """好感正常时，即使规则算出来的 p 不高，群下限也会把它抬到 0.90。

    普通一句“你好”的规则概率只有 0.55 左右（没有问句、没有叫名字），
    换成私聊只有一半机会接；在群里被点名，下限把它抬到 0.90。
    """
    store = seed_person(tmp_path)  # 好感 60、正常阶段
    sender = FakeSender()
    adapter = make_adapter(
        tmp_path, sender=sender, store=store, rng=SequenceRandom([0.85])
    )

    await adapter.handle_event(group_event("你好", at=True))

    assert len(sender.group) == 1
    row = decision_rows()[0]
    assert row["p0"] < 0.90
    assert row["p"] == pytest.approx(0.90)


@async_test
async def test_extreme_state_replies_at_floor(tmp_path: Any) -> None:
    store = seed_person(
        tmp_path,
        affection=-20.0,
        valence=-0.5,
        energy=0.2,
        conflict_stage="withdrawing",
    )
    sender = FakeSender()
    adapter = make_adapter(
        tmp_path, sender=sender, store=store, rng=SequenceRandom([0.89])
    )

    await adapter.handle_event(group_event("你好", at=True))

    assert len(sender.group) == 1


@async_test
async def test_prefix_counts_as_addressed(tmp_path: Any) -> None:
    store = seed_person(tmp_path)
    adapter = make_adapter(tmp_path, store=store, rng=SequenceRandom([0.0]))

    await adapter.handle_event(group_event("梅林 你好"))

    assert len(decision_rows()) == 1


@async_test
async def test_plain_text_under_all_is_not_addressed(tmp_path: Any) -> None:
    store = seed_person(tmp_path, trigger="all")
    sender = FakeSender()
    adapter = make_adapter(
        tmp_path, sender=sender, store=store, rng=SequenceRandom([0.0])
    )

    await adapter.handle_event(group_event("今天吃什么"))

    # 没被指名：不进概率模型、不写决策，但仍然正常回复。
    assert decision_rows() == []
    assert len(sender.group) == 1


@async_test
async def test_private_content_never_reaches_group_prompt(tmp_path: Any) -> None:
    store = seed_person(tmp_path)
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        record_event(
            conn,
            person_key="wp:7",
            platform="qq",
            channel_id="private:10001",
            scope="personal",
            audience=["wp:7"],
            source="user_message",
            content="我的银行卡密码是 998877",
            occurred_at=NOW,
        )
        conn.commit()
    finally:
        conn.close()

    stream = CapturingStream("在呢")
    adapter = make_adapter(
        tmp_path, stream=stream, store=store, rng=SequenceRandom([0.0])
    )

    await adapter.handle_event(group_event("你好", at=True))

    blob = " ".join(str(item) for item in stream.messages[-1])
    blob += " " + str(stream.kwargs[-1])
    assert "998877" not in blob


@async_test
async def test_relationship_failure_falls_back_to_reply(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = seed_person(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("app.channels.qq.adapter.load_group_signals", boom)
    sender = FakeSender()
    adapter = make_adapter(
        tmp_path, sender=sender, store=store, rng=SequenceRandom([0.99])
    )

    await adapter.handle_event(group_event("你好", at=True))

    # 判断层坏了不能表现成“她不理人”：fail-open，照常回复。
    assert len(sender.group) == 1


@async_test
async def test_group_round_does_not_change_affection(tmp_path: Any) -> None:
    store = seed_person(tmp_path)
    adapter = make_adapter(tmp_path, store=store, rng=SequenceRandom([0.0]))

    await adapter.handle_event(group_event("你好", at=True))

    conn = memory_db.connect()
    try:
        row = conn.execute(
            "SELECT affection FROM relationship_state WHERE person_key = 'wp:7'"
        ).fetchone()
    finally:
        conn.close()
    assert row["affection"] == pytest.approx(60.0)
