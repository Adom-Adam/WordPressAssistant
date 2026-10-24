"""从 WordPress 同步文章，建立本地知识库。

运行方式（在 backend/ 目录下）：

    uv run python -m app.kb.sync
    uv run python -m app.kb.sync --site https://374969782.xyz
    uv run python -m app.kb.sync --no-vision   # 不调用视觉模型，只索引现有文字/alt

同步流程：
    1. 通过 WordPress REST API 分页拉取已发布文章；
    2. 把 HTML 正文清洗成纯文本，按 600 字左右切块；
    3. 找出文章里的图片，调用 DeepSeek 视觉模型生成描述/OCR；
    4. 把文本块和图片描述一起写入 SQLite FTS5；
    5. 打印统计信息。

为什么默认“全量重建”：
    文章只有几十篇，重建一次几秒钟；全量重建能自然处理“文章被删除/
    改标题/删图片”等情况，不会留下旧索引。图片描述有缓存，不会重复花钱。
    以后文章上千篇时，再改成按 modified_gmt 增量更新。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.kb import db, vision
from app.kb.text import (
    chunk_text,
    extract_images,
    html_to_text,
    tokenize,
)

logger = logging.getLogger(__name__)

# WordPress REST 的 _fields 参数：只要这些字段，减少响应体积。
# content.rendered 是已经过 WordPress 过滤器处理的 HTML。
_FIELDS = "id,link,title,content,modified,modified_gmt,status,featured_media"


def _candidate_endpoints(site_url: str) -> list[tuple[str, dict[str, str]]]:
    """列出 REST 接口的候选地址。

    不同 WordPress 站点的固定链接/伪静态配置不同，常见有三种：
    1. /wp-json/wp/v2/posts                —— 标准写法；
    2. /index.php/wp-json/wp/v2/posts      —— 没开 rewrite 但 index.php 可用；
    3. /?rest_route=/wp/v2/posts           —— 完全依赖 query 参数。

    本项目实际测试的站点 374969782.xyz 属于第 2/3 种：
    直接访问 /wp-json/ 会被 Nginx 返回 404，但另外两种正常。
    """

    site = site_url.rstrip("/")
    return [
        (f"{site}/wp-json/wp/v2/posts", {}),
        (f"{site}/index.php/wp-json/wp/v2/posts", {}),
        (f"{site}/", {"rest_route": "/wp/v2/posts"}),
    ]


def _pick_endpoint(
    client: httpx.Client,
    site_url: str,
    per_page: int,
) -> tuple[str, dict[str, str]]:
    """依次探测候选接口，返回第一个能正常返回 JSON 数组的。"""

    errors: list[str] = []
    for url, base_params in _candidate_endpoints(site_url):
        try:
            response = client.get(
                url,
                params={
                    **base_params,
                    "per_page": min(per_page, 10),
                    "page": 1,
                    "_fields": "id,link,title",
                },
            )
        except httpx.HTTPError as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")
            continue

        if response.status_code != 200:
            errors.append(f"{url} -> HTTP {response.status_code}")
            continue

        try:
            data = response.json()
        except ValueError:
            errors.append(f"{url} -> 返回的不是 JSON")
            continue

        if isinstance(data, list):
            logger.info("使用 REST 接口：%s", url)
            return url, base_params

        errors.append(f"{url} -> JSON 不是文章数组")

    detail = "\n  ".join(errors)
    raise RuntimeError(
        "找不到可用的 WordPress REST 接口。请确认站点地址、REST API 是否被安全插件关闭。\n"
        f"尝试过：\n  {detail}"
    )


def fetch_posts(
    site_url: str,
    per_page: int = 100,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    """拉取全部已发布文章（自动分页）。"""

    settings = get_settings()
    per_page = max(1, min(per_page, 100))  # WordPress 单页上限是 100

    headers = {"User-Agent": settings.kb_user_agent, "Accept": "application/json"}
    with httpx.Client(
        timeout=settings.kb_request_timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        url, base_params = _pick_endpoint(client, site_url, per_page)

        posts: list[dict[str, Any]] = []
        page = 1
        total_pages = 1

        while page <= min(total_pages, max_pages):
            params = {
                **base_params,
                "per_page": per_page,
                "page": page,
                "_fields": _FIELDS,
            }
            response = client.get(url, params=params)

            # 页码超出范围时 WordPress 会返回 400，这是正常的结束信号。
            if response.status_code == 400 and page > 1:
                break
            response.raise_for_status()

            batch = response.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"REST 返回结构异常：期望数组，实际是 {type(batch).__name__}")

            posts.extend(batch)

            # WordPress 会在响应头里给出总页数；没有的话就靠“本页是否满页”判断。
            header_total = response.headers.get("x-wp-totalpages")
            if header_total and header_total.isdigit():
                total_pages = int(header_total)
            elif len(batch) < per_page:
                total_pages = page
            else:
                total_pages = page + 1

            logger.info("已拉取第 %s/%s 页，累计 %s 篇", page, total_pages, len(posts))
            if len(batch) < per_page:
                break
            page += 1

        return posts


def _post_title(post: dict[str, Any]) -> str:
    """从 REST 的 title.rendered 中取出纯文本标题。"""

    title = post.get("title")
    if isinstance(title, dict):
        raw = str(title.get("rendered") or "")
    else:
        raw = str(title or "")
    return html_to_text(raw) or f"文章 {post.get('id')}"


def _image_fallback_text(image_url: str, alt: str, caption: str, title: str) -> str:
    """视觉模型不可用时的图片兜底文本。

    虽然信息少，但至少让图片的 alt、图注和文件名进入索引。
    """

    parts = ["[图片]"]
    if alt:
        parts.append(f"alt：{alt}")
    if caption:
        parts.append(f"图注：{caption}")
    if not alt and not caption:
        # URL 的最后一段通常包含文件名，例如 image-20220528.png
        filename = image_url.rstrip("/").split("/")[-1]
        parts.append(f"文件名：{filename}")
    if title:
        parts.append(f"所在文章：{title}")
    return "\n".join(parts)


def sync(
    site_url: str | None = None,
    *,
    with_vision: bool = True,
    limit: int | None = None,
    per_page: int = 100,
) -> dict[str, int]:
    """执行一次完整同步，返回知识库统计。"""

    settings = get_settings()
    site_url = (site_url or settings.wp_site_url).strip()
    if not site_url:
        raise RuntimeError(
            "没有配置站点地址。请在 backend/.env 里设置 WP_SITE_URL，"
            "或运行 uv run python -m app.kb.sync --site https://你的域名"
        )

    db.init_schema()
    posts = fetch_posts(site_url, per_page=per_page)
    if limit is not None:
        posts = posts[: max(0, limit)]

    print(f"共拉取到 {len(posts)} 篇文章，开始建立索引…")

    conn = db.connect()
    total_images = 0
    vision_used = 0
    try:
        # 全量重建：清空文章和索引，但保留图片描述缓存。
        db.clear_index(conn)

        for index, post in enumerate(posts, start=1):
            post_id = int(post.get("id") or 0)
            if not post_id:
                logger.warning("跳过没有 id 的文章：%s", post)
                continue

            title = _post_title(post)
            url = str(post.get("link") or "")
            modified = str(post.get("modified_gmt") or post.get("modified") or "")

            content_html = ""
            content = post.get("content")
            if isinstance(content, dict):
                content_html = str(content.get("rendered") or "")

            text = html_to_text(content_html)
            images = extract_images(content_html, url)

            rows: list[tuple[Any, ...]] = []

            # ---------- 正文块 ----------
            text_chunks = chunk_text(
                text,
                size=settings.kb_chunk_size,
                overlap=settings.kb_chunk_overlap,
            )
            for chunk_index, chunk in enumerate(text_chunks):
                # 把标题也拼进 tokens，这样“标题里有关键词”的文章排序更靠前。
                tokens = tokenize(f"{title}\n{chunk}")
                rows.append(
                    (
                        post_id,
                        chunk_index,
                        "text",
                        None,
                        url,
                        title,
                        chunk,
                        " ".join(tokens),
                    )
                )

            # ---------- 图片块 ----------
            image_count = 0
            for image_index, image in enumerate(images):
                total_images += 1
                image_count += 1

                description: dict[str, Any] | None = None
                if with_vision:
                    description = vision.describe_image(image, title=title)
                    if description is not None:
                        # 缓存命中时 describe_image 也会返回结果，
                        # 这里用日志区分“本次是否真的调用了模型”意义不大，
                        # 只统计有描述的图片数量即可。
                        vision_used += 1

                if description is not None:
                    body_lines = [f"[图片] {description.get('description', '')}"]
                    ocr_text = str(description.get("ocr_text") or "").strip()
                    keywords = description.get("keywords") or []
                    if ocr_text:
                        body_lines.append(f"图片文字：{ocr_text}")
                    if keywords:
                        body_lines.append("关键词：" + "、".join(str(item) for item in keywords))
                    if image.alt:
                        body_lines.append(f"alt：{image.alt}")
                    if image.caption:
                        body_lines.append(f"图注：{image.caption}")
                    body = "\n".join(body_lines)
                    extra_tokens = " ".join(str(item) for item in keywords)
                else:
                    body = _image_fallback_text(image.url, image.alt, image.caption, title)
                    extra_tokens = ""

                tokens = tokenize(
                    f"{title}\n{body}\n{extra_tokens}\n{image.alt}\n{image.caption}"
                )
                rows.append(
                    (
                        post_id,
                        len(text_chunks) + image_index,
                        "image",
                        image.url,
                        url,
                        title,
                        body,
                        " ".join(tokens),
                    )
                )

            if not rows:
                logger.info("文章《%s》没有可索引的正文或图片，跳过", title)
                continue

            # 每篇文章一个事务：要么整篇更新成功，要么保持原样。
            with conn:
                conn.execute("DELETE FROM chunks WHERE post_id = ?", (post_id,))
                conn.executemany(
                    """
                    INSERT INTO chunks
                        (post_id, chunk_index, kind, image_url, url, title, body, tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.execute(
                    """
                    INSERT INTO posts (post_id, title, url, modified, synced_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(post_id) DO UPDATE SET
                        title     = excluded.title,
                        url       = excluded.url,
                        modified  = excluded.modified,
                        synced_at = excluded.synced_at
                    """,
                    (
                        post_id,
                        title,
                        url,
                        modified,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )

            print(
                f"[{index}/{len(posts)}] 《{title}》 "
                f"正文 {len(text_chunks)} 块，图片 {image_count} 张"
            )

        statistics = db.stats(conn)
    finally:
        conn.close()

    print("\n同步完成：")
    print(f"  文章：{statistics['posts']} 篇")
    print(f"  文本块：{statistics['text_chunks']} 个")
    print(f"  图片块：{statistics['image_chunks']} 个（本次处理图片 {total_images} 张）")
    print(f"  图片描述缓存：{statistics['images']} 条")
    if with_vision:
        print(f"  本次有描述的图片：{vision_used} 张")
    print(f"  数据库：{db.resolve_db_path()}")
    return statistics


def main() -> None:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="同步 WordPress 文章到本地知识库")
    parser.add_argument("--site", help="站点地址，默认读取 WP_SITE_URL")
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="不调用视觉模型，只用 alt/图注/文件名索引图片",
    )
    parser.add_argument("--limit", type=int, help="只同步前 N 篇，方便调试")
    parser.add_argument("--per-page", type=int, default=100, help="每页拉取数量（最大 100）")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        sync(
            site_url=args.site,
            with_vision=not args.no_vision,
            limit=args.limit,
            per_page=args.per_page,
        )
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 - 命令行入口需要给出友好提示
        logger.exception("同步失败")
        print(f"同步失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
