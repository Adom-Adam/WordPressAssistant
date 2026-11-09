from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory import db
from app.relationship.appraisal_budget import (
    day_start_iso,
    local_day,
    reserve_budget,
    used_budget,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def test_local_day_uses_configured_timezone():
    assert local_day("2026-11-09T16:30:00+00:00", "Asia/Shanghai") == "2026-11-10"


def test_day_start_is_local_midnight_in_utc():
    assert day_start_iso(
        "2026-11-09T16:30:00+00:00", "Asia/Shanghai"
    ) == "2026-11-09T16:00:00+00:00"


def test_invalid_timezone_falls_back_to_utc():
    assert local_day("2026-11-09T16:30:00+00:00", "Not/AZone") == "2026-11-09"


def test_reserve_respects_global_limit(conn):
    day = "2026-11-09"
    assert reserve_budget(conn, person_key="wp:1", day=day, global_limit=1, user_limit=10)
    assert not reserve_budget(
        conn, person_key="wp:2", day=day, global_limit=1, user_limit=10
    )


def test_reserve_rolls_back_global_when_user_limit_hits(conn):
    day = "2026-11-09"
    assert reserve_budget(conn, person_key="wp:1", day=day, global_limit=10, user_limit=1)
    assert not reserve_budget(
        conn, person_key="wp:1", day=day, global_limit=10, user_limit=1
    )
    assert used_budget(conn, person_key="wp:1", day=day) == (1, 1)


def test_zero_limit_means_unlimited_but_still_counts(conn):
    day = "2026-11-09"
    for _ in range(3):
        assert reserve_budget(
            conn, person_key="wp:1", day=day, global_limit=0, user_limit=0
        )
    assert used_budget(conn, person_key="wp:1", day=day) == (3, 3)
