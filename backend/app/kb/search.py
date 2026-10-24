"""查询本地知识库。

检索流程：
    用户问题
      → jieba 分词
      → 拼成 FTS5 的 MATCH 表达式
      → SQLite 用 BM25 排序
      → 按文章去重，取最相关的前 N 条
      → 返回标题、链接、摘要、图片地址

返回结构直接对应前端的“来源卡片”：
    {
        "post_id": 42,
        "title": "Docker 入门",
        "url": "https://blog.example.com/archives/42/",
        "excerpt": "……",
        "kind": "text" / "image",
        "image_url": "https://.../cover.png" 或 None,
        "score": 0.83
    }

注意：这里的 URL 全部来自 WordPress 同步时保存的元数据，
不是模型生成的，所以前端跳转不会出现“编造链接”。
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from app.config import get_settings
from app.kb import db
from app.kb.text import build_match_query, make_excerpt, tokenize

# bm25() 的权重按 FTS5 表的列顺序传入：
# post_id, chunk_index, kind, image_url, url, title, body, tokens
# 前五列是 UNINDEXED，没有词频，权重填 0 即可。
# title 权重最高（8.0），tokens 次之（3.0），body 最低（1.0）。
_BM25 = "bm25(chunks, 0, 0, 0, 0, 0, 8.0, 1.0, 3.0)"

_SELECT = f"""
    SELECT
        post_id,
        chunk_index,
        kind,
        image_url,
        url,
        title,
        body,
        {_BM25} AS rank
    FROM chunks
"""


def _row_to_result(row: sqlite3.Row, tokens: list[str]) -> dict[str, Any]:
    """把一行 SQLite 结果转成前端/工具使用的字典。"""

    body = str(row["body"] or "")
    kind = str(row["kind"] or "text")
    if kind == "image":
        # 图片块的 body 末尾有一行“关键词：a、b、c”，只用于检索，
        # 不适合展示在来源摘要里，所以截取时先去掉它。
        display_text = body.split("\n关键词：", 1)[0]
        excerpt = make_excerpt(display_text, tokens) or display_text[:160]
    else:
        excerpt = make_excerpt(body, tokens)

    return {
        "post_id": int(row["post_id"]),
        "title": str(row["title"] or ""),
        "url": str(row["url"] or ""),
        "excerpt": excerpt,
        "kind": "image" if kind == "image" else "article",
        "image_url": row["image_url"] if kind == "image" else None,
        # rank 是 BM25 分数，越小越相关；取负数变成“越大越好”，方便阅读。
        "score": round(-float(row["rank"]), 6),
    }


def _title_bonus(title: str, tokens: list[str]) -> float:
    """标题命中加成。

    为什么需要额外的加成：
    我们的 MATCH 只查 `tokens` 列（标题和正文的分词都拼在这一列），
    所以 bm25() 的 title 权重实际上永远不会参与计算。
    而 FTS5 的词频在“每个词只存一次”的 tokens 列里几乎恒定，
    分数差主要来自文章长度——长文章反而更吃亏，
    于是“ROS2 节点”可能先返回 RQT，而不是《ROS2学习记录（2）-节点》。

    这里做一个简单、可解释的二次排序：
    标题里出现的查询词越多，加分越高；完全命中时最多加 1.5 分。
    BM25 分数本身的差距通常小于 0.5，所以标题命中会明显排到前面。
    """

    if not tokens:
        return 0.0
    title_lower = title.lower()
    hits = sum(1 for token in tokens if token in title_lower)
    return 1.5 * hits / len(tokens)


def _dedupe_by_post(rows: list[sqlite3.Row], tokens: list[str]) -> list[dict[str, Any]]:
    """同一篇文章可能命中多个块，只保留最相关的一条。

    如果这篇文章的文字块先被保留，但后面又出现了图片块，
    就把图片地址补到结果上，让来源卡片能显示缩略图。
    最后按“BM25 分数 + 标题命中加成”重新排序，
    让标题就包含关键词的文章排在最前面。
    """

    results: dict[int, dict[str, Any]] = {}
    order: list[int] = []

    for row in rows:
        post_id = int(row["post_id"])
        if post_id not in results:
            results[post_id] = _row_to_result(row, tokens)
            order.append(post_id)
            continue

        # 已经有文字结果时，附加一张命中的图片作为缩略图。
        if row["kind"] == "image" and not results[post_id].get("image_url"):
            results[post_id]["image_url"] = row["image_url"]

    deduped = [results[post_id] for post_id in order]
    for item in deduped:
        item["score"] = round(
            float(item["score"]) + _title_bonus(item["title"], tokens), 6
        )

    # score 越大越相关；分数相同时保持原来的 FTS 顺序（sorted 是稳定排序）。
    deduped.sort(key=lambda item: item["score"], reverse=True)
    return deduped


def _run_fts(
    conn: sqlite3.Connection,
    match_query: str,
    candidate_limit: int,
) -> list[sqlite3.Row]:
    """执行一次 FTS5 查询，返回原始行。"""

    return list(
        conn.execute(
            _SELECT + " WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
            (match_query, candidate_limit),
        )
    )


def _matched_token_count(text: str, tokens: list[str]) -> int:
    """统计有多少个查询词真的出现在结果文本里。

    FTS5 的 OR 查询只要命中一个词就会返回结果。对“不存在的内容xyz”
    这种问题，可能只因为“存在”或“xyz”碰巧出现在代码里就返回文章，
    看起来像答非所问。这里用命中比例做二次过滤：
    - 2 个词：要求至少 2 个都出现；
    - 3 个词：至少 2 个；
    - 4 个词：至少 3 个，以此类推。
    """

    lower = text.lower()
    return sum(1 for token in tokens if token in lower)


def search_site(query: str, limit: int | None = None) -> list[dict[str, Any]]:
    """检索站内文章，返回最多 limit 条来源。"""

    query = (query or "").strip()
    if not query:
        return []

    settings = get_settings()
    limit = max(1, min(limit or settings.kb_top_k, 10))
    tokens = tokenize(query)

    db.init_schema()
    conn = db.connect()
    try:
        rows: list[sqlite3.Row] = []

        if tokens:
            # 第一轮：AND，要求所有关键词都出现。准确率高，适合大多数问题。
            # 候选集要取得比最终结果大很多：同一篇文章有很多块，
            # 如果只取 limit*5，标题完全匹配的文章可能排在第 16 个块，
            # 根本进不了后面的“按文章去重 + 标题加成”阶段。
            # 知识库只有几百个块，一次多取几十行成本可以忽略。
            rows = _run_fts(conn, build_match_query(tokens, "AND"), max(limit * 20, 60))

            # 第二轮：如果 AND 太严格，退一步用 OR 提高召回，
            # 但要求命中足够多的关键词，过滤掉“只碰巧命中一个词”的结果。
            if not rows and len(tokens) > 1:
                candidates = _run_fts(conn, build_match_query(tokens, "OR"), limit * 10)
                required = max(2, math.ceil(len(tokens) * 0.6))
                rows = [
                    row
                    for row in candidates
                    if _matched_token_count(f"{row['title']} {row['body']}", tokens)
                    >= required
                ]

            # 只有一个关键词时，AND/OR 等价，直接再查一次即可。
            if not rows and len(tokens) == 1:
                rows = _run_fts(conn, build_match_query(tokens, "OR"), limit * 5)

        # 兜底：分词后没有有效词，或 FTS 完全没命中时，用 LIKE 做一次
        # 连续子串匹配。LIKE 会全表扫描，但知识库通常只有几百块，影响可接受。
        if not rows:
            like = f"%{query}%"
            rows = list(
                conn.execute(
                    _SELECT
                    + " WHERE title LIKE ? OR body LIKE ? ORDER BY post_id LIMIT ?",
                    (like, like, limit * 5),
                )
            )

        return _dedupe_by_post(rows, tokens)[:limit]
    finally:
        conn.close()


def format_results(results: list[dict[str, Any]]) -> str:
    """把检索结果整理成给模型看的文本。

    这段文字会放进 ToolMessage。模型看到标题、URL 和摘要后，
    可以自然地组织回答；系统还会另外把结构化结果通过 citations 事件
    发给前端，所以模型不需要自己输出链接。
    """

    if not results:
        return "站内没有检索到相关文章。"

    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        kind_label = "图片" if item.get("kind") == "image" else "文章"
        lines.append(
            f"[{index}] {item['title']}（{kind_label}）\n"
            f"URL: {item['url']}\n"
            f"摘要: {item['excerpt']}"
        )
    return "\n\n".join(lines)


def stats() -> dict[str, int]:
    """知识库统计，供命令行/健康检查使用。"""

    return db.stats()
