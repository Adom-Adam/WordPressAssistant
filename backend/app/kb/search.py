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
from app.embeddings import get_embedder
from app.kb import db, vectors
from app.kb.text import build_match_query, make_excerpt, tokenize

# bm25() 的权重按 FTS5 表的列顺序传入：
# post_id, chunk_index, kind, image_url, url, title, body, tokens
# 前五列是 UNINDEXED，没有词频，权重填 0 即可。
# title 权重最高（8.0），tokens 次之（3.0），body 最低（1.0）。
_BM25 = "bm25(chunks, 0, 0, 0, 0, 0, 8.0, 1.0, 3.0)"

_SELECT = f"""
    SELECT
        rowid,
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

# 这些词单独出现时，用户通常是在问“最近有什么”，而不是检索某个词。
# 例如“最近”可能碰巧出现在某篇正文里，但用户想看的其实是时间顺序的目录。
_RECENT_WORDS = {"最近", "最新", "更新", "新东西", "东西"}


def _rowid_of(row: Any) -> int:
    """取出块的主键；sqlite3.Row 缺列抛 IndexError，dict 缺键抛 KeyError。"""
    for key in ("rowid", "_rowid"):
        try:
            return int(row[key])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return 0


def _row_to_result(row: sqlite3.Row, tokens: list[str]) -> dict[str, Any]:
    """把一行 SQLite 结果转成前端/工具使用的字典。"""

    body = str(row["body"] or "")
    kind = str(row["kind"] or "text")
    try:
        rank = float(row["rank"])
    except (IndexError, KeyError, TypeError, ValueError):
        # 向量召回的 dict 可能没有 bm25 分数；后续会用 RRF 分数覆盖。
        rank = 0.0
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
        "score": round(-rank, 6),
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


def _dedupe_by_post(
    rows: list[Any],
    tokens: list[str],
    *,
    base_scores: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """同一篇文章可能命中多个块，只保留最相关的一条。

    如果这篇文章的文字块先被保留，但后面又出现了图片块，
    就把图片地址补到结果上，让来源卡片能显示缩略图。
    最后按“基础分数 + 标题命中加成”重新排序，
    让标题就包含关键词的文章排在最前面。

    `base_scores` 存在时使用 RRF 分数（块 rowid → 分数）；不存在时
    保持原来的“-BM25”逻辑，所以纯 FTS 路径的行为不变。
    """

    results: dict[int, dict[str, Any]] = {}
    rowids: dict[int, int] = {}
    order: list[int] = []

    for row in rows:
        post_id = int(row["post_id"])
        if post_id not in results:
            results[post_id] = _row_to_result(row, tokens)
            rowids[post_id] = _rowid_of(row)
            order.append(post_id)
            continue

        # 已经有文字结果时，附加一张命中的图片作为缩略图。
        if row["kind"] == "image" and not results[post_id].get("image_url"):
            results[post_id]["image_url"] = row["image_url"]

    deduped = [results[post_id] for post_id in order]
    for item in deduped:
        post_id = int(item["post_id"])
        if base_scores is None:
            base = float(item["score"])
        else:
            base = base_scores.get(rowids.get(post_id, 0), 0.0)
        item["score"] = round(base + _title_bonus(item["title"], tokens), 6)

    # score 越大越相关；分数相同时保持原来的召回顺序（sorted 是稳定排序）。
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


def list_recent(limit: int = 4) -> list[dict[str, Any]]:
    """按最后修改时间列出最近文章，供“这里都有什么”这类宽泛问题使用。

    为什么需要它：
    search_site 依赖关键词，但用户只问“这里有什么”“最近写了什么”时，
    问题里没有可检索的具体词。以前会退化成整句 LIKE，几乎查不到东西；
    现在改为按时间列几篇真实文章，让模型基于标题和正文开头概括，
    而不是凭印象编一个“文章、图片、笔记”的分类。

    `posts.modified` 来自 WordPress；如果旧数据没有这个字段，
    就退回同步时间 `synced_at`，保证顺序仍然稳定。
    """

    settings = get_settings()
    limit = max(1, min(limit or settings.kb_top_k, 10))

    db.init_schema()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT
                p.post_id,
                p.title,
                p.url,
                (
                    SELECT c.body
                    FROM chunks AS c
                    WHERE c.post_id = p.post_id AND c.kind = 'text'
                    ORDER BY c.chunk_index
                    LIMIT 1
                ) AS body
            FROM posts AS p
            ORDER BY COALESCE(p.modified, p.synced_at) DESC, p.post_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            body = str(row["body"] or "")
            results.append(
                {
                    "post_id": int(row["post_id"]),
                    "title": str(row["title"] or ""),
                    "url": str(row["url"] or ""),
                    # 没有关键词时，make_excerpt 会取正文开头，正好适合做目录摘要。
                    "excerpt": make_excerpt(body, [], width=160),
                    "kind": "article",
                    "image_url": None,
                    "score": 0.0,
                }
            )
        return results
    finally:
        conn.close()


def _fts_candidates(
    conn: sqlite3.Connection,
    tokens: list[str],
    query: str,
    limit: int,
) -> list[sqlite3.Row]:
    """纯 FTS 召回：AND → OR 过滤 → 单词重查 → LIKE 兜底。"""
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
                _SELECT + " WHERE title LIKE ? OR body LIKE ? ORDER BY post_id LIMIT ?",
                (like, like, limit * 5),
            )
        )
    return rows


def _vector_candidates(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    """向量召回：返回普通 dict，按向量距离从近到远排列。"""
    encoder = get_embedder()
    if not getattr(encoder, "available", False):
        return []

    encoded = encoder.encode([query], query=True)
    if not encoded:
        return []

    ids = [rowid for rowid, _distance in vectors.search(conn, encoded[0], limit)]
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT rowid, post_id, chunk_index, kind, image_url, url, title, body, "
        f"0.0 AS rank FROM chunks WHERE rowid IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {int(row["rowid"]): dict(row) for row in rows}
    return [by_id[rowid] for rowid in ids if rowid in by_id]


def _rrf_merge(
    fts_rows: list[Any],
    vec_rows: list[Any],
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion：两条召回路径按名次累加，不比较原始分数。

    BM25 分值和余弦距离量纲不同，直接相加没有意义；RRF 只看“排第几”，
    所以不需要归一化。同时被两路命中的块会得到两份分数，自然排前面。
    """
    scores: dict[int, float] = {}
    rows_by_id: dict[int, dict[str, Any]] = {}

    for rank, row in enumerate(fts_rows):
        rowid = _rowid_of(row)
        if not rowid:
            continue
        rows_by_id.setdefault(rowid, dict(row))
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k + rank + 1)

    for rank, row in enumerate(vec_rows):
        rowid = _rowid_of(row)
        if not rowid:
            continue
        rows_by_id.setdefault(rowid, dict(row))
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k + rank + 1)

    merged: list[dict[str, Any]] = []
    for rowid, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        item = dict(rows_by_id[rowid])
        item["_rrf_score"] = score
        merged.append(item)
    return merged


def search_site(query: str, limit: int | None = None) -> list[dict[str, Any]]:
    """检索站内文章，返回最多 limit 条来源。"""

    query = (query or "").strip()
    settings = get_settings()
    limit = max(1, min(limit or settings.kb_top_k, 10))

    # 问题里没有有效关键词（例如“这里都有什么”）时，直接列最近文章。
    # 这比用整句 LIKE 去碰运气更可靠，也让模型有真实内容可以概括。
    # 至少给四篇：模型经常只传 limit=1，拿一篇当全部会误判“馆里没几篇”。
    tokens = tokenize(query)
    if not tokens:
        return list_recent(max(limit, 4))

    # “最近”“最近有什么新东西”这类问题没有具体主题，
    # 即使“最近”碰巧命中了某篇正文，也应该按时间列目录。
    if "最近" in query and all(token in _RECENT_WORDS for token in tokens):
        return list_recent(max(limit, 4))

    # 带具体主题时（例如“最近 DDS 有什么更新”），时间词不参与检索。
    # 否则“更新”可能碰巧命中另一篇文章，把真正要找的 DDS 挤掉。
    if "最近" in query:
        tokens = [token for token in tokens if token not in _RECENT_WORDS] or tokens

    db.init_schema()
    conn = db.connect()
    try:
        rows = _fts_candidates(conn, tokens, query, limit)

        # 向量召回是 FTS 的补充：关键词完全对不上、但语义相近的内容
        # 只有向量能找到。模型不可用或 sqlite-vec 加载失败时返回空列表，
        # 下面的逻辑自动退化成纯 FTS，行为与引入向量前一致。
        vec_rows: list[dict[str, Any]] = []
        if getattr(settings, "kb_vector_enabled", False) and tokens:
            vec_rows = _vector_candidates(
                conn,
                query,
                getattr(settings, "kb_hybrid_recall", 40),
            )

        if vec_rows:
            merged = _rrf_merge(
                rows,
                vec_rows,
                k=getattr(settings, "kb_rrf_k", 60),
            )
            # 按文章去重时用 RRF 分数排序：BM25 与向量距离量纲不同，
            # 只有融合后的名次分数可以放在一起比较。
            base_scores = {
                _rowid_of(item): float(item["_rrf_score"]) for item in merged
            }
            return _dedupe_by_post(merged, tokens, base_scores=base_scores)[:limit]

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
