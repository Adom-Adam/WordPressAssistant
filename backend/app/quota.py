"""每日聊天额度。

====================================================================
为什么额度要放在后端
====================================================================

前端可以被用户随意修改：删掉检查、伪造请求、直接 curl 后端。
所以“能不能聊天”必须由后端判断，而且判断依据必须是**签名过的用户 ID**
（见 app/auth/jwt.py），不能是前端传来的昵称或参数。

====================================================================
为什么用 SQLite
====================================================================

额度只需要“按用户 + 日期记一个整数”，不需要独立的数据库服务。
SQLite 是 Python 标准库自带的单文件数据库：
- 零运维，备份就是复制一个文件；
- 支持事务和原子更新，并发下不会把次数算错；
- 个人博客的请求量远远达不到它的瓶颈。

代价是它不适合多台服务器共享。以后如果后端横向扩容，
把这张表换成 Redis 或 MySQL 即可，接口（reserve/refund）不用变。

====================================================================
“先占位、失败退款”是什么意思
====================================================================

如果等模型回答完再扣次数，用户可能同时发 10 个请求，每个都先通过检查，
最后一起扣——额度就被突破了。正确顺序是：

1. 请求进来，先用一条原子 SQL 把次数 +1（占位）；
2. 占位成功才调用模型；
3. 如果模型一个字都没输出就失败，再把次数 -1（退款）。

`INSERT ... ON CONFLICT ... DO UPDATE ... WHERE used < ?` 是这里的关键：
它把“读当前次数、判断是否超限、写回 +1”合并成一条语句，
SQLite 会保证这条语句原子执行，不会出现两个请求同时看到 used=19 的情况。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    """一次成功占位的凭证。

    `counted=False` 表示管理员没有写数据库（不限量）；
    退款时必须检查这个字段，否则会去减一条根本不存在的记录。
    """

    user_id: int
    day: str
    counted: bool
    limit: int
    remaining: int | None


class QuotaStore:
    """基于 SQLite 的每日额度存储。

    每个方法都自己打开/关闭连接。听起来有点浪费，但 SQLite 打开本地文件
    只要几十微秒，而且这样不用担心多线程共享连接的问题。
    真正的并发压力变大时，可以换成连接池或 Redis。
    """

    def __init__(self, db_path: str, timezone: str = "Asia/Shanghai") -> None:
        self.db_path = Path(db_path)
        # ZoneInfo 来自标准库 zoneinfo，用 IANA 时区名（如 Asia/Shanghai）
        # 计算“现在属于哪一天”。不能用固定 +8 小时硬编码，
        # 因为有些地区有夏令时；用标准库能自动处理。
        self.timezone = ZoneInfo(timezone)

        # 数据库文件所在的目录可能还不存在（例如首次启动）。
        # parents=True 会连同上级目录一起创建，exist_ok=True 表示已存在也不报错。
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """借出一个连接，用完一定关闭。

        注意 Python 的一个坑：
            with sqlite3.connect(path) as conn:
                ...
        这个 with 只管“提交/回滚事务”，**并不会关闭连接**。
        连接要等垃圾回收才释放；请求一多就会累积文件句柄。
        所以这里同时用两层：
        - 内层 `with conn:` 负责事务；
        - 外层 try/finally 负责 close()。
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            # busy_timeout 是 SQLite 自己的等待策略：
            # 如果另一个请求正持有写锁，最多等 5 秒再报错，
            # 而不是立刻抛 "database is locked"。
            conn.execute("PRAGMA busy_timeout = 5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """建表，只执行一次（每次进程启动时）。

        PRIMARY KEY (user_id, day) 让“同一个用户同一天”只有一行；
        重复插入会触发 ON CONFLICT，从而走后面的“次数 +1”逻辑。
        """
        with self._connection() as conn:
            # WAL（Write-Ahead Logging）让读写可以并行，适合“一边聊天一边查额度”。
            # 这个设置会写进数据库文件，只需设置一次。
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    user_id INTEGER NOT NULL,
                    day     TEXT    NOT NULL,
                    used    INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                )
                """
            )

    def today(self) -> str:
        """返回当前时区下的日期字符串，例如 "2026-10-26"。

        用字符串存日期，排序和比较都是字典序，简单直观。
        """
        return datetime.now(self.timezone).date().isoformat()

    def seconds_until_reset(self) -> int:
        """距离“额度重置”（下一个零点）还有多少秒。

        这个值会放进 HTTP 429 响应的 Retry-After 头，
        让客户端知道什么时候可以再试，而不是盲目轮询。
        """
        now = datetime.now(self.timezone)
        # 先加一天再抹掉时分秒，得到“明天的 00:00:00”。
        # 直接 replace(day=now.day + 1) 在月末会出错，加一天更稳妥。
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((tomorrow - now).total_seconds()))

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def reserve(
        self,
        user_id: int,
        limit: int,
        *,
        is_admin: bool = False,
        day: str | None = None,
    ) -> QuotaReservation | None:
        """尝试占用一次额度。

        返回值：
        - 成功：QuotaReservation（管理员返回 counted=False）；
        - 普通用户额度用完：None。

        参数 day 只用于测试（模拟“第二天”），正常调用不传。
        """
        current_day = day or self.today()

        # 管理员不限量，也不写数据库，避免产生无意义的计数行。
        if is_admin:
            return QuotaReservation(
                user_id=user_id,
                day=current_day,
                counted=False,
                limit=limit,
                remaining=None,
            )

        # limit <= 0 表示禁止普通用户聊天。
        if limit <= 0:
            return None

        with self._connection() as conn:
            # RETURNING used 会把更新后的值直接返回；
            # 如果 WHERE used < ? 不成立（已经用满），则不会返回任何行。
            row = conn.execute(
                """
                INSERT INTO daily_usage (user_id, day, used)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, day) DO UPDATE SET used = used + 1
                WHERE used < ?
                RETURNING used
                """,
                (user_id, current_day, limit),
            ).fetchone()

        if row is None:
            return None

        used = int(row[0])
        return QuotaReservation(
            user_id=user_id,
            day=current_day,
            counted=True,
            limit=limit,
            remaining=max(limit - used, 0),
        )

    def refund(self, reservation: QuotaReservation | None) -> None:
        """退还一次额度。

        什么时候退：请求在“输出第一个字之前”就失败/超时/被取消。
        什么时候不退：已经输出了一部分内容——模型确实消耗了 token，
        用户也看到了东西，再退就不合理了。
        """
        if reservation is None or not reservation.counted:
            return

        with self._connection() as conn:
            # 加 used > 0 的判断，避免极端情况下把计数减成负数。
            conn.execute(
                """
                UPDATE daily_usage
                   SET used = CASE WHEN used > 0 THEN used - 1 ELSE 0 END
                 WHERE user_id = ? AND day = ?
                """,
                (reservation.user_id, reservation.day),
            )

        logger.info(
            "额度已退还 user=%s day=%s", reservation.user_id, reservation.day
        )


@lru_cache(maxsize=1)
def get_quota_store() -> QuotaStore:
    """全局唯一的额度存储。

    和 build_chat_model 一样用 lru_cache 缓存：建表只在第一次调用时发生。
    测试时改了环境变量，需要调用 get_quota_store.cache_clear()。
    """
    settings = get_settings()
    return QuotaStore(settings.quota_db_path, settings.quota_timezone)
