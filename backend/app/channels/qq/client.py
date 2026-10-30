"""OneBot 11 HTTP API 客户端。

OneBot 11 是 QQ 机器人框架之间的事实标准协议。NapCat 负责登录 QQ、
收发消息，然后通过两种方式和我们通信：

1. **反向 HTTP（事件）**：NapCat 把收到的消息 POST 到我们的
   `/api/channels/qq/events`。这部分在 `app/api/qq.py` 里。
2. **正向 HTTP（调用）**：我们主动 POST 到 NapCat 的 HTTP API，
   例如 `send_group_msg` 发群消息。这个文件负责这一半。

为什么不直接把 NapCat 的 SDK 引进项目：
- 我们只需要几个 HTTP 动作，`httpx` 已经够用；
- 保持“薄适配器”：换平台（Telegram、Discord）时只换这一层；
- 本地没有 NapCat 时，可以换成 `MockOneBotClient`，把回复写进文件。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import Settings


class OneBotError(RuntimeError):
    """调用 NapCat 失败时抛出，消息可以直接展示给管理员排查。"""


class OneBotTransport(Protocol):
    """适配器真正依赖的最小接口。

    用 Protocol 而不是继承某个基类：测试时传一个普通对象，
    只要它有 `send_group_message` 方法就能用，不需要导入 httpx。
    """

    async def send_group_message(self, group_id: int, message: str) -> Any:
        """向群里发一条文本消息，返回 OneBot 的 data 字段。"""


@dataclass(frozen=True, slots=True)
class OneBotClient:
    """通过 HTTP 调用 NapCat 的 OneBot 11 接口。"""

    base_url: str
    token: str = ""
    timeout: float = 15.0

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """调用一个 OneBot 动作。

        NapCat 的 HTTP API 通常把动作名放在路径里，例如
        `POST http://127.0.0.1:3000/send_group_msg`，请求体就是参数。
        少数实现只支持 `POST /` + `{"action": ..., "params": ...}`，
        这里优先用路径形式，因为 NapCat 两者都支持，路径形式更直观。
        """
        url = f"{self.base_url.rstrip('/')}/{action}"
        headers: dict[str, str] = {}
        if self.token.strip():
            # OneBot 11 的 HTTP 鉴权约定：Authorization: Bearer <token>。
            headers["Authorization"] = f"Bearer {self.token.strip()}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=params or {}, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            # 401 通常表示 token 不一致；404 通常是端口对了但路径/服务不对。
            raise OneBotError(
                f"NapCat 返回 HTTP {exc.response.status_code}（动作：{action}）。"
                "请检查 OneBot token、端口和 NapCat 的 HTTP 服务是否开启。"
            ) from None
        except (httpx.HTTPError, ValueError) as exc:
            # 连接失败、超时、返回的不是 JSON，都归到这里。
            raise OneBotError(f"无法调用 NapCat（动作：{action}）：{exc}") from None

        if not isinstance(data, dict):
            return data

        # OneBot 11 的标准返回：{"status": "ok", "retcode": 0, "data": ...}
        # 有些实现只返回 data，所以 status/retcode 缺失时不当作错误。
        if data.get("status") not in (None, "ok"):
            raise OneBotError(f"NapCat 拒绝了动作 {action}：{data.get('status')}")
        retcode = data.get("retcode")
        if retcode not in (None, 0):
            raise OneBotError(f"NapCat 动作 {action} 失败，retcode={retcode}")
        return data.get("data", data)

    async def send_group_message(self, group_id: int, message: str) -> Any:
        """发送群文本消息。"""
        return await self.call(
            "send_group_msg",
            {"group_id": group_id, "message": message},
        )

    async def get_status(self) -> Any:
        """查询 NapCat 在线状态，用于连通性探测。"""
        return await self.call("get_status", {})

    async def get_login_info(self) -> Any:
        """查询当前登录的 QQ 号/昵称，用于确认登录的是哪个账号。"""
        return await self.call("get_login_info", {})


class MockOneBotClient:
    """本地降级实现：不连 NapCat，把“要发的消息”追加到 JSONL 文件。

    用途：
    - Windows 本地没有 NapCat 时，仍能跑通“收到事件 → 调用 agent → 生成回复”；
    - 测试时断言“到底发了什么”，而不需要起一个假 HTTP 服务；
    - 默认只写 `backend/data/qq_mock_outbox.jsonl`，不会误发到真实 QQ。
    """

    def __init__(self, outbox_path: str | Path) -> None:
        self.outbox_path = Path(outbox_path)

    def _write(self, record: dict[str, Any]) -> None:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """模拟 OneBot 调用，并把调用记录写进 outbox。"""
        params = params or {}
        self._write(
            {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "action": action,
                "params": params,
            }
        )

        if action == "get_login_info":
            return {"user_id": 0, "nickname": "本地 Mock 梅林"}
        if action == "get_status":
            return {"online": True, "good": True}
        if action == "send_group_msg":
            return {"message_id": int(time.time() * 1000)}
        return {}

    async def send_group_message(self, group_id: int, message: str) -> Any:
        return await self.call(
            "send_group_msg",
            {"group_id": group_id, "message": message},
        )

    async def get_status(self) -> Any:
        return await self.call("get_status", {})

    async def get_login_info(self) -> Any:
        return await self.call("get_login_info", {})


def build_onebot_client(settings: Settings) -> OneBotClient | MockOneBotClient:
    """根据配置选真实客户端或本地 mock。"""
    if settings.qq_mock_mode:
        return MockOneBotClient(settings.qq_mock_outbox)
    return OneBotClient(
        base_url=settings.qq_onebot_base_url,
        token=settings.qq_onebot_token,
    )
