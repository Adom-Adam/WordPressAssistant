"""本地假 NapCat：接收后端发来的 OneBot 动作，并打印/落盘。

用途
----
Windows 本地没有 NapCat，但想验证：
- 后端是否能正常调用 `send_group_msg`；
- 回复内容、群号、拆条是否符合预期；
- 整个“事件 → agent → 发送”链路是否通。

它不连接 QQ，也不会登录任何账号。所有消息只写到
`backend/data/qq_mock_outbox.jsonl`（路径可用 QQ_MOCK_OUTBOX 改）。

启动
----
在 backend/ 目录：

    uv run python scripts/mock_onebot.py

默认监听 127.0.0.1:3000。然后让后端 `.env` 指向它：

    QQ_ENABLED=true
    QQ_ONEBOT_BASE_URL=http://127.0.0.1:3000
    QQ_MOCK_MODE=false          # 让后端真的发 HTTP 到这个假服务
    QQ_ALLOWED_GROUPS=你的测试群号
    QQ_EVENT_TOKEN=dev-token

再用 `scripts/qq_simulate.py` 伪造一条群消息发给后端。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock NapCat (OneBot 11)")

OUTBOX = Path(os.getenv("QQ_MOCK_OUTBOX", "data/qq_mock_outbox.jsonl"))


def _record(action: str, params: dict[str, Any]) -> None:
    """把调用追加到 JSONL，方便测试脚本和人工排查。"""
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "params": params,
    }
    with OUTBOX.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[mock-napcat] {action}: {json.dumps(params, ensure_ascii=False)}")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-napcat"}


@app.post("/get_status")
async def get_status() -> dict[str, Any]:
    _record("get_status", {})
    return {"status": "ok", "retcode": 0, "data": {"online": True, "good": True}}


@app.post("/get_login_info")
async def get_login_info() -> dict[str, Any]:
    _record("get_login_info", {})
    return {
        "status": "ok",
        "retcode": 0,
        "data": {"user_id": 10000, "nickname": "本地 Mock 梅林"},
    }


@app.post("/send_group_msg")
async def send_group_msg(request: Request) -> JSONResponse:
    try:
        params = await request.json()
    except ValueError:
        params = {}
    _record("send_group_msg", params if isinstance(params, dict) else {})
    return JSONResponse(
        {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": int(time.time() * 1000)},
        }
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("QQ_MOCK_PORT", "3000"))
    print(f"Mock NapCat 监听 http://127.0.0.1:{port}")
    print(f"出站消息写入 {OUTBOX.resolve()}")
    uvicorn.run(app, host="127.0.0.1", port=port)
