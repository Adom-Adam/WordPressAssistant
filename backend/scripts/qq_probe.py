"""NapCat / OneBot 连通性探针。

只做两件事：
1. 调 `get_status`，确认 HTTP API 能连上；
2. 调 `get_login_info`，确认当前登录的是哪个 QQ。

默认**不发消息**。要测试发群消息，必须显式加 `--send-test` 和 `--confirm`，
避免只是检查连通性时误发到真实群里。

在服务器上（NapCat 已启动后）执行：

    cd /opt/l2d-ai/backend
    uv run python scripts/qq_probe.py --base-url http://127.0.0.1:3000

如果要验证发消息：

    uv run python scripts/qq_probe.py \
      --base-url http://127.0.0.1:3000 \
      --send-test 你的测试群号 \
      --text "连通性测试，请忽略" \
      --confirm

token 用 `--token` 或环境变量 `QQ_ONEBOT_TOKEN` 传入，不会打印出来。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 直接运行 scripts/xxx.py 时，Python 只把 scripts/ 加入 sys.path；
# 把 backend/ 插到最前面，才能 `import app`。
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.channels.qq.client import OneBotClient, OneBotError  # noqa: E402


async def probe(base_url: str, token: str, send_group: int | None, text: str) -> int:
    client = OneBotClient(base_url=base_url, token=token)

    print(f"探测 {base_url} ...")
    try:
        status = await client.get_status()
        print(f"get_status: {status}")
        login = await client.get_login_info()
        print(f"get_login_info: {login}")
    except OneBotError as exc:
        print(f"连接失败：{exc}")
        print("请检查：NapCat 是否启动、HTTP 服务/端口是否正确、token 是否一致。")
        return 1

    if send_group is not None:
        try:
            result = await client.send_group_message(send_group, text)
            print(f"send_group_msg: {result}")
        except OneBotError as exc:
            print(f"发送失败：{exc}")
            return 1

    print("连通性检查通过。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="NapCat OneBot 连通性探针")
    parser.add_argument(
        "--base-url",
        default=os.getenv("QQ_ONEBOT_BASE_URL", "http://127.0.0.1:3000"),
        help="NapCat HTTP API 地址",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("QQ_ONEBOT_TOKEN", ""),
        help="OneBot token（不会打印）",
    )
    parser.add_argument(
        "--send-test",
        type=int,
        default=None,
        metavar="GROUP_ID",
        help="向这个群发一条测试消息（必须同时加 --confirm）",
    )
    parser.add_argument("--text", default="连通性测试，请忽略。")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认允许真实发送测试消息",
    )
    args = parser.parse_args()

    if args.send_test is not None and not args.confirm:
        print("拒绝发送：--send-test 需要同时加 --confirm。")
        raise SystemExit(2)

    raise SystemExit(
        asyncio.run(
            probe(
                base_url=args.base_url,
                token=args.token,
                send_group=args.send_test,
                text=args.text,
            )
        )
    )


if __name__ == "__main__":
    main()
