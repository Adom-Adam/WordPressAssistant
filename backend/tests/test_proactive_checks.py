"""主动消息发出前的几道闸门。

每一条都对应一种“发出去会让人不舒服”的具体情形：半夜吵醒人、
刚说完话又来一句、一天发太多次、同一句话反复说。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.memory import db
from app.proactive.checks import (
    in_quiet_hours,
    is_repetitive,
    over_limit,
    recently_active,
    too_soon,
)

NOW = "2026-11-09T15:30:00+00:00"  # 北京时间 23:30


def test_quiet_hours_wrap_midnight() -> None:
    assert in_quiet_hours(NOW, ["23:00-07:00"]) is True
    # 北京 06:59
    assert in_quiet_hours("2026-11-09T22:59:00+00:00", ["23:00-07:00"]) is True
    # 北京 12:00
    assert in_quiet_hours("2026-11-09T04:00:00+00:00", ["23:00-07:00"]) is False
    # 北京 04:00
    assert in_quiet_hours("2026-11-09T20:00:00+00:00", ["00:00-07:00"]) is True


def test_quiet_hours_range_without_wrap() -> None:
    # 北京 13:00
    assert in_quiet_hours("2026-11-09T05:00:00+00:00", ["12:00-14:00"]) is True
    # 北京 11:00
    assert in_quiet_hours("2026-11-09T03:00:00+00:00", ["12:00-14:00"]) is False


def test_quiet_hours_ignores_broken_ranges() -> None:
    """人设文件里写错一个区间不该让主动消息整体失效。"""
    assert in_quiet_hours(NOW, ["23点-7点", "23:00-07:00"]) is True
    assert in_quiet_hours(NOW, ["23点-7点"]) is False


def test_too_soon_zero_means_unlimited() -> None:
    assert too_soon(NOW, NOW, 0) is False
    assert too_soon(None, NOW, 180) is False


def test_too_soon_minutes() -> None:
    assert too_soon("2026-11-09T15:00:00+00:00", NOW, 180) is True
    assert too_soon("2026-11-09T11:00:00+00:00", NOW, 180) is False


def test_over_limit_zero_means_unlimited() -> None:
    assert over_limit(999, 0) is False
    assert over_limit(3, 3) is True
    assert over_limit(2, 3) is False


def test_is_repetitive() -> None:
    assert is_repetitive("晚上好呀，今天累不累", ["晚上好呀，今天累不累"]) is True
    assert is_repetitive("明天一起吃饭吗", ["今天天气不错"]) is False
    assert is_repetitive("", ["今天天气不错"]) is False


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(
            memory_db_path=str(tmp_path / "m.sqlite3"), embedding_dim=4
        ),
    )
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def test_recently_active_only_counts_user_messages(conn) -> None:
    from app.memory.events import record_event

    record_event(
        conn,
        person_key="wp:7",
        platform="qq",
        channel_id="private:10001",
        scope="personal",
        audience=["wp:7"],
        source="user_message",
        content="在吗",
        occurred_at="2026-11-09T15:25:00+00:00",
    )

    assert (
        recently_active(
            conn,
            person_key="wp:7",
            channel_id="private:10001",
            now=NOW,
            minutes=10,
        )
        is True
    )
    assert (
        recently_active(
            conn,
            person_key="wp:7",
            channel_id="private:10001",
            now=NOW,
            minutes=3,
        )
        is False
    )
    assert (
        recently_active(
            conn,
            person_key="wp:7",
            channel_id="private:99999",
            now=NOW,
            minutes=10,
        )
        is False
    )
