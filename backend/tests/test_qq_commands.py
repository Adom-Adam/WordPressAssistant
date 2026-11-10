"""QQ 命令注册表的测试。

命令是唯一"不经过梅林"的输入路径，所以这里重点验证：
1. 四种前缀（半角/全角、正斜杠/反斜杠）都能识别；
2. 普通聊天不会被误判成命令；
3. 未知命令、群里发私聊命令都有确定文案；
4. /帮助 从注册表生成，新增命令不用改它。
"""

from __future__ import annotations

import pytest

from app.channels.qq import commands
from app.channels.qq.commands import CommandContext, dispatch, parse_command
from app.credits import CreditStore, hash_bind_code


@pytest.fixture
def store(tmp_path) -> CreditStore:
    return CreditStore(str(tmp_path / "quota.sqlite3"))


def make_ctx(
    store: CreditStore,
    *,
    qq_uin: int = 10001,
    is_private: bool = True,
    bound_user_id: int | None = None,
    is_admin: bool = False,
    args: str = "",
) -> CommandContext:
    return CommandContext(
        qq_uin=qq_uin,
        is_private=is_private,
        group_id=None if is_private else 999,
        args=args,
        bound_user_id=bound_user_id,
        is_admin=is_admin,
        store=store,
    )


def test_parse_accepts_all_prefixes() -> None:
    for text in ("/绑定 1234-5678", "\\绑定 1234-5678", "／绑定 1234-5678"):
        parsed = parse_command(text)
        assert parsed is not None
        assert parsed.name == "绑定"

    assert parse_command("＼bind 12345678").name == "bind"
    assert parse_command("/BIND 12345678").name == "bind"


def test_parse_rejects_plain_text() -> None:
    assert parse_command("今天天气怎么样") is None
    assert parse_command("/") is None
    assert parse_command("") is None


def test_parse_keeps_args(store: CreditStore) -> None:
    parsed = parse_command("/绑定   1234 5678 ")

    assert parsed is not None
    assert parsed.args == "1234 5678"


def test_unknown_command_does_not_reach_agent(store: CreditStore) -> None:
    reply = dispatch(parse_command("/天气"), make_ctx(store))

    assert "这条咒文我没见过呢" in reply
    assert "绑定" in reply


def test_help_lists_registry(store: CreditStore) -> None:
    reply = dispatch(parse_command("/帮助"), make_ctx(store))

    assert "/绑定" in reply
    assert "1234-5678" in reply


def test_bind_success(store: CreditStore) -> None:
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)

    reply = dispatch(parse_command("/绑定 1234-5678"), make_ctx(store))

    assert reply == commands.BIND_OK
    assert store.qq_uin_of(7) == "10001"


def test_bind_invalid_format(store: CreditStore) -> None:
    assert dispatch(parse_command("/绑定 12"), make_ctx(store)) == commands.BIND_FORMAT


def test_bind_wrong_code(store: CreditStore) -> None:
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)

    assert (
        dispatch(parse_command("/绑定 8765-4321"), make_ctx(store)) == commands.BIND_INVALID
    )


def test_bind_account_taken(store: CreditStore) -> None:
    store.set_bind_code(7, hash_bind_code("3333-3333"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("3333-3333"))

    assert (
        dispatch(parse_command("/绑定 3333-3333"), make_ctx(store, qq_uin=20002))
        == commands.BIND_ACCOUNT_TAKEN
    )


def test_already_bound(store: CreditStore) -> None:
    reply = dispatch(parse_command("/绑定 1234-5678"), make_ctx(store, bound_user_id=7))

    assert reply == commands.BIND_ALREADY


def test_private_only_command_in_group(store: CreditStore) -> None:
    reply = dispatch(parse_command("/绑定 1234-5678"), make_ctx(store, is_private=False))

    assert reply == commands.PRIVATE_ONLY
    assert store.binding_of_qq("10001") is None


# ----------------------------------------------------------------------
# 引导文案：梅林的人设 + 不含“额度”这类系统词
# ----------------------------------------------------------------------
def test_guide_copy_uses_library_metaphor_and_never_says_quota():
    texts = [
        commands.private_guide("https://blog.example"),
        commands.anon_first_hint("https://blog.example"),
        commands.anon_memory_hint("https://blog.example"),
        commands.GROUP_GUIDE,
        commands.ANON_QUOTA_USER,
        commands.ANON_QUOTA_GLOBAL,
        commands.ANON_GROUP_ASK,
    ]
    for text in texts:
        assert "额度" not in text
    assert "图书馆" in commands.private_guide("https://blog.example")
    assert "登记" in commands.private_guide("https://blog.example")
    assert "记住" in commands.anon_first_hint("https://blog.example")


def test_guide_texts_covers_every_user_facing_guide():
    # GUIDE_TEXTS 存的是不带站点地址的默认渲染结果
    assert commands.PRIVATE_GUIDE in commands.GUIDE_TEXTS
    assert commands.GROUP_GUIDE in commands.GUIDE_TEXTS
    assert commands.ANON_QUOTA_USER in commands.GUIDE_TEXTS
