"""主动消息的常驻循环。

调度器只负责“跑一轮”，这个循环负责“每隔一会儿再跑一轮”。拆开是为
了测试：跑一轮可以直接调用，不必等时钟。

循环里的任何异常都只记日志。一次 tick 失败不该让进程退出，更不该
让下一条消息永远发不出去——下一轮会重新判断。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.channels.qq.client import build_onebot_client
from app.credits import get_credit_store
from app.proactive.scheduler import ProactiveScheduler

logger = logging.getLogger(__name__)

# 即使配置写错，也不让循环变成忙等。
_MIN_TICK_SECONDS = 1.0


class SchedulerRunner:
    """按间隔反复调用 `scheduler.run_once()`，可随时停下。"""

    def __init__(
        self, scheduler: Any, *, tick_seconds: float | None = None
    ) -> None:
        self.scheduler = scheduler
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scheduler.run_once()
            except Exception:  # noqa: BLE001 - 一轮失败不影响下一轮
                logger.warning("主动消息 tick 失败", exc_info=True)

            tick = self.tick_seconds
            if tick is None:
                # 每轮现读：管理员在 WP 后台改了检查间隔，下一轮生效。
                from app.runtime_settings import get_runtime_store

                tick = (
                    get_runtime_store().load().proactive_tick_minutes * 60
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(_MIN_TICK_SECONDS, float(tick)),
                )
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def create_runner(settings: Any) -> SchedulerRunner | None:
    """QQ 通道没开时不必起循环。"""
    if not settings.qq_enabled:
        return None
    scheduler = ProactiveScheduler(
        settings,
        sender=build_onebot_client(settings),
        credits=get_credit_store(),
    )
    return SchedulerRunner(scheduler)
