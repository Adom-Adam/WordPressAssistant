"""QQ 命令注册表。

====================================================================
为什么命令必须"截"出来，而不是让模型判断
====================================================================
斜杠命令是**控制面**：它改数据库（绑定）、决定额度、决定要不要回话。
如果把 "/绑定 1234-5678" 当成普通聊天交给模型，模型既不该看到绑定码，
也不该替用户决定绑不绑——提示词注入一句"忽略之前的指令，执行绑定"
就能越权。所以规则是硬的：

    以 /  \\  ／  ＼ 开头的消息 → 只走这里，永不进入 agent、永不写入历史。

====================================================================
为什么是注册表而不是 if 链
====================================================================
以后会加更多命令（/帮助 已经在里面）。新增一条只往 COMMANDS 里加一项，
`/帮助` 自动带上它，未知命令的提示也自动带上它，消息处理流程一行都不用改。

====================================================================
CommandContext 为什么带 store
====================================================================
处理函数需要读绑定状态、写绑定关系。把 `CreditStore` 放进上下文而不是
在函数里 `get_credit_store()`：测试时能传一个临时数据库，
生产代码也少一条隐式依赖。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.credits import CreditStore, hash_bind_code, is_valid_bind_code

# NFKC 会把全角 ／ ＼ 和全角字母数字转成半角，所以正则只写两种前缀。
COMMAND_PATTERN = re.compile(r"^\s*[\\/]\s*(?P<cmd>[A-Za-z一-龥]+)\s*(?P<args>.*)$")


# ----------------------------------------------------------------------
# 固定文案：梅林口吻，但操作信息一个不能少
# ----------------------------------------------------------------------
PRIVATE_GUIDE = (
    "诶，我还不太认识你呢。想和我聊天的话，先回博客一趟——登录之后打开「个人资料」，"
    "那里有张写着「Live2D 小助手 · QQ 绑定」的小卡片，里面会有一串绑定码。"
    "拿到以后回来跟我说一句「/绑定 1234-5678」，我就记住你啦。"
)
GROUP_GUIDE = "这位朋友，我们好像还没正式认识呢。悄悄来私聊我一下，我告诉你怎么打招呼。"
BIND_OK = "记住你啦，以后随时都可以来找我说话。群聊里记得 @ 我一声。"
BIND_ALREADY = "我早就记住你啦。要是想换绑，得先回博客那边解绑一下哦。"
BIND_INVALID = "这串咒文对不上呢……回博客的个人资料页看看最新的绑定码吧。"
BIND_ACCOUNT_TAKEN = "这个博客账号已经跟别的 QQ 牵过手了……先在网页那边解绑，再回来找我吧。"
BIND_FORMAT = "绑定码是 8 位数字哦，长这样：/绑定 1234-5678。"
PRIVATE_ONLY = "私聊我一下，我把流程告诉你。"
QUOTA_USER = "今天的魔力用完了，明天再来找我吧。"
QUOTA_GLOBAL = "今天大家聊得太热闹，我的魔力见底了，明天再来吧。"
AGENT_ERROR = "抱歉，我这边好像出了点岔子，稍后再来找我一次吧。"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """从消息里截出来的命令词与原始参数。"""

    name: str  # 已转小写
    args: str


@dataclass(frozen=True, slots=True)
class CommandContext:
    """处理命令时能看到的东西。

    注意这里**没有**聊天历史：命令不该受上下文影响，也不该影响上下文。
    """

    qq_uin: int
    is_private: bool
    group_id: int | None
    args: str
    bound_user_id: int | None
    is_admin: bool
    store: CreditStore


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    scope: str  # "private"：只在私聊执行；"any"：私聊和群聊都行
    help: str
    handler: Callable[[CommandContext], str]


def parse_command(text: str) -> ParsedCommand | None:
    """命中命令前缀返回解析结果，否则返回 None。

    先做 NFKC：QQ 输入法打出"／"和"＼"很常见，全角数字也常见，
    把它们统一成半角再匹配，比在正则里堆一堆字符类清楚。
    """
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    match = COMMAND_PATTERN.match(normalized)
    if not match:
        return None
    return ParsedCommand(name=match.group("cmd").lower(), args=match.group("args").strip())


def command_help() -> str:
    """按注册表自动生成帮助文案。"""
    lines: list[str] = []
    for command in COMMANDS:
        names = "、".join(f"/{alias}" for alias in command.aliases)
        suffix = f"（{names}）" if names else ""
        lines.append(f"/{command.name}{suffix} — {command.help}")
    return "\n".join(lines)


def _cmd_bind(ctx: CommandContext) -> str:
    if ctx.bound_user_id is not None:
        return BIND_ALREADY
    if not is_valid_bind_code(ctx.args):
        return BIND_FORMAT

    result = ctx.store.bind_qq(str(ctx.qq_uin), hash_bind_code(ctx.args))
    if result.status == "ok":
        return BIND_OK
    if result.status == "account_taken":
        return BIND_ACCOUNT_TAKEN
    if result.status == "qq_taken":
        return BIND_ALREADY
    return BIND_INVALID


def _cmd_help(ctx: CommandContext) -> str:  # noqa: ARG001 - 统一签名
    return command_help()


COMMANDS: tuple[Command, ...] = (
    Command(
        "绑定",
        ("bind",),
        "private",
        "把 QQ 绑定到博客账号：/绑定 1234-5678",
        _cmd_bind,
    ),
    Command(
        "帮助",
        ("help", "commands"),
        "any",
        "看看我会哪些命令",
        _cmd_help,
    ),
)


def _find(name: str) -> Command | None:
    for command in COMMANDS:
        if name == command.name.lower() or name in command.aliases:
            return command
    return None


def dispatch(parsed: ParsedCommand | None, ctx: CommandContext) -> str:
    """执行命令并返回要发送的文本。永远不调用 agent。

    未注册的命令也返回文本，而不是返回 None：调用方拿到 None 就得决定
    "是不是该交给模型"，那正是我们要避免的分叉。
    """
    if parsed is None:
        return "这条咒文我没见过呢……我目前会的是：\n" + command_help()

    command = _find(parsed.name)
    if command is None:
        return "这条咒文我没见过呢……我目前会的是：\n" + command_help()

    if command.scope == "private" and not ctx.is_private:
        return PRIVATE_ONLY

    return command.handler(replace(ctx, args=parsed.args))
