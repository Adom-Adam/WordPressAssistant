"""QQ 通道自己的额度计数。

====================================================================
为什么单独一套表
====================================================================
网页额度按 WordPress 用户 ID 记（`daily_usage` / `global_usage`）。
QQ 有两种可能：绑定到某个账号、或者（共享额度模式下）直接用网页那套。
但无论哪种模式，站长都希望有一个**只属于 QQ** 的全站闸门
（`QQ_GLOBAL_DAILY_QUOTA`），这样网页额度还很宽裕时也能单独把 QQ 卡住。

所以这里自己建两张表：

    qq_daily_usage (qq_uin, day, used)   —— 每个 QQ 每天用了几条
    qq_global_usage (day, used)          —— 所有 QQ 每天合计

按 QQ 号而不是用户 ID 记：还没绑定的人不该产生任何计数（他们根本
不会走到这里），已绑定的人换了账号也还是同一个"人"。

====================================================================
为什么返回结果对象而不是 None
====================================================================
`QuotaStore.reserve` 用 None 表示"个人额度用完"，用异常表示"全站用完"。
QQ 侧两种失败要给用户两句不同的话（"你的魔力用完了" vs "今天大家聊得太
热闹"），在调用处区分异常和 None 容易写错顺序；这里直接返回带 status 的
结果，调用方按 status 选文案，不需要 try/except。

====================================================================
事务语义
====================================================================
和网页完全一致：先按人、最后占全站，全程一个 BEGIN IMMEDIATE 事务。
全站满了就整体回滚，按人那一次 +1 也不能留下。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger(__name__)

QQ_QUOTA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qq_daily_usage (
    qq_uin TEXT    NOT NULL,
    day    TEXT    NOT NULL,
    used   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (qq_uin, day)
);

CREATE TABLE IF NOT EXISTS qq_global_usage (
    day  TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True, slots=True)
class QQReservation:
    """一次成功的 QQ 额度占用。

    退款时要按这两个 counted 标记原路退回：
    - 共享模式下只占了 QQ 全站（counted=False）；
    - 独立模式通常两个都占。
    """

    qq_uin: str
    day: str
    counted: bool
    limit: int | None
    remaining: int | None
    global_counted: bool
    global_limit: int
    global_remaining: int | None


@dataclass(frozen=True, slots=True)
class QQQuotaResult:
    """占用结果：失败时 free 一定是 None。"""

    status: str  # "ok" | "user_exhausted" | "global_exhausted"
    free: QQReservation | None


class QQQuotaStore:
    """QQ 按人与全站两级计数。"""

    def __init__(
        self,
        db_path: str,
        timezone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.timezone = ZoneInfo(timezone)
        self._now = now or (lambda: datetime.now(self.timezone))

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(QQ_QUOTA_SCHEMA_SQL)

    def today(self) -> str:
        return self._now().date().isoformat()

    def reserve(
        self,
        qq_uin: str,
        limit: int | None,
        *,
        global_limit: int = 0,
    ) -> QQQuotaResult:
        """尝试占用一次 QQ 额度。

        limit 为 None：不按人计（共享模式，按人那部分由网页额度负责）；
        limit 为 0：这个通道关掉了，直接拒绝；
        global_limit 为 0：QQ 全站不限量。
        """
        current_day = self.today()
        qq_uin = str(qq_uin or "").strip()

        if limit is not None and limit <= 0:
            return QQQuotaResult("user_exhausted", None)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            used: int | None = None
            if limit is not None:
                row = conn.execute(
                    """
                    INSERT INTO qq_daily_usage (qq_uin, day, used)
                    VALUES (?, ?, 1)
                    ON CONFLICT(qq_uin, day) DO UPDATE SET used = used + 1
                    WHERE used < ?
                    RETURNING used
                    """,
                    (qq_uin, current_day, limit),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return QQQuotaResult("user_exhausted", None)
                used = int(row[0])

            global_used: int | None = None
            if global_limit > 0:
                global_row = conn.execute(
                    """
                    INSERT INTO qq_global_usage (day, used)
                    VALUES (?, 1)
                    ON CONFLICT(day) DO UPDATE SET used = used + 1
                    WHERE used < ?
                    RETURNING used
                    """,
                    (current_day, global_limit),
                ).fetchone()
                if global_row is None:
                    # 全站满了：连按人的 +1 一起撤销。
                    conn.rollback()
                    return QQQuotaResult("global_exhausted", None)
                global_used = int(global_row[0])

            return QQQuotaResult(
                "ok",
                QQReservation(
                    qq_uin=qq_uin,
                    day=current_day,
                    counted=limit is not None,
                    limit=limit,
                    remaining=(limit - used) if (limit is not None and used is not None) else None,
                    global_counted=global_limit > 0,
                    global_limit=global_limit,
                    global_remaining=(
                        global_limit - global_used if global_used is not None else None
                    ),
                ),
            )

    def refund(self, free: QQReservation) -> None:
        """退回一次占用；只退当初真的占过的桶。"""
        with self._connection() as conn:
            if free.counted:
                conn.execute(
                    """
                    UPDATE qq_daily_usage
                       SET used = MAX(0, used - 1)
                     WHERE qq_uin = ? AND day = ?
                    """,
                    (free.qq_uin, free.day),
                )
            if free.global_counted:
                conn.execute(
                    """
                    UPDATE qq_global_usage
                       SET used = MAX(0, used - 1)
                     WHERE day = ?
                    """,
                    (free.day,),
                )


@lru_cache(maxsize=1)
def get_qq_quota_store() -> QQQuotaStore:
    """全局唯一的 QQ 额度存储。测试改了环境变量后要 cache_clear。"""
    settings = get_settings()
    return QQQuotaStore(settings.quota_db_path, settings.quota_timezone)
