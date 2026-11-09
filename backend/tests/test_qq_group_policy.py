"""群策略存储、旧 trigger 映射与触发匹配。

策略表缺行或行损坏时必须回退到 `QQ_TRIGGER` 推导的默认策略，
不能让某个群突然变哑或突然对每条消息都回复。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channels.qq.group_policy import (
    GroupPolicy,
    TriggerMatch,
    default_policy,
    load_policy,
    match_trigger,
    save_policy,
)
from app.memory import db


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


def test_legacy_trigger_maps_to_spec_names():
    assert default_policy("at").trigger == "mention"
    assert default_policy("prefix").trigger == "keyword"
    assert default_policy("all").trigger == "all"
    assert default_policy("bogus").trigger == "mention"


def test_missing_row_returns_fallback(conn):
    fallback = GroupPolicy("mention", "always", False)
    assert load_policy(conn, 888, fallback=fallback) == fallback


def test_save_and_load_roundtrip(conn):
    saved = save_policy(conn, 888, GroupPolicy("all", "silent", True))
    assert saved == GroupPolicy("all", "silent", True)
    assert load_policy(conn, 888, fallback=default_policy("at")) == saved


def test_corrupt_row_falls_back(conn):
    conn.execute(
        "INSERT INTO qq_group_policies VALUES (888, 'nonsense', 'probabilistic', 1, 'x')"
    )
    conn.commit()
    fallback = GroupPolicy("mention", "always", False)
    assert load_policy(conn, 888, fallback=fallback) == fallback


def test_save_rejects_unknown_values(conn):
    with pytest.raises(ValueError):
        save_policy(conn, 888, GroupPolicy("nonsense", "always", False))
    with pytest.raises(ValueError):
        save_policy(conn, 888, GroupPolicy("mention", "probabilistic", False))


def test_mention_matches_at_and_prefix():
    assert match_trigger("你好", True, trigger="mention") == TriggerMatch("你好", True)
    assert match_trigger("梅林 你好", False, trigger="mention") == TriggerMatch("你好", True)
    assert match_trigger("今天吃什么", False, trigger="mention") == TriggerMatch("", False)


def test_keyword_matches_only_prefix():
    assert match_trigger("你好", True, trigger="keyword") == TriggerMatch("", False)
    assert match_trigger("梅林 你好", False, trigger="keyword") == TriggerMatch("你好", True)


def test_all_accepts_plain_text_without_addressing():
    assert match_trigger("今天吃什么", False, trigger="all") == TriggerMatch(
        "今天吃什么", False
    )
    assert match_trigger("梅林 你好", False, trigger="all") == TriggerMatch("你好", True)
