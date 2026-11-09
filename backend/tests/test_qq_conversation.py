"""私聊会话协调器：防抖合并与生成中打断。

这一层不碰模型和数据库，只负责“什么时候把消息交给下一层”。
关键行为：
- 连续到达的消息合并成一个回合，只调用一次 handler；
- 回合执行中来了新消息，取消当前回合并把它的文本一起并进下一轮；
- 被取消的等待者拿到 interrupted，而不是永远挂起。
"""

from __future__ import annotations

import asyncio
import functools

from app.channels.qq.conversation import ConversationCoordinator


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@async_test
async def test_debounce_merges_messages_into_one_round():
    coordinator = ConversationCoordinator()
    seen: list[list[str]] = []

    async def handler(texts):
        seen.append(texts)
        return {"handled": True, "round": len(seen)}

    first = asyncio.create_task(
        coordinator.submit("k", "第一句", handler=handler, debounce=0.05, max_debounce=0.2)
    )
    await asyncio.sleep(0.01)
    second = asyncio.create_task(
        coordinator.submit("k", "第二句", handler=handler, debounce=0.05, max_debounce=0.2)
    )
    results = await asyncio.gather(first, second)

    assert seen == [["第一句", "第二句"]]
    assert results[0] == results[1]


@async_test
async def test_new_message_interrupts_running_round_and_merges():
    coordinator = ConversationCoordinator()
    seen: list[list[str]] = []
    started = asyncio.Event()

    async def handler(texts):
        seen.append(texts)
        if len(seen) == 1:
            started.set()
            await asyncio.sleep(10)  # 会被打断
        return {"handled": True, "round": len(seen)}

    first = asyncio.create_task(
        coordinator.submit("k", "长回复", handler=handler, debounce=0, max_debounce=0)
    )
    await started.wait()
    second = asyncio.create_task(
        coordinator.submit("k", "新消息", handler=handler, debounce=0, max_debounce=0)
    )
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["reason"] == "interrupted"
    assert seen[-1] == ["长回复", "新消息"]
    assert second_result["handled"] is True


@async_test
async def test_drain_waits_for_workers():
    coordinator = ConversationCoordinator()

    async def handler(texts):
        await asyncio.sleep(0.02)
        return {"handled": True}

    await coordinator.submit("k", "x", handler=handler, debounce=0, max_debounce=0)
    await coordinator.drain()
    assert coordinator.pending("k") is False
