"""本地预览梅林的回答，不需要 WordPress 登录。

====================================================================
为什么可以直接测
====================================================================
生产路径是：

    浏览器 → FastAPI（校验 JWT）→ stream_reply() → DeepSeek

这个脚本跳过前面两层，直接调用 `stream_reply()`：

    终端 → stream_reply() → DeepSeek

所以：
- 不需要 WordPress 账号、Cookie 或 JWT；
- 不会经过 `/api/chat`，也不会占用每日额度；
- 仍然使用 `backend/.env` 里的 DeepSeek Key 和本地知识库。

适合在改完提示词后、上传服务器前快速看真实语气和工具调用。
注意：它会真实调用 DeepSeek，产生少量 token 费用。

====================================================================
用法
====================================================================
在 backend/ 目录执行：

    # 单轮
    uv run python scripts/chat_preview.py "你好呀"

    # 连续多轮（会保留上下文）
    uv run python scripts/chat_preview.py "你好" "这里都有什么" "帮我找一篇关于 DDS 的文章"

    # 不传问题则进入交互模式
    uv run python scripts/chat_preview.py

    # 以普通访客身份测试（默认是管理员/馆长大人）
    uv run python scripts/chat_preview.py --guest "你好"

交互模式里的命令：
    /reset   清空对话历史
    /admin   切换为管理员身份
    /guest   切换为普通访客身份
    exit     退出
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 直接运行 scripts/ xxx.py 时，Python 只会把 scripts/ 加进 sys.path，
# 找不到 backend 目录下的 app 包。这里手动把 backend/ 放到最前面。
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.agent.core import stream_reply  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas import ChatMessage  # noqa: E402

# Windows 控制台默认可能不是 UTF-8，中文会变成乱码。
# reconfigure 只在支持时调用，失败也不影响程序运行。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


async def ask(
    text: str,
    history: list[ChatMessage],
    *,
    is_admin: bool,
    page_url: str,
    page_title: str,
) -> None:
    """发一轮消息并把流式回答打印出来。"""
    history.append(ChatMessage(role="user", content=text))

    print("梅林：", end="", flush=True)
    answer: list[str] = []
    citations: list[dict] = []
    actions: list[dict] = []
    failed = False

    async for event in stream_reply(
        history,
        page_url=page_url,
        page_title=page_title,
        is_admin=is_admin,
    ):
        event_type = event.get("type")
        if event_type == "delta":
            chunk = event.get("content", "")
            answer.append(chunk)
            print(chunk, end="", flush=True)
        elif event_type == "citations":
            citations = list(event.get("citations") or [])
        elif event_type == "action" and event.get("action"):
            actions.append(event["action"])
        elif event_type == "error":
            failed = True
            print(f"\n[错误] {event.get('message', '')}", end="")

    print()

    if citations:
        print("来源：")
        for index, item in enumerate(citations, start=1):
            title = item.get("title") or "（无标题）"
            print(f"  [{index}] {title} — {item.get('url', '')}")

    for action in actions:
        print(f"跳转按钮：{action.get('label') or action.get('url')} ({action.get('url')})")

    # 只有正常产出正文才把 assistant 消息写回历史；
    # 出错时移除这一轮用户消息，避免下一轮带着半截上下文。
    if answer and not failed:
        history.append(ChatMessage(role="assistant", content="".join(answer)))
    else:
        history.pop()


async def interactive(args: argparse.Namespace) -> None:
    history: list[ChatMessage] = []
    is_admin = not args.guest

    print("输入问题开始聊天；/reset 清空历史，/admin、/guest 切换身份，exit 退出。")
    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue
        if text in {"exit", "quit", "/exit", "/quit"}:
            break
        if text == "/reset":
            history.clear()
            print("（对话历史已清空）")
            continue
        if text == "/admin":
            is_admin = True
            print("（当前身份：管理员 / 馆长大人）")
            continue
        if text == "/guest":
            is_admin = False
            print("（当前身份：普通访客）")
            continue

        await ask(
            text,
            history,
            is_admin=is_admin,
            page_url=args.page_url,
            page_title=args.page_title,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="不登录 WordPress，直接预览梅林的提示词效果"
    )
    parser.add_argument("messages", nargs="*", help="要发送的问题；不传则进入交互模式")
    parser.add_argument("--guest", action="store_true", help="以普通访客身份测试")
    parser.add_argument("--page-url", default="http://localhost:5173/", help="模拟当前页面地址")
    parser.add_argument("--page-title", default="本地预览", help="模拟当前页面标题")
    args = parser.parse_args()

    settings = get_settings()
    if settings.mock_enabled:
        print("提示：当前是 mock 模式（没有 Key 或 MOCK_MODE=true），回答是假模型，测不出人设。")
    elif not settings.deepseek_api_key.strip():
        print("提示：没有读到 DeepSeek Key，真实回答可能不可用。")

    if not args.messages:
        await interactive(args)
        return

    # 一次传多个问题时保留上下文，方便观察连续对话是否出戏。
    history: list[ChatMessage] = []
    for text in args.messages:
        print(f"\n你：{text}")
        await ask(
            text,
            history,
            is_admin=not args.guest,
            page_url=args.page_url,
            page_title=args.page_title,
        )


if __name__ == "__main__":
    asyncio.run(main())
