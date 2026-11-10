"""后台可编辑的 QQ 文案。

站长改文案时最容易踩的两件事：

1. 留空应该恢复代码默认值，而不是发一条空消息；
2. 文案里出现花括号（颜文字、代码片段）不能让回复 500——
   所以占位符替换用 str.replace，不用 str.format。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.channels.qq import commands
from app.channels.qq.copy import QQCopy
from app.config import Settings
from app.runtime_settings import RuntimeSettings, RuntimeSettingsStore


def defaults(tmp_path) -> RuntimeSettings:
    return RuntimeSettingsStore(str(tmp_path / "runtime.json"), Settings()).defaults


def test_empty_override_falls_back_to_code_default(tmp_path) -> None:
    copy = QQCopy.from_runtime(defaults(tmp_path), place="/blog")

    assert copy.private_guide == commands.private_guide("/blog")
    assert copy.group_guide == commands.GROUP_GUIDE
    assert copy.anon_quota_user == commands.ANON_QUOTA_USER
    assert copy.anon_quota_global == commands.ANON_QUOTA_GLOBAL
    assert copy.anon_group_ask == commands.ANON_GROUP_ASK
    assert copy.anon_first_hint == commands.anon_first_hint("/blog")
    assert copy.anon_memory_hint == commands.anon_memory_hint("/blog")


def test_override_wins_and_place_is_substituted(tmp_path) -> None:
    runtime = replace(defaults(tmp_path), copy_private_guide="  来吧，登记处在 {place}  ")

    copy = QQCopy.from_runtime(runtime, place="/library")

    # {place} 渲染成“，地方在这里：<站点>/”，不是裸替换成站点地址。
    assert copy.private_guide.startswith("来吧，登记处在")
    assert "/library/" in copy.private_guide
    assert "{place}" not in copy.private_guide


def test_stray_braces_survive(tmp_path) -> None:
    runtime = replace(defaults(tmp_path), copy_anon_quota_user="明天再来 {>_<}")

    copy = QQCopy.from_runtime(runtime, place="")

    assert copy.anon_quota_user == "明天再来 {>_<}"


def test_overlong_override_is_rejected(tmp_path) -> None:
    store = RuntimeSettingsStore(str(tmp_path / "runtime.json"), Settings())

    with pytest.raises(ValueError, match="copy_group_guide"):
        store.update({"copy_group_guide": "字" * 501})


def test_copy_roundtrip(tmp_path) -> None:
    store = RuntimeSettingsStore(str(tmp_path / "runtime.json"), Settings())

    store.update({"copy_anon_group_ask": "来登记一下"})

    assert store.load().copy_anon_group_ask == "来登记一下"
    # 留空 = 恢复默认
    assert store.update({"copy_anon_group_ask": ""}).copy_anon_group_ask == ""
