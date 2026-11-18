"""把 `data/stickers/inbox/` 里的图片导入表情库。

用法（在 `backend/` 下）：

    python -m app.stickers.sync            # 识别、打标、入库
    python -m app.stickers.sync --dry-run  # 只看会导入哪些
    python -m app.stickers.sync --no-vision

识别失败时仍然导入，只是没有标签，后台可以补。同一张图按内容哈希去重，
重复执行是幂等的。inbox 里的原文件不会删除。
"""

from __future__ import annotations

import argparse
import hashlib
import sys

from app.config import get_settings
from app.memory import db as memory_db
from app.stickers import files, store
from app.stickers.tagging import analyze_bytes
from app.vision_common import guess_mime


def _origin_names() -> list[str]:
    conn = memory_db.connect()
    try:
        memory_db.init_schema(conn)
        names: list[str] = []
        for item in store.list_origins(conn):
            names.append(str(item["name"]))
            names.extend(str(alias) for alias in item["aliases"])
        return names
    finally:
        conn.close()


def import_inbox(
    *, with_vision: bool = True, dry_run: bool = False
) -> dict[str, int]:
    files.ensure_dirs()
    stats = {"scanned": 0, "imported": 0, "skipped": 0, "failed": 0, "untagged": 0}
    origins = _origin_names()

    for path in sorted(files.inbox_dir().iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        stats["scanned"] += 1
        try:
            data = path.read_bytes()
            mime = guess_mime(data)
            if not mime:
                stats["failed"] += 1
                print(f"跳过（不是支持的图片）：{path.name}")
                continue
        except OSError:
            stats["failed"] += 1
            print(f"读不了：{path.name}")
            continue

        conn = memory_db.connect()
        try:
            memory_db.init_schema(conn)
            existing = store.get_by_hash(
                conn, hashlib.sha256(data).hexdigest()
            )
        finally:
            conn.close()
        if existing is not None:
            stats["skipped"] += 1
            print(f"已存在（#{existing.id}）：{path.name}")
            continue

        if dry_run:
            stats["imported"] += 1
            print(f"会导入：{path.name}")
            continue

        analysis = (
            analyze_bytes(data, mime, known_origins=origins) if with_vision else None
        )
        if analysis is None:
            stats["untagged"] += 1
            description = path.stem
            tone = "neutral"
            tags: list[str] = []
            origin = ""
            proposed = ""
        else:
            description = str(analysis.get("description") or path.stem)
            tone = str(analysis.get("tone") or "neutral")
            tags = list(analysis.get("emotions") or [])
            origin = str(analysis.get("known_origin") or "")
            proposed = "" if origin else str(analysis.get("origin") or "")

        content_hash, file_name, detected = files.save_bytes(data, mime)
        conn = memory_db.connect()
        try:
            memory_db.init_schema(conn)
            sticker_id = store.add_sticker(
                conn,
                content_hash=content_hash,
                file_name=file_name,
                format=detected,
                description=description,
                tone=tone,
                origin=origin or None,
                proposed_origin=proposed,
                tags=tags,
                source="import",
            )
            conn.commit()
        finally:
            conn.close()
        stats["imported"] += 1
        print(f"已导入 #{sticker_id}：{path.name}{'（未打标）' if analysis is None else ''}")

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 inbox 里的表情素材")
    parser.add_argument("--no-vision", action="store_true", help="不调用视觉模型")
    parser.add_argument("--dry-run", action="store_true", help="只列出会导入的文件")
    args = parser.parse_args(argv)

    settings = get_settings()
    with_vision = not args.no_vision
    if with_vision and not settings.vision_enabled:
        print("VISION_ENABLED=false，本次不调用视觉模型")
        with_vision = False

    stats = import_inbox(with_vision=with_vision, dry_run=args.dry_run)
    print(
        "完成：扫描 {scanned}，导入 {imported}，已存在 {skipped}，"
        "未打标 {untagged}，失败 {failed}".format(**stats)
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
