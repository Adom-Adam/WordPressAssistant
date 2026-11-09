"""常驻循环。

调度器本身是“跑一轮”，循环负责“每隔一会儿跑一轮”。分开的好处是：
测试里可以直接调一轮，不必等时钟。
"""

from __future__ import annotations

import asyncio
import functools

from app.config import Settings
from app.proactive.runner import SchedulerRunner, create_runner


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper


class CountingScheduler:
    def __init__(self):
        self.calls = 0

    async def run_once(self, now=None):
        self.calls += 1
        return []


def test_create_runner_respects_qq_enabled():
    assert create_runner(Settings(qq_enabled=False)) is None
    assert (
        create_runner(Settings(qq_enabled=True, qq_mock_mode=True))
        is not None
    )


@async_test
async def test_runner_starts_and_stops_cleanly():
    scheduler = CountingScheduler()
    runner = SchedulerRunner(scheduler, tick_seconds=0.01)
    await runner.start()
    await asyncio.sleep(0.05)
    await runner.stop()
    assert scheduler.calls >= 1
