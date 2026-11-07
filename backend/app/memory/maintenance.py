"""记忆的日常维护：重要性衰减、清理过期约定。

为什么需要衰减：`importance` 在写入时由模型给出，之后不会自己变化。
半年前很重要的事，今天可能已经不重要了；如果一直保持原始权重，
检索结果会越来越被“老记忆”占据。

为什么清理过期约定：`promise` 会被无条件放进提示词（最多 3 条）。
“下周告诉你”这种约定一旦过期还留着，模型每次都会提起一件已经过去的事。

两种维护都是幂等的，可以每天跑一次，也可以在命令行手动跑：

    cd backend
    uv run python -m app.memory.maintenance
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from datetime import datetime, timezone

from app.config import get_settings
from app.memory import db
from app.memory.vectors import MEMORIES_VEC

logger = logging.getLogger(__name__)

MIN_IMPORTANCE = 0.05

# meta 表里记录上次衰减的时间。没有它，每天都按“绝对年龄”乘一次因子，
# 一周后就会变成几何级数衰减：0.5^(1/90) 连续乘 30 次远小于 0.5^(30/90)。
_LAST_DECAY_KEY = "maintenance:last_decay"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def decay_importance(
    conn: sqlite3.Connection,
    *,
    half_life_days: float = 90.0,
    now: datetime | None = None,
) -> int:
    """按“每过一个半衰期减半”衰减重要性，返回实际更新的行数。

    只处理至少一天没衰减过的行（不是“没更新过”）：一条记忆可能天天被
    检索，但重要性仍然应该随时间下降。上次衰减的时间存在 meta 表里，
    每次只按“距上次衰减的间隔”乘因子，连续跑 N 次恰好等于跑一次；
    否则每天按绝对年龄乘一次，几天内就会把记忆全部压到下限。

    衰减有下限 `MIN_IMPORTANCE`，避免长期记忆彻底归零、再也排不上来。
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days 必须大于 0")

    moment = now or datetime.now(timezone.utc)
    last_row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_LAST_DECAY_KEY,)
    ).fetchone()
    last_decay = _parse_time(last_row[0]) if last_row is not None else None

    rows = conn.execute(
        "SELECT id, importance, updated_at, created_at FROM memories"
    ).fetchall()

    changed = 0
    for row in rows:
        stamp = _parse_time(row["updated_at"]) or _parse_time(row["created_at"])
        if stamp is None:
            continue
        # 上次衰减之后新写/更新过的记忆，从它自己的时间算起；
        # 其余记忆从上次衰减的时间算起，保证因子只乘一次。
        base = stamp
        if last_decay is not None and stamp < last_decay:
            base = last_decay
        age_days = (moment - base).total_seconds() / 86400.0
        if age_days < 1:
            continue

        importance = max(float(row["importance"]), MIN_IMPORTANCE)
        decayed = max(
            MIN_IMPORTANCE,
            round(importance * (0.5 ** (age_days / half_life_days)), 4),
        )
        if decayed >= importance - 1e-6:
            continue

        conn.execute(
            "UPDATE memories SET importance = ? WHERE id = ?",
            (decayed, int(row["id"])),
        )
        changed += 1

    # 无论这次是否真的衰减了行，都要记下时间：本次检查已经把“截至现在”
    # 的衰减算过了，下次从这一刻重新起算。
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_LAST_DECAY_KEY, moment.isoformat(timespec="seconds")),
    )
    conn.commit()
    return changed


def expire_promises(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> int:
    """删除已经过了 `valid_until` 的约定，返回删除条数。

    ISO 时间字符串可以直接比较大小，不需要在 Python 里逐条解析。
    """
    moment = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM memories "
            "WHERE type = 'promise' AND valid_until IS NOT NULL AND valid_until < ?",
            (moment,),
        ).fetchall()
    ]
    if not ids:
        return 0

    MEMORIES_VEC.delete_ids(conn, ids)
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
    conn.commit()
    return len(ids)


def run(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """跑一遍全部维护任务，返回两个计数。"""
    own = conn is None
    if conn is None:
        db.init_schema()
        conn = db.connect()
    try:
        return {
            "decayed": decay_importance(conn),
            "expired": expire_promises(conn),
        }
    finally:
        if own and conn is not None:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="记忆维护：衰减重要性、清理过期约定")
    parser.add_argument(
        "--db",
        default=None,
        help="记忆库路径；不填则使用配置里的 MEMORY_DB_PATH",
    )
    args = parser.parse_args()

    if args.db:
        # 配置对象是 lru_cache 的：先改环境变量再清缓存，
        # db.connect() 才会读到命令行给的路径。
        os.environ["MEMORY_DB_PATH"] = args.db
        get_settings.cache_clear()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run()
    print(
        "记忆维护完成：衰减 {decayed} 条，清理过期约定 {expired} 条".format(**result)
    )


if __name__ == "__main__":
    main()
