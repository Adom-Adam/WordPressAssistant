"""用户等级、额外额度与兑换码。

====================================================================
为什么这些东西不在 WordPress 里
====================================================================

等级和额度必须由后端说了算：前端能改请求，WordPress 的用户元数据后端也写不了。
如果“会员”标记存在 WordPress 里，兑换码升级就得让 Python 反过来去写 WordPress
（开写权限或搞一张待生效表），两条路都比现在复杂。

反过来，邮箱、昵称、注册时间只存在 WordPress，后端一个字节都不存。
两边各管自己知道的事，后台页在 PHP 里把两份数据拼起来。

====================================================================
为什么额外额度单独一张表
====================================================================

`daily_usage` 按 (user_id, day) 记数，天然每天刷新。
额外额度是“攒下来、用完不补”的存量，没有日期维度，所以必须单独一张
`credit_balance`；每一次增减再写一条 `credit_ledger`，出问题能查、能反向补偿。

====================================================================
为什么到期用“惰性降级”
====================================================================

会员到期不需要定时任务：读等级时顺手判断一下，过期就改回普通用户。
这样即使某天进程没跑、机器关了一周，恢复后第一次读也是正确的。
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger(__name__)

# 只有两档“普通用户”等级；管理员由 JWT 的 admin 字段决定，不进这张表。
TIERS: tuple[str, ...] = ("user", "member")

# Crockford Base32：去掉了 I、L、O、U，避免和 1、0、V 混淆。
# 16 位约 79 bit，靠猜是猜不出来的。
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 16

# 这些表都由这里定义，`QuotaStore` 在启动时执行同一份 SQL。
# 之所以只保留一份定义：额度扣减发生在 quota.py 的事务里，
# 如果两个模块各自建表，迟早会出现“一边有、一边没有”的错位。
CREDIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_profile (
    user_id         INTEGER PRIMARY KEY,
    tier            TEXT    NOT NULL DEFAULT 'user',
    tier_expires_at TEXT,
    qq_uin          TEXT,
    note            TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_balance (
    user_id    INTEGER PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    ref        TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_user
    ON credit_ledger(user_id, id);

CREATE TABLE IF NOT EXISTS redeem_code (
    code       TEXT PRIMARY KEY,
    kind       TEXT    NOT NULL,
    value      INTEGER NOT NULL,
    max_uses   INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    note       TEXT,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS redeem_log (
    code    TEXT    NOT NULL,
    user_id INTEGER NOT NULL,
    used_at TEXT    NOT NULL,
    PRIMARY KEY (code, user_id)
);
"""


class RedeemError(Exception):
    """兑换失败。

    和 `GlobalQuotaExceeded` 一样用异常而不是返回 None：
    失败原因要原样显示给用户（“已过期”“已用完”“你兑换过”），
    接口层只负责把它转成 400。
    """


@dataclass(frozen=True, slots=True)
class Profile:
    """一个用户在后台看到的全部“后端侧”信息（不含邮箱）。"""

    user_id: int
    tier: str
    tier_expires_at: str | None
    credit: int
    note: str | None
    qq_uin: str | None


@dataclass(frozen=True, slots=True)
class RedeemResult:
    """一次成功兑换的结果，接口层直接拿去拼响应。"""

    kind: str
    credit_added: int
    balance: int
    tier: str
    tier_expires_at: str | None
    message: str


def _random_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[index : index + 4] for index in range(0, CODE_LENGTH, 4))


def _normalize_code(raw: str) -> str:
    """把用户输入的码变成数据库里的标准形式。

    用户经常从聊天框粘贴，带空格、全小写、或者连字符被吞掉。
    三种情况都当成同一个码处理，不要因为格式差异让人白跑一趟。
    """
    text = "".join(str(raw or "").split()).upper()
    if "-" not in text and len(text) == CODE_LENGTH:
        text = "-".join(text[index : index + 4] for index in range(0, CODE_LENGTH, 4))
    return text


class CreditStore:
    """等级、存量额度与兑换码的 SQLite 存储。

    每个方法自己开关连接，理由和 `QuotaStore` 一样：
    SQLite 打开本地文件很便宜，省掉线程间共享连接的麻烦。
    """

    def __init__(
        self,
        db_path: str,
        timezone: str = "Asia/Shanghai",
        max_credits: int = 10_000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.timezone = ZoneInfo(timezone)
        self.max_credits = max_credits
        # now 可注入，测试里就能“把时钟拨到三天后”验证过期与续期，
        # 不用真的等时间过去，也不用 sleep。
        self._now = now or (lambda: datetime.now(self.timezone))

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(CREDIT_SCHEMA_SQL)

    def _now_iso(self) -> str:
        """带时区偏移的 ISO 时间，例如 2026-11-06T12:00:00+08:00。

        存字符串而不是 Unix 时间戳：直接看得出是哪天，
        比较时用 fromisoformat 解析，避免跨时区的字符串比较出错。
        """
        return self._now().isoformat(timespec="seconds")

    def _ensure_profile_row(self, conn: sqlite3.Connection, user_id: int) -> None:
        stamped = self._now_iso()
        conn.execute(
            """
            INSERT INTO user_profile (user_id, tier, created_at, updated_at)
            VALUES (?, 'user', ?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, stamped, stamped),
        )

    def _expired(self, expires_at: str) -> bool:
        try:
            return datetime.fromisoformat(expires_at) <= self._now()
        except ValueError:
            # 手工改坏的时间当成“已过期”，宁可降级也不要把错误的人当会员。
            logger.warning("会员到期时间无法解析，按已过期处理 value=%s", expires_at)
            return True

    # ------------------------------------------------------------------
    # 等级
    # ------------------------------------------------------------------
    def _tier_locked(
        self, conn: sqlite3.Connection, user_id: int
    ) -> tuple[str, str | None]:
        """在调用方的事务里读等级并做惰性降级。"""
        row = conn.execute(
            "SELECT tier, tier_expires_at FROM user_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return ("user", None)

        tier = str(row["tier"])
        expires = row["tier_expires_at"]
        if tier == "member" and expires and self._expired(str(expires)):
            conn.execute(
                """
                UPDATE user_profile
                   SET tier = 'user', tier_expires_at = NULL, updated_at = ?
                 WHERE user_id = ?
                """,
                (self._now_iso(), user_id),
            )
            return ("user", None)

        return (tier, str(expires) if expires else None)

    def tier_of(self, user_id: int) -> tuple[str, str | None]:
        """返回当前有效的 (等级, 到期时间)。

        读到已过期的会员时顺手改回普通用户（惰性降级），见模块开头的说明。
        """
        with self._connection() as conn:
            return self._tier_locked(conn, user_id)

    def _set_tier_locked(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        tier: str,
        days: int | None,
    ) -> str | None:
        """在调用方的事务里改等级。`redeem()` 需要它，所以不自己开事务。"""
        if tier not in TIERS:
            raise ValueError(f"未知等级：{tier}")

        expires: str | None = None
        if tier == "member":
            if days is None or days <= 0:
                raise ValueError("会员必须给出正数天数")

            base = self._now()
            row = conn.execute(
                "SELECT tier, tier_expires_at FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            # 续期从“现在”和“原到期日”里较晚的那个开始，
            # 这样提前续费不会把还没用完的时间吞掉。
            if row is not None and row["tier"] == "member" and row["tier_expires_at"]:
                current = datetime.fromisoformat(str(row["tier_expires_at"]))
                if current > base:
                    base = current
            expires = (base + timedelta(days=days)).isoformat(timespec="seconds")

        self._ensure_profile_row(conn, user_id)
        conn.execute(
            """
            UPDATE user_profile
               SET tier = ?, tier_expires_at = ?, updated_at = ?
             WHERE user_id = ?
            """,
            (tier, expires, self._now_iso(), user_id),
        )
        return expires

    def set_tier(self, user_id: int, tier: str, days: int | None = None) -> str | None:
        """设置等级，返回新的到期时间（普通用户返回 None）。"""
        with self._connection() as conn:
            return self._set_tier_locked(conn, user_id, tier, days)

    # ------------------------------------------------------------------
    # 额外额度
    # ------------------------------------------------------------------
    def balance_of(self, user_id: int) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT balance FROM credit_balance WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["balance"]) if row else 0

    def _grant_locked(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        delta: int,
        reason: str,
        ref: str | None = None,
    ) -> int:
        """在调用方的事务里增减余额，返回新余额。

        “够不够扣”和“有没有超上限”都写进 UPDATE 的 WHERE 里，
        让 SQLite 原子地判断并修改——先 SELECT 再 UPDATE 会在并发下算错。
        """
        if delta == 0:
            raise ValueError("没有需要变更的额度")

        stamped = self._now_iso()
        conn.execute(
            """
            INSERT INTO credit_balance (user_id, balance, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, stamped),
        )

        if delta > 0:
            row = conn.execute(
                """
                UPDATE credit_balance
                   SET balance = balance + ?, updated_at = ?
                 WHERE user_id = ? AND balance + ? <= ?
                RETURNING balance
                """,
                (delta, stamped, user_id, delta, self.max_credits),
            ).fetchone()
            if row is None:
                raise ValueError(f"单个用户的额外额度上限是 {self.max_credits} 条")
        else:
            row = conn.execute(
                """
                UPDATE credit_balance
                   SET balance = balance + ?, updated_at = ?
                 WHERE user_id = ? AND balance + ? >= 0
                RETURNING balance
                """,
                (delta, stamped, user_id, delta),
            ).fetchone()
            if row is None:
                raise ValueError("余额不足，不能扣成负数")

        balance = int(row["balance"])
        conn.execute(
            """
            INSERT INTO credit_ledger (user_id, delta, reason, ref, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, delta, reason, ref, stamped),
        )
        return balance

    def grant_credits(
        self,
        user_id: int,
        delta: int,
        reason: str,
        ref: str | None = None,
    ) -> int:
        """增减额外额度并记账，返回新余额。delta 为负表示扣减。"""
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                balance = self._grant_locked(conn, user_id, delta, reason, ref)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return balance

    def _known_user_ids_locked(self, conn: sqlite3.Connection) -> list[int]:
        rows = conn.execute(
            """
            SELECT user_id FROM user_profile
            UNION
            SELECT user_id FROM credit_balance
            """
        ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def known_user_ids(self) -> list[int]:
        """后端“有记录”的用户集合。

        后端不知道 WordPress 的全部用户，所以后台页只拿这个集合做合并；
        没有记录的用户按默认档案处理。
        """
        with self._connection() as conn:
            return self._known_user_ids_locked(conn)

    def profile_snapshot(self, user_ids: Iterable[int]) -> dict[int, Profile]:
        """一次取齐一批用户的等级与余额，避免后台页 N+1 查询。"""
        ids = [int(item) for item in user_ids]
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        with self._connection() as conn:
            profile_rows = conn.execute(
                f"""
                SELECT user_id, tier, tier_expires_at, note, qq_uin
                  FROM user_profile
                 WHERE user_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            balance_rows = conn.execute(
                f"""
                SELECT user_id, balance
                  FROM credit_balance
                 WHERE user_id IN ({placeholders})
                """,
                ids,
            ).fetchall()

        balances = {int(row["user_id"]): int(row["balance"]) for row in balance_rows}
        profiles: dict[int, Profile] = {}
        for row in profile_rows:
            user_id = int(row["user_id"])
            tier = str(row["tier"])
            expires = row["tier_expires_at"]
            # 列表里也要显示“到期就是普通用户”，否则后台会误以为会员还在。
            if tier == "member" and expires and self._expired(str(expires)):
                tier, expires = "user", None
            profiles[user_id] = Profile(
                user_id=user_id,
                tier=tier,
                tier_expires_at=str(expires) if expires else None,
                credit=balances.get(user_id, 0),
                note=row["note"],
                qq_uin=row["qq_uin"],
            )

        for user_id in ids:
            profiles.setdefault(
                user_id,
                Profile(
                    user_id=user_id,
                    tier="user",
                    tier_expires_at=None,
                    credit=balances.get(user_id, 0),
                    note=None,
                    qq_uin=None,
                ),
            )
        return profiles

    # ------------------------------------------------------------------
    # 兑换码
    # ------------------------------------------------------------------
    def create_codes(
        self,
        *,
        kind: str,
        value: int,
        count: int = 1,
        max_uses: int = 1,
        expires_at: str | None = None,
        note: str | None = None,
    ) -> list[str]:
        """批量生成兑换码，返回明文列表（只在这里生成，之后一直存明文）。"""
        if kind not in {"credit", "member"}:
            raise ValueError("兑换码类型只能是 credit 或 member")
        if value < 1:
            raise ValueError("面值至少为 1")
        if not 1 <= count <= 50:
            raise ValueError("一次最多生成 50 个兑换码")
        if not 1 <= max_uses <= 1000:
            raise ValueError("单个兑换码的使用次数必须在 1~1000 之间")
        if expires_at is not None:
            try:
                datetime.fromisoformat(expires_at)
            except ValueError:
                raise ValueError("到期时间格式不对，应类似 2026-11-30T00:00:00") from None

        created = self._now_iso()
        codes: list[str] = []
        with self._connection() as conn:
            for _ in range(count):
                # 撞码的概率极低，但主键冲突时会抛 IntegrityError，
                # 与其相信概率，不如重试几次。
                for attempt in range(5):
                    code = _random_code()
                    try:
                        conn.execute(
                            """
                            INSERT INTO redeem_code
                                (code, kind, value, max_uses, used_count,
                                 expires_at, note, created_at)
                            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                            """,
                            (code, kind, value, max_uses, expires_at, note, created),
                        )
                    except sqlite3.IntegrityError:
                        if attempt == 4:
                            raise
                        continue
                    codes.append(code)
                    break
        return codes

    def redeem(self, user_id: int, code: str) -> RedeemResult:
        """兑换一个码。失败抛 `RedeemError`，消息可以直接给用户看。"""
        normalized = _normalize_code(code)

        with self._connection() as conn:
            try:
                # 整个流程一个事务：校验次数、写兑换记录、发额度必须同时成功，
                # 否则会出现“次数用掉了但额度没到账”。
                conn.execute("BEGIN IMMEDIATE")

                row = conn.execute(
                    "SELECT kind, value, max_uses, used_count, expires_at "
                    "FROM redeem_code WHERE code = ?",
                    (normalized,),
                ).fetchone()
                if row is None:
                    raise RedeemError("兑换码不存在")

                expires_at = row["expires_at"]
                if expires_at and self._expired(str(expires_at)):
                    raise RedeemError("这个兑换码已经过期了")
                if int(row["used_count"]) >= int(row["max_uses"]):
                    raise RedeemError("这个兑换码已经被用完了")

                already = conn.execute(
                    "SELECT 1 FROM redeem_log WHERE code = ? AND user_id = ?",
                    (normalized, user_id),
                ).fetchone()
                if already is not None:
                    raise RedeemError("你已经兑换过这个码了")

                updated = conn.execute(
                    """
                    UPDATE redeem_code
                       SET used_count = used_count + 1
                     WHERE code = ? AND used_count < max_uses
                    """,
                    (normalized,),
                )
                if updated.rowcount == 0:
                    # 并发下另一个请求刚把这个码的最后一次用掉。
                    raise RedeemError("这个兑换码已经被用完了")

                conn.execute(
                    "INSERT INTO redeem_log (code, user_id, used_at) VALUES (?, ?, ?)",
                    (normalized, user_id, self._now_iso()),
                )

                kind = str(row["kind"])
                value = int(row["value"])
                if kind == "credit":
                    credit_added = self._grant_locked(
                        conn, user_id, value, "redeem", normalized
                    )
                else:
                    self._set_tier_locked(conn, user_id, "member", value)
                    credit_added = 0

                tier, tier_expires_at = self._tier_locked(conn, user_id)
                balance_row = conn.execute(
                    "SELECT balance FROM credit_balance WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                balance = int(balance_row["balance"]) if balance_row else 0

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if kind == "credit":
            message = f"兑换成功，当前共有 {balance} 条额外额度"
        else:
            message = f"会员已延长到 {tier_expires_at}"

        return RedeemResult(
            kind=kind,
            credit_added=credit_added,
            balance=balance,
            tier=tier,
            tier_expires_at=tier_expires_at,
            message=message,
        )

    def recent_codes(self, limit: int = 50) -> list[dict[str, object]]:
        """后台列表用：最近的兑换码和使用情况。"""
        bounded = max(1, min(limit, 200))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT code, kind, value, max_uses, used_count,
                       expires_at, note, created_at
                  FROM redeem_code
                 ORDER BY created_at DESC, rowid DESC
                 LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]


@lru_cache(maxsize=1)
def get_credit_store() -> CreditStore:
    """全局唯一的等级/额度存储。

    测试改了环境变量后要调用 `get_credit_store.cache_clear()`，
    否则会继续用上一个测试的数据库路径。
    """
    settings = get_settings()
    return CreditStore(settings.quota_db_path, settings.quota_timezone)
