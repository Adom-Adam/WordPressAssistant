from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.memory import db, maintenance, store
from app.memory.store import MemoryDraft


def _conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    return db.connect()


def _add(conn, **overrides):
    values = dict(
        person_key="wp:12",
        type="fact",
        text="事实",
        scope="personal",
        audience=["wp:12"],
        importance=0.8,
    )
    values.update(overrides)
    # `embedder=None` 会退回全局编码器并加载真实模型；测试用不可用占位符。
    return store.add_memory(
        conn,
        MemoryDraft(**values),
        embedder=SimpleNamespace(available=False, encode=lambda *a, **k: []),
    )


def test_decay_halves_importance_after_one_half_life(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        memory_id = _add(conn)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=30)).isoformat(timespec="seconds")
        conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (old, memory_id))
        conn.commit()

        changed = maintenance.decay_importance(conn, half_life_days=90.0, now=now)
        assert changed == 1
        importance = conn.execute(
            "SELECT importance FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()[0]
        assert 0.55 < importance < 0.70  # 0.8 * 0.5**(30/90) ≈ 0.635
    finally:
        conn.close()


def test_expire_promises_removes_only_outdated(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        past = "2026-01-01T00:00:00+00:00"
        future = "2030-01-01T00:00:00+00:00"
        _add(conn, type="promise", text="过期的约定", valid_until=past)
        keep = _add(conn, type="promise", text="还没到期的约定", valid_until=future)
        removed = maintenance.expire_promises(conn)
        assert removed == 1
        remaining = store.list_memories(conn, "wp:12")
        assert [row["id"] for row in remaining] == [keep]
    finally:
        conn.close()


def test_run_reports_counts(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path, monkeypatch)
    try:
        _add(conn, type="promise", text="过期", valid_until="2026-01-01T00:00:00+00:00")
        result = maintenance.run(conn)
        assert set(result) == {"decayed", "expired"}
        assert result["expired"] == 1
    finally:
        conn.close()


def test_decay_is_not_compounded_by_repeated_runs(tmp_path, monkeypatch) -> None:
    """每天跑一次维护，不能把同一条记忆按“绝对年龄”反复衰减。"""
    conn = _conn(tmp_path, monkeypatch)
    try:
        memory_id = _add(conn)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=30)).isoformat(timespec="seconds")
        conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (old, memory_id))
        conn.commit()

        maintenance.decay_importance(conn, half_life_days=90.0, now=now)
        maintenance.decay_importance(
            conn, half_life_days=90.0, now=now + timedelta(days=1)
        )

        importance = conn.execute(
            "SELECT importance FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()[0]
        expected = 0.8 * 0.5 ** (31 / 90)
        assert abs(importance - expected) < 0.01
    finally:
        conn.close()
