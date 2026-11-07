"""OneBot 事件 → 内部 ChatMessage → agent → 群消息。

设计原则
--------
1. **默认拒绝**：`QQ_ALLOWED_GROUPS` 为空时，任何群消息都不处理。
2. **只处理文本**：群聊与私聊都支持；频道、图片、表情先忽略。
3. **用户内容只是数据**：群消息会作为 `ChatMessage(role="user")` 进入模型，
   永远不会拼进 system prompt，也不会改变管理员身份。
4. **幂等**：NapCat 在网络抖动时可能重发同一个 `message_id`，必须去重。
5. **失败可观察**：发送失败只记日志，不让后台任务崩掉整个进程。

这一层刻意保持“薄”：只读绑定关系，不写长期记忆，也从不主动发消息。

消息处理顺序（顺序本身就是规则）：

    去重 → 触发判断 → 斜杠命令 → 绑定判定 → 额度 → agent

命令在绑定判定之前：命令自己对自己的权限负责（比如 /绑定 只在私聊执行），
也不消耗额度；未绑定的人在命令里拿到的是引导，而不是 AI 回答。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.core import stream_reply
from app.channels.qq import commands
from app.channels.qq.client import OneBotError, OneBotTransport
from app.channels.qq.commands import CommandContext, dispatch, parse_command
from app.config import Settings
from app.credits import CreditStore, get_credit_store
from app.memory import pipeline
from app.qq_quota import QQQuotaGate, QQUsage, get_qq_quota_gate
from app.schemas import ChatMessage

logger = logging.getLogger(__name__)

# 一条 QQ 文本消息最多发多少字符。太长会刷屏，也容易被平台限流。
# 这里不是模型的输出上限，而是“单条群消息”的展示上限；超出的部分拆成多条。
MAX_MESSAGE_CHARS = 1_200

# 去重表最多记多少个 message_id。QQ 群消息量不大，500 足够覆盖重发窗口。
MAX_SEEN_MESSAGES = 500


@dataclass(frozen=True, slots=True)
class Incoming:
    """解析后的入站消息。私聊的 group_id 固定为 0。"""

    message_type: str  # "group" | "private"
    group_id: int
    user_id: int
    self_id: int
    text: str
    at_self: bool
    message_id: int | None


class QQAdapter:
    """处理 NapCat 推送的 OneBot 事件。"""

    def __init__(
        self,
        settings: Settings,
        sender: OneBotTransport,
        *,
        stream: Callable[..., AsyncIterator[dict[str, Any]]] = stream_reply,
        credits: CreditStore | None = None,
        gate: QQQuotaGate | None = None,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self._stream = stream
        # 绑定关系存在额度库里。可注入是为了测试用一个临时数据库，
        # 生产不传时惰性取全局实例（导入时就建库会让 --help 之类也写文件）。
        self.credits = credits
        # 额度门面同理：测试注入固定参数的门面，生产惰性取全局实例。
        self.gate = gate

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
        incoming = self._parse_message(payload)
        if incoming is None:
            return {"handled": False, "reason": "not_a_text_message"}

        # 机器人自己发的消息（包括 NapCat 回显）不处理，否则会自己和自己聊天。
        if incoming.self_id and incoming.user_id == incoming.self_id:
            return {"handled": False, "reason": "self_message"}

        if incoming.message_type == "group":
            if incoming.group_id not in self.settings.qq_allowed_group_ids:
                return {"handled": False, "reason": "group_not_allowed"}
            content = self._extract_trigger_text(incoming.text, incoming.at_self)
        else:
            # 私聊没有 @ 的概念，整条文本就是正文。
            content = incoming.text.strip()

        if self._is_duplicate(incoming.group_id, incoming.message_id):
            return {"handled": False, "reason": "duplicate_message"}

        if not content:
            return {"handled": False, "reason": "not_triggered_or_empty"}

        is_private = incoming.message_type == "private"
        key = (incoming.group_id, incoming.user_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            store = self.credits or get_credit_store()
            admin_whitelisted = incoming.user_id in self.settings.qq_admin_user_ids
            binding = store.binding_of_qq(str(incoming.user_id))
            is_admin = admin_whitelisted or (binding.is_admin if binding else False)

            # 1. 斜杠命令：先于绑定判定，永不进 agent、不进历史。
            parsed = parse_command(content)
            if parsed is not None:
                reply = dispatch(
                    parsed,
                    CommandContext(
                        qq_uin=incoming.user_id,
                        is_private=is_private,
                        group_id=None if is_private else incoming.group_id,
                        args="",
                        bound_user_id=binding.user_id if binding else None,
                        is_admin=is_admin,
                        store=store,
                    ),
                )
                return await self._send(incoming, reply, reason="command")

            # 2. 未绑定：只回引导。管理员白名单是自救通道，不受绑定限制。
            if binding is None and not admin_whitelisted:
                guide = (
                    commands.private_guide(self.settings.wp_site_url)
                    if is_private
                    else commands.GROUP_GUIDE
                )
                return await self._send(incoming, guide, reason="guide_unbound")

            # 3. 额度：管理员不计数；用尽时回固定文案，不调模型。
            status, usage = self._reserve(
                qq_uin=str(incoming.user_id),
                user_id=binding.user_id if binding else None,
                tier=binding.tier if binding else "user",
                is_admin=is_admin,
            )
            if status == "user_exhausted":
                return await self._send(
                    incoming, commands.QUOTA_USER, reason="quota_exhausted"
                )
            if status == "global_exhausted":
                return await self._send(
                    incoming, commands.QUOTA_GLOBAL, reason="quota_exhausted"
                )

            # 3.5 取这一轮要注入的记忆。失败时为空字符串，回复照常。
            try:
                memory_context = await asyncio.to_thread(
                    pipeline.qq_memory_context,
                    incoming.user_id,
                    incoming.group_id or None,
                    binding,
                    content,
                )
            except Exception:  # noqa: BLE001 - 记忆只是增强
                logger.warning("QQ 记忆检索失败 qq=%s", incoming.user_id, exc_info=True)
                memory_context = ""

            # 4. 正常聊天。
            reply, messages_for_history, produced = await self._reply(
                key,
                content,
                is_admin=is_admin,
                channel="qq_private" if is_private else "qq_group",
                memory_context=memory_context or None,
            )
            if produced:
                if messages_for_history:
                    self._history[key] = messages_for_history
                # QQ 侧在回复产出后一次性写两条事件：这一轮整体入库，
                # 失败轮次（没产出正文）本来也没有完整的一问一答可提炼。
                turn = await asyncio.to_thread(
                    pipeline.record_qq_turn,
                    incoming.user_id,
                    incoming.group_id or None,
                    binding,
                    content,
                    reply,
                )
                if turn is not None:
                    pipeline.schedule_extraction(turn)
            else:
                # 没产出正文（模型报错/断流）：这次不算，退款，也别污染上下文。
                self._refund(usage)
                reply = commands.AGENT_ERROR

            return await self._send(incoming, reply)

    async def _send(
        self, incoming: Incoming, reply: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """按来源把回复发回群/私聊；发送失败只记日志。"""
        result: dict[str, Any] = {"handled": True, "reply": reply}
        if reason:
            result["reason"] = reason

        try:
            for chunk in self._split_text(reply):
                if incoming.message_type == "private":
                    await self.sender.send_private_message(incoming.user_id, chunk)
                else:
                    await self.sender.send_group_message(incoming.group_id, chunk)
        except OneBotError as exc:
            logger.warning(
                "QQ 回复发送失败（%s %s）：%s",
                incoming.message_type,
                incoming.group_id or incoming.user_id,
                exc,
            )
            result["send_error"] = str(exc)
        except Exception:  # noqa: BLE001 - 后台任务不能把异常抛给进程
            logger.exception("QQ 回复发送时出现未预期错误")
            result["send_error"] = "unexpected_error"

        return result

    # ------------------------------------------------------------------
    # 额度
    # ------------------------------------------------------------------
    def _quota_gate(self) -> QQQuotaGate:
        """惰性取额度门面；导入时就建库会让 --help 之类也写文件。"""
        if self.gate is None:
            self.gate = get_qq_quota_gate()
        return self.gate

    def _reserve(
        self, *, qq_uin: str, user_id: int | None, tier: str, is_admin: bool
    ) -> tuple[str, QQUsage | None]:
        """占一次额度；返回 (状态, 可用于退款的凭据)。

        额度库出错时放行而不是拦人：配额是防滥用，不是可用性的前提。
        """
        try:
            return self._quota_gate().reserve(
                qq_uin=qq_uin, user_id=user_id, tier=tier, is_admin=is_admin
            )
        except Exception:  # noqa: BLE001 - 额度故障不该让聊天整体不可用
            logger.exception("QQ 额度预占失败，本次放行")
            return "ok", None

    def _refund(self, usage: QQUsage | None) -> None:
        """没产出正文就退回这次占用的额度。"""
        if usage is None:
            return
        try:
            self._quota_gate().refund(usage)
        except Exception:  # noqa: BLE001 - 退款失败只影响计数，不影响回复
            logger.exception("QQ 额度退款失败")

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _parse_message(self, payload: dict[str, Any]) -> Incoming | None:
        """从 OneBot 事件里取出群聊/私聊文本；其他事件返回 None。

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
        私聊把 message_type 换成 "private"，没有 group_id。
        不同实现的 `message` 可能是字符串（CQ 码），这里只处理数组形式；
        字符串形式先忽略，避免在没有验证过的格式上做错误解析。
        """
        if payload.get("post_type") != "message":
            return None
        message_type = payload.get("message_type")
        if message_type not in {"group", "private"}:
            return None

        try:
            user_id = int(payload.get("user_id") or 0)
            self_id = int(payload.get("self_id") or 0)
            group_id = int(payload.get("group_id") or 0) if message_type == "group" else 0
        except (TypeError, ValueError):
            return None
        if not user_id or (message_type == "group" and not group_id):
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

        message_id_raw = payload.get("message_id")
        try:
            message_id = int(message_id_raw) if message_id_raw is not None else None
        except (TypeError, ValueError):
            message_id = None

        return Incoming(
            message_type=message_type,
            group_id=group_id,
            user_id=user_id,
            self_id=self_id,
            text="".join(parts).strip(),
            at_self=at_self,
            message_id=message_id,
        )

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
        *,
        is_admin: bool,
        channel: str,
        memory_context: str | None = None,
    ) -> tuple[str, list[ChatMessage], bool]:
        """调用 agent，返回 (要发送的文本, 更新后的历史, 是否产出过正文)。

        第三个返回值给额度层用：没产出正文的失败要退款。

        `channel` 只影响人设覆盖层。QQ 里她是在用通讯法阵和人说话，
        眼前没有书架、没有页面，所以 page_url/page_title 一律传 None。
        """
        history = self._history.get(key, [])
        messages = [*history, ChatMessage(role="user", content=content[:8000])]

        parts: list[str] = []
        error_message = ""
        try:
            async for event in self._stream(
                messages,
                page_url=None,
                page_title=None,
                is_admin=is_admin,
                channel=channel,
                memory_context=memory_context,
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

        # 与网页 chat.py 保持一致：只有真正产出非空正文才算“答上了”。
        # 空 delta 也算没交付，额度要退、历史不能写。
        produced = any(part.strip() for part in parts)

        # agent 出错时不把错误写进历史，避免下一轮模型把报错当成事实。
        if error_message and not produced:
            return reply, history, produced

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
        return reply, new_history, produced

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
