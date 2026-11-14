"""私聊会话协调器：防抖合并 + 生成中打断。

QQ 私聊的现实是：人打字很快，一句话会拆成好几条；上一轮还没回完，
下一句已经来了。适配器如果按“一条消息一次模型调用”处理，就会
又慢又碎，而且旧回复会压在新消息后面。

这里按会话（key）维护一个小队列：

    submit(text, payload) ──▶ 队列 ──▶ 防抖窗口 ──▶ handler([messages])

- 防抖：最后一条到达后再等 `debounce` 秒，总等待不超过 `max_debounce`；
- 合并：窗口内的多条消息交给 handler 一次处理；
- 打断：handler 正在跑时又来新消息，取消当前回合，把它的文本
  放回队首，和新消息一起重答。

`submit` 会一直等到“它所在的那一轮”结束，所以调用方仍然可以
把返回值直接当成这一条消息的处理结果。被取消的那一轮返回
`{"handled": True, "reason": "interrupted"}`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """排队中的一条消息：文本，以及随行数据（目前是图片附件）。"""

    text: str
    payload: Any = None


Handler = Callable[[list[QueuedMessage]], Awaitable[dict]]


@dataclass
class _Conversation:
    queue: list[QueuedMessage] = field(default_factory=list)
    waiters: list[asyncio.Future] = field(default_factory=list)
    first_at: float | None = None
    last_at: float = 0.0
    event: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    round_task: asyncio.Task | None = None


class ConversationCoordinator:
    """按 key 串行的私聊回合调度器。"""

    def __init__(self) -> None:
        self._conversations: dict[object, _Conversation] = {}

    def pending(self, key: object) -> bool:
        conversation = self._conversations.get(key)
        if conversation is None:
            return False
        if conversation.queue:
            return True
        return conversation.worker is not None and not conversation.worker.done()

    async def submit(
        self,
        key: object,
        text: str,
        *,
        handler: Handler,
        debounce: float,
        max_debounce: float,
        payload: Any = None,
    ) -> dict:
        """把一条消息放进会话，等待它所在回合的结果。

        `payload` 随文本一起排队，合并/打断时不会丢；目前用来携带
        图片和表情包附件。
        """
        loop = asyncio.get_running_loop()
        conversation = self._conversations.get(key)
        if conversation is None or (
            conversation.worker is not None and conversation.worker.done()
        ):
            conversation = _Conversation()
            self._conversations[key] = conversation

        now = loop.time()
        conversation.queue.append(QueuedMessage(text=str(text), payload=payload))
        conversation.last_at = now
        if conversation.first_at is None:
            conversation.first_at = now
        conversation.event.set()

        # 新消息到达 = 打断当前生成。被取消回合的文本会在 worker 里
        # 放回队首，和本条一起进入下一轮。
        if conversation.round_task is not None and not conversation.round_task.done():
            conversation.round_task.cancel()

        waiter: asyncio.Future = loop.create_future()
        conversation.waiters.append(waiter)

        if conversation.worker is None or conversation.worker.done():
            conversation.worker = loop.create_task(
                self._run(
                    key,
                    conversation,
                    handler=handler,
                    debounce=debounce,
                    max_debounce=max_debounce,
                )
            )

        return await waiter

    async def drain(self) -> None:
        """等待所有会话空闲；测试和优雅退出用。"""
        while True:
            workers = [
                conversation.worker
                for conversation in list(self._conversations.values())
                if conversation.worker is not None and not conversation.worker.done()
            ]
            if not workers:
                return
            await asyncio.gather(*workers, return_exceptions=True)

    async def _run(
        self,
        key: object,
        conversation: _Conversation,
        *,
        handler: Handler,
        debounce: float,
        max_debounce: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            while conversation.queue:
                await self._wait_window(conversation, debounce, max_debounce)

                messages = conversation.queue
                waiters = conversation.waiters
                conversation.queue = []
                conversation.waiters = []
                conversation.first_at = None
                if not messages:
                    break

                conversation.round_task = loop.create_task(handler(messages))
                try:
                    result = await conversation.round_task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    # 这一轮被新消息打断：文本和附件放回队首，等待者立刻返回。
                    conversation.queue[:0] = messages
                    conversation.round_task = None
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_result(
                                {"handled": True, "reason": "interrupted"}
                            )
                    continue
                except Exception:  # noqa: BLE001 - handler 出错不能卡住会话
                    logger.exception("QQ 私聊回合处理失败")
                    result = {"handled": False, "reason": "handler_error"}

                conversation.round_task = None
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_result(result)
        finally:
            conversation.worker = None
            conversation.round_task = None
            if self._conversations.get(key) is conversation:
                self._conversations.pop(key, None)

    @staticmethod
    async def _wait_window(
        conversation: _Conversation, debounce: float, max_debounce: float
    ) -> None:
        """等到防抖窗口结束：最后一条之后 debounce 秒，或从第一条起 max_debounce 秒。"""
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            if conversation.first_at is None:
                return
            if now - conversation.first_at >= max_debounce:
                return
            remaining = debounce - (now - conversation.last_at)
            if remaining <= 0:
                return
            conversation.event.clear()
            try:
                await asyncio.wait_for(
                    conversation.event.wait(),
                    timeout=min(remaining, max_debounce - (now - conversation.first_at)),
                )
            except asyncio.TimeoutError:
                pass
