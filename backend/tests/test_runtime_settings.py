"""运行时设置的读写与校验测试。

重点：
1. 没有文件时使用 .env 默认值；
2. 保存后能持久化，并立即被下一次 load 读到；
3. 非法值整体拒绝，不会留下半套配置；
4. 文件损坏时回退默认值，聊天接口不会因此启动失败。
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.runtime_settings import RuntimeSettingsStore


def make_store(tmp_path, **overrides) -> RuntimeSettingsStore:
    settings = Settings(**overrides)
    return RuntimeSettingsStore(str(tmp_path / "runtime.json"), settings)


def test_defaults_come_from_env(tmp_path) -> None:
    store = make_store(tmp_path, daily_quota=37, global_daily_quota=500)

    runtime = store.load()

    assert runtime.daily_quota == 37
    assert runtime.global_daily_quota == 500


def test_update_persists_and_returns_full_snapshot(tmp_path) -> None:
    store = make_store(tmp_path)

    updated = store.update({"daily_quota": 5, "global_daily_quota": 100})
    assert updated.daily_quota == 5
    assert updated.global_daily_quota == 100

    # 新实例模拟进程重启：值应该还在。
    again = make_store(tmp_path)
    assert again.load().daily_quota == 5
    assert again.load().global_daily_quota == 100


def test_invalid_value_rejects_whole_patch(tmp_path) -> None:
    store = make_store(tmp_path)
    store.update({"daily_quota": 8})

    with pytest.raises(ValueError):
        store.update({"daily_quota": 9, "agent_max_tool_calls": 999})

    # 整批拒绝：daily_quota 不能被改成 9。
    assert store.load().daily_quota == 8


def test_unknown_field_is_rejected(tmp_path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.update({"global_quota": 10})  # 少写了 daily


def test_corrupt_file_falls_back_to_defaults(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{not json", encoding="utf-8")
    store = make_store(tmp_path)

    runtime = store.load()

    assert runtime == store.defaults


def test_single_invalid_field_only_falls_back_that_field(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps({"daily_quota": 6, "agent_max_seconds": -1}),
        encoding="utf-8",
    )
    store = make_store(tmp_path, agent_max_seconds=90)

    runtime = store.load()

    assert runtime.daily_quota == 6
    assert runtime.agent_max_seconds == 90


def test_bool_is_not_accepted_as_int(tmp_path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.update({"daily_quota": True})
