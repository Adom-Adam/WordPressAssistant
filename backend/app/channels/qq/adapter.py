"""OneBot 事件 → 内部 ChatMessage → agent → 群消息。

设计原则
--------
1. **默认拒绝**：`QQ_ALLOWED_GROUPS` 为空时，任何群消息都不处理。
2. **只处理群聊文本**：私聊、频道、图片、表情都先忽略；等身份绑定做好后再扩展。
3. **用户内容只是数据**：群消息会作为 `ChatMessage(role="user")` 进入模型，
   永远不会拼进 system prompt，也不会改变管理员身份。
4. **幂等**：NapCat 在网络抖动时可能重发同一个 `message_id`，必须去重。
5. **失败可观察**：发送失败只记日志，不让后台任务崩掉整个进程。

这一层刻意保持“薄”：没有数据库、没有长期记忆、没有主动消息。
它先解决“QQ 消息能进来、回复能出去”，身份绑定和持久记忆是后续步骤。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.agent.core import stream_reply
from app.channels.qq.client import OneBotError, OneBotTransport
from app.config import Settings
from app.schemas import ChatMessage

logger = logging.getLogger(__name__)

# 一条 QQ 文本消息最多发多少字符。太长会刷屏，也容易被平台限流。
# 这里不是模型的输出上限，而是“单条群消息”的展示上限；超出的部分拆成多条。
MAX_MESSAGE_CHARS = 1_200

# 去重表最多记多少个 message_id。QQ 群消息量不大，500 足够覆盖重发窗口。
MAX_SEEN_MESSAGES = 500


class QQAdapter:
    """处理 NapCat 推送的 OneBot 事件。"""

    def __init__(
        self,
        settings: Settings,
        sender: OneBotTransport,
        *,
        stream: Callable[..., AsyncIterator[dict[str, Any]]] = stream_reply,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self._stream = stream

        # (群号, QQ 号) -> 最近几轮消息。只在内存里，进程重启就清空。
        # 这是有意的：在账号绑定/关系记忆做好之前，不假装自己有长期记忆。
        self._history: dict[tuple[int, int], list[ChatMessage]] = {}

        # (群号, message_id) -> 时间戳。OrderedDict 的插入顺序帮助我们淘汰最旧的记录。
        # 不用单独的 message_id 做 key：不同群的 ID 理论上可能重复，
        # 只按 ID 去重会误吞另一个群的消息。
        self._seen: OrderedDict[tuple[int, int], float] = OrderedDict()

        # 同一个群同一个人连续发消息时，串行处理，避免历史顺序错乱。
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

        trigger = self.settings.qq_trigger.strip().lower()
        if trigger not in {"at", "prefix", "all"}:
            logger.warning("未知的 QQ_TRIGGER=%r，回退到 at", trigger)
            trigger = "at"
        self._trigger = trigger

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理一个 OneBot 事件，返回处理结果（主要给测试/日志用）。

        真正的 HTTP 路由会把它放进后台任务，先快速返回 204，
        因为 NapCat 不关心我们的回复内容，只关心有没有及时收到响应。
        """
        parsed = self._parse_group_message(payload)
        if parsed is None:
            return {"handled": False, "reason": "not_a_group_text_message"}

        group_id, user_id, self_id, text, at_self, message_id = parsed

        # 机器人自己发的消息（包括 NapCat 回显）不处理，否则会自己和自己聊天。
        if self_id and user_id == self_id:
            return {"handled": False, "reason": "self_message"}

        if group_id not in self.settings.qq_allowed_group_ids:
            return {"handled": False, "reason": "group_not_allowed"}

        if self._is_duplicate(group_id, message_id):
            return {"handled": False, "reason": "duplicate_message"}

        content = self._extract_trigger_text(text, at_self)
        if not content:
            return {"handled": False, "reason": "not_triggered_or_empty"}

        key = (group_id, user_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            reply, messages_for_history = await self._reply(key, content, user_id)
            if messages_for_history:
                self._history[key] = messages_for_history

            try:
                for chunk in self._split_text(reply):
                    await self.sender.send_group_message(group_id, chunk)
            except OneBotError as exc:
                logger.warning("QQ 回复发送失败（群 %s）：%s", group_id, exc)
                return {"handled": True, "reply": reply, "send_error": str(exc)}
            except Exception:  # noqa: BLE001 - 后台任务不能把异常抛给进程
                logger.exception("QQ 回复发送时出现未预期错误（群 %s）", group_id)
                return {"handled": True, "reply": reply, "send_error": "unexpected_error"}

        return {"handled": True, "reply": reply}

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _parse_group_message(
        self, payload: dict[str, Any]
    ) -> tuple[int, int, int, str, bool, int | None] | None:
        """从 OneBot 事件里取出群聊文本；不是群文本消息就返回 None。

        OneBot 11 的群消息大致长这样：
        ```json
        {
          "post_type": "message",
          "message_type": "group",
          "self_id": 123456,
          "group_id": 789,
          "user_id": 456,
          "message_id": 1001,
          "message": [
            {"type": "at", "data": {"qq": "123456"}},
            {"type": "text", "data": {"text": " 你好"}}
          ]
        }
        ```
        不同实现的 `message` 可能是字符串（CQ 码），这里只处理数组形式；
        字符串形式先忽略，避免在没有验证过的格式上做错误解析。
        """
        if payload.get("post_type") != "message":
            return None
        if payload.get("message_type") != "group":
            return None

        try:
            group_id = int(payload.get("group_id") or 0)
            user_id = int(payload.get("user_id") or 0)
            self_id = int(payload.get("self_id") or 0)
        except (TypeError, ValueError):
            return None
        if not group_id or not user_id:
            return None

        message = payload.get("message")
        if not isinstance(message, list):
            return None

        parts: list[str] = []
        at_self = False
        for segment in message:
            if not isinstance(segment, dict):
                continue
            segment_type = segment.get("type")
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            if segment_type == "text":
                parts.append(str(data.get("text") or ""))
            elif segment_type == "at" and self_id:
                if str(data.get("qq") or "") == str(self_id):
                    at_self = True

        text = "".join(parts).strip()
        message_id_raw = payload.get("message_id")
        try:
            message_id = int(message_id_raw) if message_id_raw is not None else None
        except (TypeError, ValueError):
            message_id = None

        return group_id, user_id, self_id, text, at_self, message_id

    def _extract_trigger_text(self, text: str, at_self: bool) -> str:
        """判断是否触发，并去掉触发前缀。"""
        if self._trigger == "at":
            return text if at_self else ""
        if self._trigger == "prefix":
            prefix = self.settings.qq_prefix.strip() or "梅林"
            if not text.startswith(prefix):
                return ""
            return text[len(prefix) :].strip()
        # all：白名单群里的任何文本都回。仍然要求非空，避免回复纯空格。
        return text if text else ""

    def _is_duplicate(self, group_id: int, message_id: int | None) -> bool:
        """记录并判断 (群号, message_id) 是否已经处理过。"""
        if message_id is None:
            return False
        key = (group_id, message_id)
        if key in self._seen:
            return True
        self._seen[key] = asyncio.get_running_loop().time()
        while len(self._seen) > MAX_SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return False

    # ------------------------------------------------------------------
    # 调用 agent
    # ------------------------------------------------------------------
    async def _reply(
        self,
        key: tuple[int, int],
        content: str,
        user_id: int,
    ) -> tuple[str, list[ChatMessage]]:
        """调用 agent，返回 (要发送的文本, 更新后的历史)。"""
        history = self._history.get(key, [])
        messages = [*history, ChatMessage(role="user", content=content[:8000])]
        is_admin = user_id in self.settings.qq_admin_user_ids

        parts: list[str] = []
        error_message = ""
        try:
            async for event in self._stream(
                messages,
                page_url=None,
                page_title=None,
                is_admin=is_admin,
            ):
                event_type = event.get("type")
                if event_type == "delta":
                    parts.append(str(event.get("content") or ""))
                elif event_type == "error":
                    error_message = str(event.get("message") or "AI 服务暂时不可用。")
        except Exception:  # noqa: BLE001 - agent 内部已经会捕获大部分异常
            logger.exception("QQ 通道调用 agent 失败")
            error_message = "AI 服务暂时不可用，请稍后再试。"

        reply = "".join(parts).strip()
        if not reply:
            reply = error_message or "我在，但这次没想好怎么回。"

        # agent 出错时不把错误写进历史，避免下一轮模型把报错当成事实。
        if error_message and not parts:
            return reply, history

        # 只保留最近 N 轮。1 轮 = 用户一条 + 助手一条。
        new_history = [
            *messages,
            ChatMessage(role="assistant", content=reply[:8000]),
        ]
        max_messages = max(0, self.settings.qq_history_turns) * 2
        if max_messages:
            new_history = new_history[-max_messages:]
        else:
            new_history = []
        return reply, new_history

    # ------------------------------------------------------------------
    # 输出格式
    # ------------------------------------------------------------------
    @staticmethod
    def _split_text(text: str) -> list[str]:
        """把长回复拆成适合群聊的短消息。

        先按换行切，再硬切超长段落。不要按标点做复杂分段：
        QQ 的显示宽度和字体不可控，简单可靠比“漂亮”重要。
        """
        text = re.sub(r"\x00", "", text).strip()
        if not text:
            return ["我在，但这次没想好怎么回。"]

        chunks: list[str] = []
        current = ""
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                if current:
                    current += "\n"
                continue
            while len(line) > MAX_MESSAGE_CHARS:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:MAX_MESSAGE_CHARS])
                line = line[MAX_MESSAGE_CHARS:]
            if not current:
                current = line
            elif len(current) + 1 + len(line) <= MAX_MESSAGE_CHARS:
                current += "\n" + line
            else:
                chunks.append(current)
                current = line

        if current:
            chunks.append(current)
        return chunks or [text[:MAX_MESSAGE_CHARS]]
