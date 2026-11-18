"""表情库的 SQLite 存取。

三个概念分开：
- `stickers`：一张素材，tone 表示感情色彩（warm / neutral / cold）；
- `sticker_tags`：情绪和自由标签，多对多；
- `sticker_origins`：出处词表（原神、鸣潮……），带别名，后台负责合并。

素材文件本身不在库里，只在 `file_name` 记一个文件名；文件放在
`data/stickers/library/` 下。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

TONES = ("warm", "neutral", "cold")
DEFAULT_TONE = "neutral"

# 更新时用来区分“不改这个字段”和“明确清空”。
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_tag(tag: str) -> str:
    return " ".join(str(tag or "").split()).strip()


def _dedupe_tags(tags: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in tags:
        tag = _normalize_tag(raw)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _split_aliases(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


@dataclass(frozen=True, slots=True)
class Sticker:
    id: int
    content_hash: str
    file_name: str
    format: str
    description: str
    tone: str
    origin_id: int | None
    origin_name: str
    proposed_origin: str
    enabled: bool
    source: str
    source_person: str
    tags: tuple[str, ...]
    created_at: str
    updated_at: str


def normalize_tone(tone: str) -> str:
    value = str(tone or "").strip().lower()
    return value if value in TONES else DEFAULT_TONE


# ----------------------------------------------------------------------
# 出处
# ----------------------------------------------------------------------
def ensure_origin(
    conn: sqlite3.Connection, name: str, aliases: list[str] | None = None
) -> int:
    """按名字或别名找现有出处；没有就新建。返回 origin_id。"""
    clean = " ".join(str(name or "").split()).strip()
    if not clean:
        raise ValueError("出处不能为空")

    for row in conn.execute("SELECT id, name, aliases FROM sticker_origins"):
        names = [str(row["name"]), *_split_aliases(row["aliases"])]
        if any(item.casefold() == clean.casefold() for item in names):
            if aliases:
                merged = _split_aliases(str(row["aliases"]))
                for alias in aliases:
                    alias = alias.strip()
                    if alias and alias.casefold() not in {
                        item.casefold() for item in [str(row["name"]), *merged]
                    }:
                        merged.append(alias)
                conn.execute(
                    "UPDATE sticker_origins SET aliases = ? WHERE id = ?",
                    (",".join(merged), int(row["id"])),
                )
            return int(row["id"])

    cursor = conn.execute(
        "INSERT INTO sticker_origins (name, aliases, created_at) VALUES (?, ?, ?)",
        (clean, ",".join(aliases or []), _now()),
    )
    return int(cursor.lastrowid or 0)


def update_origin(
    conn: sqlite3.Connection, origin_id: int, *, name: str, aliases: list[str]
) -> bool:
    clean = " ".join(str(name or "").split()).strip()
    if not clean:
        raise ValueError("出处不能为空")
    cursor = conn.execute(
        "UPDATE sticker_origins SET name = ?, aliases = ? WHERE id = ?",
        (clean, ",".join(_dedupe_tags(aliases)), int(origin_id)),
    )
    return cursor.rowcount > 0


def delete_origin(conn: sqlite3.Connection, origin_id: int) -> bool:
    conn.execute(
        "UPDATE stickers SET origin_id = NULL WHERE origin_id = ?", (int(origin_id),)
    )
    cursor = conn.execute(
        "DELETE FROM sticker_origins WHERE id = ?", (int(origin_id),)
    )
    return cursor.rowcount > 0


def list_origins(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT id, name, aliases FROM sticker_origins ORDER BY name"
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "aliases": _split_aliases(row["aliases"]),
        }
        for row in rows
    ]


# ----------------------------------------------------------------------
# 素材
# ----------------------------------------------------------------------
def _replace_tags(conn: sqlite3.Connection, sticker_id: int, tags: list[str]) -> None:
    conn.execute("DELETE FROM sticker_tags WHERE sticker_id = ?", (sticker_id,))
    for tag in _dedupe_tags(tags):
        conn.execute(
            "INSERT INTO sticker_tags (sticker_id, tag, weight) VALUES (?, ?, 1.0)",
            (sticker_id, tag),
        )


def add_sticker(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    file_name: str,
    format: str,
    description: str = "",
    tone: str = DEFAULT_TONE,
    origin: str | None = None,
    proposed_origin: str = "",
    tags: list[str] | tuple[str, ...] = (),
    enabled: bool = True,
    source: str = "manual",
    source_person: str | None = None,
    now: str | None = None,
) -> int:
    """新增素材；同一内容哈希直接返回已有的 id。"""
    existing = conn.execute(
        "SELECT id FROM stickers WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    stamp = now or _now()
    origin_id = ensure_origin(conn, origin) if origin else None
    cursor = conn.execute(
        """INSERT INTO stickers
               (content_hash, file_name, format, description, tone, origin_id,
                proposed_origin, enabled, source, source_person, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            content_hash,
            file_name,
            format,
            str(description or "").strip(),
            normalize_tone(tone),
            origin_id,
            str(proposed_origin or "").strip(),
            1 if enabled else 0,
            source,
            source_person,
            stamp,
            stamp,
        ),
    )
    sticker_id = int(cursor.lastrowid or 0)
    _replace_tags(conn, sticker_id, list(tags))
    return sticker_id


def update_sticker(
    conn: sqlite3.Connection,
    sticker_id: int,
    *,
    description: str | None = None,
    tone: str | None = None,
    origin: str | None | object = _UNSET,
    tags: list[str] | tuple[str, ...] | None = None,
    enabled: bool | None = None,
    now: str | None = None,
) -> bool:
    """更新素材。`origin` 传 None 表示清空出处，传字符串表示改名/新建。"""
    row = conn.execute("SELECT id FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
    if row is None:
        return False

    assignments: list[str] = []
    params: list[object] = []
    if description is not None:
        assignments.append("description = ?")
        params.append(str(description).strip())
    if tone is not None:
        assignments.append("tone = ?")
        params.append(normalize_tone(tone))
    if origin is not _UNSET:
        assignments.append("origin_id = ?")
        params.append(ensure_origin(conn, str(origin)) if origin else None)
        if origin:
            assignments.append("proposed_origin = ''")
    if enabled is not None:
        assignments.append("enabled = ?")
        params.append(1 if enabled else 0)
    assignments.append("updated_at = ?")
    params.append(now or _now())

    conn.execute(
        f"UPDATE stickers SET {', '.join(assignments)} WHERE id = ?",
        [*params, sticker_id],
    )
    if tags is not None:
        _replace_tags(conn, sticker_id, list(tags))
    return True


def delete_sticker(conn: sqlite3.Connection, sticker_id: int) -> bool:
    conn.execute("DELETE FROM sticker_tags WHERE sticker_id = ?", (sticker_id,))
    cursor = conn.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
    return cursor.rowcount > 0


def _row_to_sticker(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> Sticker:
    tags = tuple(
        str(item["tag"])
        for item in conn.execute(
            "SELECT tag FROM sticker_tags WHERE sticker_id = ? ORDER BY tag",
            (row["id"],),
        )
    )
    origin_name = ""
    if row["origin_id"] is not None:
        origin = conn.execute(
            "SELECT name FROM sticker_origins WHERE id = ?", (row["origin_id"],)
        ).fetchone()
        origin_name = str(origin["name"]) if origin is not None else ""
    return Sticker(
        id=int(row["id"]),
        content_hash=str(row["content_hash"]),
        file_name=str(row["file_name"]),
        format=str(row["format"]),
        description=str(row["description"]),
        tone=str(row["tone"]),
        origin_id=row["origin_id"],
        origin_name=origin_name,
        proposed_origin=str(row["proposed_origin"] or ""),
        enabled=bool(row["enabled"]),
        source=str(row["source"]),
        source_person=str(row["source_person"] or ""),
        tags=tags,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def get_sticker(conn: sqlite3.Connection, sticker_id: int) -> Sticker | None:
    row = conn.execute("SELECT * FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
    return _row_to_sticker(conn, row) if row is not None else None


def get_by_hash(conn: sqlite3.Connection, content_hash: str) -> Sticker | None:
    row = conn.execute(
        "SELECT * FROM stickers WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return _row_to_sticker(conn, row) if row is not None else None


def list_stickers(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    tone: str | None = None,
    enabled: bool | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Sticker]:
    clauses: list[str] = []
    params: list[object] = []
    text = str(query or "").strip()
    if text:
        clauses.append(
            """(s.description LIKE ? OR s.id IN (
                   SELECT st.sticker_id FROM sticker_tags st WHERE st.tag LIKE ?
               ) OR o.name LIKE ? OR o.aliases LIKE ?)"""
        )
        pattern = f"%{text}%"
        params.extend([pattern, pattern, pattern, pattern])
    if tone:
        clauses.append("s.tone = ?")
        params.append(normalize_tone(tone))
    if enabled is not None:
        clauses.append("s.enabled = ?")
        params.append(1 if enabled else 0)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""SELECT s.* FROM stickers s
              LEFT JOIN sticker_origins o ON o.id = s.origin_id
              {where}
             ORDER BY s.id DESC
             LIMIT ? OFFSET ?""",
        [*params, max(1, int(limit)), max(0, int(offset))],
    ).fetchall()
    return [_row_to_sticker(conn, row) for row in rows]


def count_stickers(
    conn: sqlite3.Connection, *, enabled: bool | None = None
) -> int:
    if enabled is None:
        row = conn.execute("SELECT COUNT(*) FROM stickers").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM stickers WHERE enabled = ?",
            (1 if enabled else 0,),
        ).fetchone()
    return int(row[0])


def sticker_tags(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["tag"])
        for row in conn.execute(
            "SELECT DISTINCT tag FROM sticker_tags ORDER BY tag"
        )
    ]
