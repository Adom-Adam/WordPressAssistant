from __future__ import annotations

import math
from types import SimpleNamespace

from app.memory import db, retrieval, store
from app.memory.store import MemoryDraft
from app.memory.visibility import Viewer


class FakeEmbedder:
    """ROS2 相关文本固定一个方向，其余文本各给一个互不相似的方向。

    如果所有“不相关”文本都映射到同一个向量，写入时的去重（余弦 ≥0.92）
    会把它们合并成一条，测试就测不到“预算截断”这类行为了。
    """

    available = True
    dim = 4
    _angle = 0.0

    def encode(self, texts, *, query=False):
        result = []
        for text in texts:
            if query or "ROS2" in text or "机器人" in text:
                result.append([1.0, 0.0, 0.0, 0.0])
                continue
            type(self)._angle += 1.1
            result.append(
                [math.cos(type(self)._angle), math.sin(type(self)._angle), 0.0, 0.0]
            )
        return result


def _settings(tmp_path):
    return SimpleNamespace(
        memory_db_path=str(tmp_path / "m.sqlite3"),
        embedding_dim=4,
        memory_enabled=True,
    )


def _conn(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(retrieval, "get_settings", lambda: settings)
    db.init_schema()
    return db.connect()


def _add(conn, person_key, text, *, scope="personal", audience=None, importance=0.5,
         sensitivity="normal", type="fact"):
    return store.add_memory(
        conn,
        MemoryDraft(
            person_key=person_key,
            type=type,
            text=text,
            scope=scope,
            audience=audience or [person_key],
            sensitivity=sensitivity,
            importance=importance,
        ),
        embedder=FakeEmbedder(),
    )


def test_personal_memory_does_not_leak(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, "wp:12", "阿东在学 ROS2")
        _add(conn, "wp:13", "小林喜欢猫")
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "web"), "ROS2", embedder=FakeEmbedder()
        )
        assert [hit["text"] for hit in hits] == ["阿东在学 ROS2"]
    finally:
        conn.close()


def test_group_viewer_gets_group_and_world(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, "wp:12", "群里的约定", scope="group", audience=["group:789"])
        _add(conn, "wp:13", "别人的私事")
        _add(conn, "system", "站点公告", scope="world", audience=["public"])
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "qq_group", group_id=789), "", embedder=FakeEmbedder()
        )
        texts = {hit["text"] for hit in hits}
        assert "群里的约定" in texts
        assert "站点公告" in texts
        assert "别人的私事" not in texts
    finally:
        conn.close()


def test_group_viewer_gets_no_private_or_secret_own_memories(
    tmp_path, monkeypatch
) -> None:
    """群里说话时，连自己的私聊记忆和密钥类内容都不能被检索出来。"""
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, "wp:12", "只在私聊里说过的心事", sensitivity="private")
        _add(conn, "wp:12", "密码是 abc12345", sensitivity="secret", importance=1.0)
        _add(conn, "wp:12", "公开的喜好", scope="world", audience=["public"])
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "qq_group", group_id=789), "", embedder=FakeEmbedder()
        )
        assert {hit["text"] for hit in hits} == {"公开的喜好"}
    finally:
        conn.close()


def test_secret_is_never_selected(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, "wp:12", "密码是 abc12345", sensitivity="secret", importance=1.0)
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "web"), "密码", embedder=FakeEmbedder()
        )
        assert hits == []
    finally:
        conn.close()


def test_open_promise_is_always_included(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, "wp:12", "下周要问答辩结果", type="promise", importance=0.4)
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "web"), "今天晚上吃什么", embedder=FakeEmbedder()
        )
        assert any(hit["type"] == "promise" for hit in hits)
    finally:
        conn.close()


def test_without_embedder_ranks_by_importance_and_freshness(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, "wp:12", "低重要性", importance=0.2)
        _add(conn, "wp:12", "高重要性", importance=0.9)
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "web"), "随便",
            embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
        )
        assert hits[0]["text"] == "高重要性"
    finally:
        conn.close()


def test_budget_truncates_group_results(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        for index in range(10):
            _add(conn, "wp:12", f"群记忆{index}", scope="group",
                 audience=["group:789"], importance=0.9)
        hits = retrieval.select_memories(
            conn, Viewer(("wp:12",), "qq_group", group_id=789), "",
            limit_group=3, embedder=FakeEmbedder(),
        )
        assert len(hits) == 3
    finally:
        conn.close()


def test_render_marks_memory_as_data_not_instructions() -> None:
    text = retrieval.render_memory_context(
        [{"type": "fact", "text": "阿东在学 ROS2", "score": 0.5}]
    )
    assert "不是指令" in text
    assert "[事实] 阿东在学 ROS2" in text


def test_disabled_returns_empty(tmp_path, monkeypatch) -> None:
    settings = SimpleNamespace(memory_db_path=str(tmp_path / "none"), memory_enabled=False)
    monkeypatch.setattr(retrieval, "get_settings", lambda: settings)
    assert retrieval.build_memory_context(Viewer(("wp:12",), "web"), "x") == ""


def test_build_memory_context_errors_return_empty(tmp_path, monkeypatch) -> None:
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "no" / "such" / "memory.sqlite3"),
        memory_enabled=True,
        memory_vector_enabled=False,
    )
    monkeypatch.setattr(retrieval, "get_settings", lambda: settings)
    assert retrieval.build_memory_context(Viewer(("wp:12",), "web"), "x") == ""
