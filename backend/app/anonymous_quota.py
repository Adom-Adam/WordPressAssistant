"""匿名聊天的额度计数。

====================================================================
为什么不能复用 QuotaStore
====================================================================
`QuotaStore` 的主键是 WordPress 用户 ID。匿名的人没有 ID，退而用
“IP 哈希”或 QQ 号作 scope；而且匿名的总量和登录用户的额度必须**分开**
记账——否则一群游客能把会员当天的份吃光。

所以这里自己两张表：个人 scope 一份、全站一份。表结构放在
`quota_db_path` 指向的同一个 SQLite 文件里，少一个需要备份的文件。

====================================================================
两个 0 的含义不一样
====================================================================
- 个人上限 0：这条匿名通道关掉了（网页关了但 QQ 还开着是允许的）；
- 全站上限 0：全站不限量。

写反了会出现“明明关了匿名却还能聊”或者“想把全站放开却被卡住”，
所以 reserve 的注释和测试都盯着这两个 0。

====================================================================
事务语义
====================================================================
和 QQ 额度一致：先按人、最后占全站，一个 BEGIN IMMEDIATE 事务；
全站满了整体回滚，按人那次 +1 不能留下。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

ANONYMOUS_QUOTA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS anonymous_usage (
    scope TEXT    NOT NULL,
    day   TEXT    NOT NULL,
    used  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, day)
);

CREATE TABLE IF NOT EXISTS anonymous_global_usage (
    day  TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True, slots=True)
class AnonymousReservation:
    """一次成功的匿名额度占用。

    `global_remaining` 为 None 表示全站不限量（`global_limit == 0`）。
    """

    scope: str
    day: str
    limit: int
    remaining: int
    global_limit: int
    global_remaining: int | None


@dataclass(frozen=True, slots=True)
class AnonymousQuotaResult:
    """占用结果：失败时 free 一定是 None。"""

    status: str  # "ok" | "user_exhausted" | "global_exhausted"
    free: AnonymousReservation | None


class AnonymousQuotaStore:
    """匿名按 scope（IP 哈希 / QQ 号）与全站两级计数。"""

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
            conn.executescript(ANONYMOUS_QUOTA_SCHEMA_SQL)

    def today(self) -> str:
        return self._now().date().isoformat()

    def seconds_until_reset(self) -> int:
        """距离本地零点还有多少秒（响应头用，不让前端自己算时区）。"""
        now = self._now()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(0, int((tomorrow - now).total_seconds()))

    def reserve(
        self,
        scope: str,
        limit: int,
        *,
        global_limit: int = 0,
        day: str | None = None,
    ) -> AnonymousQuotaResult:
        """尝试占用一次匿名额度。

        limit <= 0：这条通道关掉了，直接拒绝；
        global_limit <= 0：全站不限量。
        """
        current_day = day or self.today()
        scope = str(scope or "").strip() or "unknown"

        if limit <= 0:
            return AnonymousQuotaResult("user_exhausted", None)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                INSERT INTO anonymous_usage (scope, day, used)
                VALUES (?, ?, 1)
                ON CONFLICT(scope, day) DO UPDATE SET used = used + 1
                WHERE used < ?
                RETURNING used
                """,
                (scope, current_day, limit),
            ).fetchone()
            if row is None:
                conn.rollback()
                return AnonymousQuotaResult("user_exhausted", None)
            used = int(row[0])

            global_used: int | None = None
            if global_limit > 0:
                global_row = conn.execute(
                    """
                    INSERT INTO anonymous_global_usage (day, used)
                    VALUES (?, 1)
                    ON CONFLICT(day) DO UPDATE SET used = used + 1
                    WHERE used < ?
                    RETURNING used
                    """,
                    (current_day, global_limit),
                ).fetchone()
                if global_row is None:
                    # 全站满了：连按 scope 的 +1 一起撤销。
                    conn.rollback()
                    return AnonymousQuotaResult("global_exhausted", None)
                global_used = int(global_row[0])

            return AnonymousQuotaResult(
                "ok",
                AnonymousReservation(
                    scope=scope,
                    day=current_day,
                    limit=limit,
                    remaining=limit - used,
                    global_limit=global_limit,
                    global_remaining=(
                        global_limit - global_used
                        if global_used is not None
                        else None
                    ),
                ),
            )

    def refund(self, free: AnonymousReservation) -> None:
        """退回一次占用；全站不限量时只退按 scope 那一次。"""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE anonymous_usage
                   SET used = MAX(0, used - 1)
                 WHERE scope = ? AND day = ?
                """,
                (free.scope, free.day),
            )
            if free.global_limit > 0:
                conn.execute(
                    """
                    UPDATE anonymous_global_usage
                       SET used = MAX(0, used - 1)
                     WHERE day = ?
                    """,
                    (free.day,),
                )

    def used(self, scope: str, *, day: str | None = None) -> int:
        row = self._connection_row(
            "SELECT used FROM anonymous_usage WHERE scope = ? AND day = ?",
            (str(scope or "").strip() or "unknown", day or self.today()),
        )
        return int(row["used"]) if row is not None else 0

    def _connection_row(
        self, sql: str, params: tuple[object, ...]
    ) -> sqlite3.Row | None:
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchone()


@lru_cache(maxsize=1)
def get_anonymous_quota_store() -> AnonymousQuotaStore:
    """全局唯一的匿名额度存储。测试改了环境变量后要 cache_clear。"""
    settings = get_settings()
    return AnonymousQuotaStore(settings.quota_db_path, settings.quota_timezone)
