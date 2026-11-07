from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.memory import db, identity, pipeline, retrieval, store
from app.memory.extract import Turn
from app.memory.store import MemoryDraft


def _settings(tmp_path):
    return SimpleNamespace(
        memory_db_path=str(tmp_path / "m.sqlite3"),
        embedding_dim=4,
        memory_enabled=True,
        memory_extract_enabled=False,
        memory_vector_enabled=False,
    )


def _patch(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    # retrieval 有自己的 get_settings 引用，不补的话会去读真实的 data/ 目录。
    monkeypatch.setattr(retrieval, "get_settings", lambda: settings)
    db.init_schema()
    return settings


def test_record_web_turn_writes_two_events(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    turn = pipeline.record_web_turn(42, "我在学 ROS2", "加油")
    assert turn is not None
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT scope, source, content FROM events ORDER BY id"
        ).fetchall()
        assert [(row["source"], row["content"]) for row in rows] == [
            ("user_message", "我在学 ROS2"),
            ("assistant_message", "加油"),
        ]
    finally:
        conn.close()


def test_web_viewer_and_qq_viewer_share_history_after_link(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        store.add_memory(
            conn,
            MemoryDraft(
                person_key="qq:123456",
                type="fact",
                text="在学 ROS2",
                scope="personal",
                audience=["qq:123456"],
            ),
            embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
        )
    finally:
        conn.close()

    binding = SimpleNamespace(user_id=42)
    pipeline.record_qq_turn(123456, None, binding, "在吗", "在的")  # 会建立身份关联
    assert "在学 ROS2" in pipeline.web_memory_context(42, "ROS2")


def test_unbound_qq_uses_qq_key(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    turn = pipeline.record_qq_turn(123456, None, None, "你好", "你好呀")
    assert turn.person_key == "qq:123456"


def test_record_web_turn_swallows_db_errors(tmp_path, monkeypatch) -> None:
    # 把路径指向一个已经存在的目录：sqlite 打不开，走异常分支。
    settings = _settings(tmp_path)
    settings.memory_db_path = str(tmp_path)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    # db 有自己的 get_settings 引用；不补的话会连真实 data/ 目录，测不到异常分支。
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    assert pipeline.record_web_turn(1, "a", "b") is None


def test_extract_turn_writes_memories(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    turn = Turn(
        person_key="wp:42",
        channel="web",
        user_text="我在学 ROS2",
        assistant_text="加油",
    )

    class FakeExtractor:
        async def extract(self, turn):
            return [
                MemoryDraft(
                    person_key=turn.person_key,
                    type="fact",
                    text="在学 ROS2",
                    scope="personal",
                    audience=[turn.person_key],
                )
            ]

    assert asyncio.run(pipeline.extract_turn(turn, extractor=FakeExtractor())) == 1


def test_schedule_extraction_respects_flag(tmp_path, monkeypatch) -> None:
    settings = _patch(monkeypatch, tmp_path)  # memory_extract_enabled=False
    turn = Turn(
        person_key="wp:42", channel="web", user_text="a", assistant_text="b"
    )
    assert pipeline.schedule_extraction(turn) is None
    assert settings.memory_extract_enabled is False


def test_unbound_qq_does_not_read_previous_account_memories(tmp_path, monkeypatch) -> None:
    """解绑后 persons 里仍留着旧关联，但不能继续读旧账号的记忆。"""
    _patch(monkeypatch, tmp_path)
    binding = SimpleNamespace(user_id=42)
    pipeline.record_qq_turn(123456, None, binding, "在吗", "在的")

    conn = db.connect()
    try:
        store.add_memory(
            conn,
            MemoryDraft(
                person_key="wp:42",
                type="fact",
                text="阿东在学 ROS2",
                scope="personal",
                audience=["wp:42"],
            ),
            embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
        )
        viewer = pipeline.qq_viewer(conn, 123456, None, None)
    finally:
        conn.close()

    assert viewer.person_keys == ("qq:123456",)
    assert "阿东在学 ROS2" not in pipeline.qq_memory_context(
        123456, None, None, "ROS2"
    )


def test_rebinding_does_not_leak_previous_account_memories(tmp_path, monkeypatch) -> None:
    """同一个 QQ 换绑到新账号后，不能通过旧的 wp 关联读到前一个账号的记忆。"""
    _patch(monkeypatch, tmp_path)
    pipeline.record_qq_turn(123456, None, SimpleNamespace(user_id=7), "在吗", "在的")

    conn = db.connect()
    try:
        store.add_memory(
            conn,
            MemoryDraft(
                person_key="wp:7",
                type="fact",
                text="旧账号的私事",
                scope="personal",
                audience=["wp:7"],
            ),
            embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
        )
    finally:
        conn.close()

    new_binding = SimpleNamespace(user_id=8)
    pipeline.record_qq_turn(123456, None, new_binding, "你好", "你好呀")

    conn = db.connect()
    try:
        old_links = identity.linked_keys(conn, "wp:7")
        new_links = identity.linked_keys(conn, "wp:8")
    finally:
        conn.close()

    assert old_links == ["wp:7"]
    assert "wp:7" not in new_links
    assert "旧账号的私事" not in pipeline.qq_memory_context(
        123456, None, new_binding, "私事"
    )


def test_replacement_qq_reads_memories_bound_to_wp_account(tmp_path, monkeypatch) -> None:
    """QQ 只是登录方式：换一个新 QQ 绑同一账号，记忆仍跟着 wp 账号走。"""
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        store.add_memory(
            conn,
            MemoryDraft(
                person_key="wp:42",
                type="fact",
                text="阿东在学 ROS2",
                scope="personal",
                audience=["wp:42"],
            ),
            embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
        )
    finally:
        conn.close()

    new_qq_binding = SimpleNamespace(user_id=42)
    assert "阿东在学 ROS2" in pipeline.qq_memory_context(
        99999, None, new_qq_binding, "ROS2"
    )
