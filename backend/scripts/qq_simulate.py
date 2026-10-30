"""伪造一条 OneBot 群消息，发给本地后端，验证 QQ 通道。

它模拟的是 NapCat 的“反向 HTTP”那一半：把事件 POST 到
`/api/channels/qq/events`。配合 `scripts/mock_onebot.py`：

    # 终端 1：后端
    uv run uvicorn app.main:app --reload --port 18080

    # 终端 2：假 NapCat
    uv run python scripts/mock_onebot.py

    # 终端 3：伪造群消息
    uv run python scripts/qq_simulate.py --text "在吗"

后端处理完会向假 NapCat 发 `send_group_msg`，
在终端 2 和 `backend/data/qq_mock_outbox.jsonl` 里都能看到回复。

默认参数可以通过环境变量改：
- QQ_BACKEND_URL：后端地址，默认 http://127.0.0.1:18080
- QQ_EVENT_TOKEN：和后端 .env 的 QQ_EVENT_TOKEN 一致
- QQ_SELF_ID：机器人的 QQ 号，用于 @ 判断，默认 999
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import httpx


def build_event(
    *,
    text: str,
    group_id: int,
    user_id: int,
    self_id: int,
    message_id: int,
    at_bot: bool,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if at_bot:
        segments.append({"type": "at", "data": {"qq": str(self_id)}})
    segments.append({"type": "text", "data": {"text": f" {text}"}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": self_id,
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": segments,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="伪造一条 OneBot 群消息")
    parser.add_argument("--text", default="你好，梅林", help="消息正文")
    parser.add_argument("--group", type=int, default=123456, help="群号")
    parser.add_argument("--user", type=int, default=456789, help="发送者 QQ 号")
    parser.add_argument("--self-id", type=int, default=int(os.getenv("QQ_SELF_ID", "999")))
    parser.add_argument("--message-id", type=int, default=int(time.time() * 1000))
    parser.add_argument(
        "--no-at",
        action="store_true",
        help="不加 @ 段（测试 prefix 触发方式时用）",
    )
    parser.add_argument(
        "--backend",
        default=os.getenv("QQ_BACKEND_URL", "http://127.0.0.1:18080"),
        help="后端地址",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("QQ_EVENT_TOKEN", ""),
        help="和后端 QQ_EVENT_TOKEN 一致",
    )
    args = parser.parse_args()

    event = build_event(
        text=args.text,
        group_id=args.group,
        user_id=args.user,
        self_id=args.self_id,
        message_id=args.message_id,
        at_bot=not args.no_at,
    )
    headers = {"X-OneBot-Token": args.token} if args.token else {}

    url = args.backend.rstrip("/") + "/api/channels/qq/events"
    try:
        response = httpx.post(url, json=event, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        print(f"请求失败：{exc}")
        print("请确认后端已启动，且 QQ_ENABLED=true。")
        raise SystemExit(1) from None

    print(f"HTTP {response.status_code}")
    if response.text:
        print(response.text)

    if response.status_code == 204:
        print("事件已被接收。现在去看假 NapCat 的终端或 data/qq_mock_outbox.jsonl。")


if __name__ == "__main__":
    main()
