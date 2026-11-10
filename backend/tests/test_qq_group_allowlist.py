"""群白名单从环境变量迁到运行时表。

为什么要迁：加一个群要改 `.env` 再重启服务，等于每次都得登服务器。
迁移必须**只播种一次**——否则管理员在后台删掉的群，下次重启又会被
`.env` 里那一行加回来，后台看起来像坏了。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channels.qq import group_allowlist
from app.memory import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
        qq_allowed_group_ids=set(),
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def test_seeds_once_from_env(conn) -> None:
    group_allowlist.ensure_seeded(conn, {111, 222})
    assert group_allowlist.allowed_group_ids(conn) == {111, 222}

    group_allowlist.remove_group(conn, 222)
    # 第二次播种不能把 222 加回来：后台的删除是最终决定。
    group_allowlist.ensure_seeded(conn, {111, 222})
    assert group_allowlist.allowed_group_ids(conn) == {111}


def test_empty_env_seeds_empty_list(conn) -> None:
    group_allowlist.ensure_seeded(conn, set())

    assert group_allowlist.allowed_group_ids(conn) == set()


def test_add_and_remove(conn) -> None:
    group_allowlist.add_group(conn, 333)
    assert group_allowlist.allowed_group_ids(conn) == {333}

    assert group_allowlist.remove_group(conn, 333) is True
    assert group_allowlist.remove_group(conn, 333) is False
    assert group_allowlist.allowed_group_ids(conn) == set()


def test_env_groups_override_is_used_on_first_seed(conn) -> None:
    """`env_groups` 传什么就播种什么，不偷偷读全局设置。"""
    group_allowlist.allowed_group_ids(conn, env_groups={444})

    assert group_allowlist.allowed_group_ids(conn) == {444}
