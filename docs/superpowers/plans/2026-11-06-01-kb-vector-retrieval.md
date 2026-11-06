# 知识库向量检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给现有知识库加上本地向量检索，与 FTS5 用 RRF 融合；嵌入模型或扩展不可用时静默降级为纯 FTS。

**Architecture:** 新增 `app/embeddings.py`（本地 `bge-small-zh-v1.5` 编码器，惰性加载、CPU、线程数受控）和 `app/kb/vectors.py`（`sqlite-vec` 扩展加载、`chunks_vec` 表、写入/检索/清理）。`kb/db.py` 在每次连接时加载扩展、建表时按配置维度创建向量表；`kb/sync.py` 在写入文本块后批量嵌入；`kb/search.py` 把 FTS 与向量结果做 RRF 融合，返回结构保持不变。

**Tech Stack:** Python 3.11 / SQLite（FTS5 + sqlite-vec）/ sentence-transformers（CPU）/ pytest / uv。

**Spec:** `docs/superpowers/specs/2026-11-06-merlin-memory-relationship-proactive-design.md`

## Global Constraints

- 嵌入模型固定为 `BAAI/bge-small-zh-v1.5`，512 维，本地目录 `backend/model/bge-small-zh-v1.5/`；模型文件不提交（`.gitignore` 已忽略 `backend/model/` 与 `backend/models/`）。
- 查询侧前缀固定为 `为这个句子生成表示以用于检索相关文章：`；文档侧不加前缀；向量必须 L2 归一化。
- 向量检索是增强，不是前提：模型目录缺失、依赖未装、扩展加载失败、编码返回空时，全部回退到现有 FTS 行为，聊天不能因此不可用。
- 现有 `search_site()` 的返回字段、排序兜底（无关键词列最近文章、“最近”词处理）保持不变；向量不可用时输出必须与改动前一致。
- `chunks_vec` 的维度由 `Settings.embedding_dim`（默认 512）决定；维度变化时重建向量表并清空旧向量，不能混用两种维度。
- 每个数据库连接都要加载 `sqlite-vec`；加载失败只记一次警告，不影响 FTS。
- 重同步单篇文章时，先删除该文章旧向量，再删旧文本块，避免留下指向不存在 `rowid` 的孤儿向量。
- 嵌入在写入事务之外执行，不能长时间持有 SQLite 写锁；按 32 条一批。
- 生产服务器 2 vCPU / 4 GiB：加载模型时把 torch 线程数限制为 2。
- 不新增除 `sqlite-vec` 和 `sentence-transformers` 之外的依赖。
- 只做本地提交，不推送、不部署；服务器端代码修改需站长另行同意。
- 测试命令统一在 `backend/` 下执行：`uv run pytest`。

## Review Focus

1. **模型目录缺失或损坏**：服务必须照常启动、纯 FTS 检索可用；不能因为 `SentenceTransformer(...)` 抛异常就让聊天 500。
2. **`sqlite-vec` 扩展不可用**：`init_schema` 不能留下半张表；检索走 FTS；日志只提醒一次，不刷屏。
3. **维度变化/旧向量**：把 `embedding_dim` 从 512 改成别的值后，旧向量不能被静默当成新维度查询；必须重建并清空。
4. **文章重同步或删除**：旧 `chunks_vec` 行必须跟着旧 `chunks` 一起删除；否则 `rowid` 会被新文章复用，返回完全错误的文章。
5. **批量嵌入长度不一致/空向量/NaN**：任何一条失败只跳过该条并记日志，不能让整次同步或整次检索崩掉；写入前校验维度与有限值。
6. **现有测试的简化 settings**：`test_kb_search.py` 用 `SimpleNamespace(kb_db_path, kb_top_k)`，新代码必须用 `getattr(..., 默认值)` 容忍缺字段，不能要求测试对象补全所有配置。

---

### Task 1: 嵌入编码器与配置

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Create: `backend/app/embeddings.py`
- Test: `backend/tests/test_embeddings.py`

**Interfaces:**
- Consumes: `app.config.get_settings()`。
- Produces:
  - `QUERY_INSTRUCTION: str`（值固定为 `为这个句子生成表示以用于检索相关文章：`）
  - `class Encoder(Protocol): available: bool; dim: int; encode(texts: list[str], *, query: bool = False) -> list[list[float]]`
  - `class Embedder: __init__(model_path: str | Path, dim: int = 512)`，实现 `Encoder`
  - `get_embedder() -> Embedder`（`lru_cache(maxsize=1)`；测试可 `get_embedder.cache_clear()`）
  - `Settings.embedding_model_path: str = "model/bge-small-zh-v1.5"`
  - `Settings.embedding_dim: int = 512`
  - `Settings.kb_vector_enabled: bool = True`
  - `Settings.kb_hybrid_recall: int = 40`
  - `Settings.kb_rrf_k: int = 60`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_embeddings.py
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.embeddings import QUERY_INSTRUCTION, Embedder


def test_query_instruction_is_fixed() -> None:
    assert QUERY_INSTRUCTION == "为这个句子生成表示以用于检索相关文章："


def test_missing_model_is_unavailable_and_returns_empty(tmp_path: Path) -> None:
    embedder = Embedder(tmp_path / "missing-model", dim=8)
    assert embedder.available is False
    assert embedder.encode(["你好"], query=True) == []


def test_relative_model_path_is_resolved_to_absolute() -> None:
    embedder = Embedder("model/bge-small-zh-v1.5", dim=512)
    assert embedder.model_path.is_absolute()
    assert embedder.model_path.name == "bge-small-zh-v1.5"


@pytest.mark.skipif(
    os.environ.get("RUN_EMBEDDING_TESTS") != "1",
    reason="真实模型推理较慢，设置 RUN_EMBEDDING_TESTS=1 才运行",
)
def test_real_model_encodes_normalized_512_dim_vectors() -> None:
    embedder = Embedder("model/bge-small-zh-v1.5", dim=512)
    assert embedder.available is True
    vectors = embedder.encode(["分布式通信中间件", "今天天气很好"], query=False)
    assert len(vectors) == 2
    assert all(len(vector) == 512 for vector in vectors)
    assert all(abs(sum(value * value for value in vector) - 1.0) < 1e-3 for vector in vectors)
```

- [ ] **Step 2: 安装依赖并运行测试，确认失败**

```bash
cd backend
uv add "sqlite-vec>=0.1.6" "sentence-transformers>=3.0"
uv run pytest tests/test_embeddings.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'app.embeddings'`。

- [ ] **Step 3: 在 `app/config.py` 增加四项配置**

```python
# 放在“站内知识库”配置段，kb_chunk_overlap 之后
embedding_model_path: str = "model/bge-small-zh-v1.5"
embedding_dim: int = 512
kb_vector_enabled: bool = True
kb_hybrid_recall: int = 40
kb_rrf_k: int = 60
```

- [ ] **Step 4: 实现 `app/embeddings.py`**

要求：

- 用 `typing.Protocol` 定义 `Encoder`（三个成员：`available`、`dim`、`encode`），供 `sync` / `search` 的类型标注和测试替身使用。
- `_BACKEND_ROOT = Path(__file__).resolve().parents[1]`；相对 `model_path` 拼到该目录下。
- `available`：`_failed` 为真返回 `False`；模型已加载返回 `True`；否则检查目录存在且 `config.json` 存在。**不在这里 import torch**，避免测试/启动时加载重依赖。
- `_load()`：`threading.Lock` 保护；`import torch`；`torch.set_num_threads(2)`；`SentenceTransformer(str(self.model_path), device="cpu", local_files_only=True)`；任何异常设置 `_failed=True` 并抛出。
- `encode()`：空输入返回 `[]`；`not available` 返回 `[]`；`query=True` 时给每条加 `QUERY_INSTRUCTION` 前缀；`model.encode(..., batch_size=32, normalize_embeddings=True, show_progress_bar=False)`；捕获加载/编码异常，设置 `_failed=True`，记 `logger.warning`，返回 `[]`。返回值转成 `list[list[float]]`。
- `get_embedder()` 读取 `get_settings().embedding_model_path` 与 `embedding_dim`。

- [ ] **Step 5: 更新 `backend/.env.example`**

```env
# ---- 本地向量检索（可选增强）----
# 模型目录相对 backend/；不存在时自动降级为纯 FTS 检索。
EMBEDDING_MODEL_PATH=model/bge-small-zh-v1.5
EMBEDDING_DIM=512
KB_VECTOR_ENABLED=true
# 向量召回条数（与 FTS 结果做 RRF 融合）
KB_HYBRID_RECALL=40
# RRF 平滑常数，越大越弱化单路排名差异
KB_RRF_K=60
```

- [ ] **Step 6: 运行测试并确认通过**

```bash
cd backend
uv run pytest tests/test_embeddings.py -v
```

Expected: 3 passed, 1 skipped（未设置 `RUN_EMBEDDING_TESTS` 时）。

- [ ] **Step 7: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/config.py backend/app/embeddings.py backend/tests/test_embeddings.py backend/.env.example
git commit -m "feat(kb): 本地中文嵌入编码器与向量检索配置"
```

---

### Task 2: `sqlite-vec` 存储层

**Files:**
- Create: `backend/app/kb/vectors.py`
- Modify: `backend/app/kb/db.py`
- Test: `backend/tests/test_kb_vectors.py`

**Interfaces:**
- Consumes: `sqlite_vec`（可选导入）、`Settings.embedding_dim`。
- Produces:
  - `load_extension(conn: sqlite3.Connection) -> bool`
  - `ensure_schema(conn: sqlite3.Connection, dim: int) -> bool`
  - `upsert(conn: sqlite3.Connection, rows: list[tuple[int, list[float]]]) -> int`
  - `search(conn: sqlite3.Connection, embedding: list[float], limit: int) -> list[tuple[int, float]]`（按距离升序）
  - `delete_post(conn: sqlite3.Connection, post_id: int) -> None`
  - `clear(conn: sqlite3.Connection) -> None`
  - `stats(conn: sqlite3.Connection) -> dict[str, int]`，键为 `vectors`
  - 修改后的 `db.connect()` / `db.init_schema()` / `db.clear_index()` / `db.stats()`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kb_vectors.py
from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, vectors


def _settings(tmp_path, dim=4):
    return SimpleNamespace(kb_db_path=str(tmp_path / "kb.sqlite3"), embedding_dim=dim)


def _insert_chunks(conn, post_id: int, bodies: list[str]) -> list[int]:
    rowids = []
    for index, body in enumerate(bodies):
        cursor = conn.execute(
            "INSERT INTO chunks (post_id, chunk_index, kind, image_url, url, title, body, tokens) "
            "VALUES (?, ?, 'text', NULL, ?, ?, ?, ?)",
            (post_id, index, f"https://example.com/{post_id}/", f"文章{post_id}", body, body),
        )
        rowids.append(int(cursor.lastrowid))
    conn.commit()
    return rowids


def test_vectors_are_stored_and_ranked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path))
    db.init_schema()
    conn = db.connect()
    try:
        rowids = _insert_chunks(conn, 1, ["第一块", "第二块", "第三块"])
        rows = [
            (rowids[0], [1.0, 0.0, 0.0, 0.0]),
            (rowids[1], [0.0, 1.0, 0.0, 0.0]),
            (rowids[2], [0.0, 0.0, 1.0, 0.0]),
        ]
        assert vectors.upsert(conn, rows) == 3
        found = vectors.search(conn, [0.1, 0.9, 0.0, 0.0], limit=2)
        assert [rowid for rowid, _distance in found] == [rowids[1], rowids[0]]
    finally:
        conn.close()


def test_delete_post_removes_its_vectors(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path))
    db.init_schema()
    conn = db.connect()
    try:
        rowids = _insert_chunks(conn, 1, ["第一块", "第二块"])
        vectors.upsert(conn, [(rowid, [1.0, 0.0, 0.0, 0.0]) for rowid in rowids])
        vectors.delete_post(conn, 1)
        assert vectors.stats(conn)["vectors"] == 0
    finally:
        conn.close()


def test_clear_index_removes_chunks_and_vectors(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path))
    db.init_schema()
    conn = db.connect()
    try:
        rowids = _insert_chunks(conn, 1, ["第一块"])
        vectors.upsert(conn, [(rowids[0], [1.0, 0.0, 0.0, 0.0])])
        db.clear_index(conn)
        assert db.stats(conn)["chunks"] == 0
        assert vectors.stats(conn)["vectors"] == 0
    finally:
        conn.close()


def test_dimension_change_rebuilds_vector_table(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path, dim=4))
    db.init_schema()
    conn = db.connect()
    rowids = _insert_chunks(conn, 1, ["第一块"])
    vectors.upsert(conn, [(rowids[0], [1.0, 0.0, 0.0, 0.0])])
    conn.close()

    monkeypatch.setattr(db, "get_settings", lambda: _settings(tmp_path, dim=8))
    db.init_schema()
    conn = db.connect()
    try:
        assert vectors.stats(conn)["vectors"] == 0
        vectors.upsert(conn, [(rowids[0], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])])
        assert vectors.search(conn, [1.0] + [0.0] * 7, limit=1)[0][0] == rowids[0]
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd backend
uv run pytest tests/test_kb_vectors.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'app.kb.vectors'`。

- [ ] **Step 3: 实现 `app/kb/vectors.py`**

要点：

- `try: import sqlite_vec except ImportError: sqlite_vec = None`；`_serialize = sqlite_vec.serialize_float32`。
- `load_extension`：`sqlite_vec is None` 返回 `False`；否则 `conn.enable_load_extension(True)`、`sqlite_vec.load(conn)`、`conn.enable_load_extension(False)`；捕获 `AttributeError/sqlite3.Error/OSError`，`logger.warning` 一次并返回 `False`。
- `ensure_schema`：先 `load_extension`，失败返回 `False`；读 `meta` 的 `vec_dim`，与 `dim` 不同则 `DROP TABLE IF EXISTS chunks_vec`；执行 `CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{dim}])`；写回 `vec_dim`；`conn.commit()`；返回 `True`。
- `upsert`：逐条校验 `len(vector) == 当前维度`（从 `meta` 读）且全部为有限值；非法项记 warning 跳过；用 `INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)`；返回成功条数。写入前如扩展未加载，返回 0。
- `search`：扩展未加载、`limit <= 0`、向量维度不符或含非有限值时返回 `[]`；SQL 为 `SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance`。
- `delete_post`：`DELETE FROM chunks_vec WHERE rowid IN (SELECT rowid FROM chunks WHERE post_id = ?)`；捕获 `sqlite3.Error` 并返回。
- `clear`：`DELETE FROM chunks_vec`。
- `stats`：表不存在返回 `{"vectors": 0}`，否则返回真实计数。

- [ ] **Step 4: 修改 `app/kb/db.py`**

- 顶部 `from app.kb import vectors`。
- `connect()` 在 `PRAGMA` 之后调用 `vectors.load_extension(conn)`（忽略返回值）。
- `init_schema()` 在 `executescript(_SCHEMA)` 之后调用 `vectors.ensure_schema(conn, getattr(settings, "embedding_dim", 512))`；`settings` 从 `get_settings()` 取。
- `clear_index(conn)`：先 `vectors.clear(conn)`，再删 `chunks` / `posts`。
- `stats()`：增加 `"vectors": vectors.stats(conn)["vectors"]`，并保持原有键不变。

- [ ] **Step 5: 运行向量测试与现有知识库测试**

```bash
cd backend
uv run pytest tests/test_kb_vectors.py tests/test_kb_search.py -v
```

Expected: 全部 PASS；`test_kb_search.py` 不因缺少 `embedding_dim` 字段报错。

- [ ] **Step 6: 提交**

```bash
git add backend/app/kb/vectors.py backend/app/kb/db.py backend/tests/test_kb_vectors.py
git commit -m "feat(kb): sqlite-vec 向量表与连接级扩展加载"
```

---

### Task 3: 同步时批量嵌入

**Files:**
- Modify: `backend/app/kb/sync.py`
- Test: `backend/tests/test_kb_sync_vectors.py`

**Interfaces:**
- Consumes: `Embedder` / `get_embedder()`（Task 1）；`vectors.upsert`、`vectors.delete_post`（Task 2）。
- Produces:
  - `sync(..., embedder: Encoder | None = None, with_vectors: bool = True) -> dict[str, int]`
  - `build_vectors(embedder: Encoder | None = None, batch_size: int = 32) -> int`
  - 统计结果新增 `"vectors"` 键（来自 `db.stats`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kb_sync_vectors.py
from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, sync, vectors


class FakeEmbedder:
    available = True
    dim = 4

    def encode(self, texts, *, query=False):
        return [[1.0, 0.0, 0.0, 0.0] if "DDS" in text else [0.0, 1.0, 0.0, 0.0] for text in texts]


class UnavailableEmbedder:
    available = False
    dim = 4

    def encode(self, texts, *, query=False):
        return []


def _settings(tmp_path):
    return SimpleNamespace(
        wp_site_url="https://example.com",
        kb_db_path=str(tmp_path / "kb.sqlite3"),
        kb_chunk_size=600,
        kb_chunk_overlap=80,
        embedding_dim=4,
        kb_vector_enabled=True,
    )


def _post():
    return {
        "id": 1,
        "link": "https://example.com/1/",
        "title": {"rendered": "DDS 入门"},
        "content": {"rendered": "<p>DDS 是一种分布式通信中间件。</p>"},
        "modified_gmt": "2026-11-01T00:00:00",
    }


def _patch(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(sync, "fetch_posts", lambda *args, **kwargs: [_post()])


def test_sync_writes_one_vector_per_chunk(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    stats = sync.sync(with_vision=False, embedder=FakeEmbedder())
    assert stats["chunks"] > 0
    assert stats["vectors"] == stats["chunks"]


def test_sync_without_vectors_keeps_fts_only(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    stats = sync.sync(with_vision=False, embedder=FakeEmbedder(), with_vectors=False)
    assert stats["chunks"] > 0
    assert stats["vectors"] == 0


def test_unavailable_embedder_does_not_break_sync(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    stats = sync.sync(with_vision=False, embedder=UnavailableEmbedder())
    assert stats["chunks"] > 0
    assert stats["vectors"] == 0


def test_resync_does_not_leave_stale_vectors(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    sync.sync(with_vision=False, embedder=FakeEmbedder())
    sync.sync(with_vision=False, embedder=FakeEmbedder())
    conn = db.connect()
    try:
        assert db.stats(conn)["chunks"] == vectors.stats(conn)["vectors"]
    finally:
        conn.close()


def test_build_vectors_backfills_all_chunks(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    sync.sync(with_vision=False, embedder=FakeEmbedder(), with_vectors=False)
    assert sync.build_vectors(embedder=FakeEmbedder()) > 0
    conn = db.connect()
    try:
        assert db.stats(conn)["vectors"] == db.stats(conn)["chunks"]
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd backend
uv run pytest tests/test_kb_sync_vectors.py -v
```

Expected: FAIL，`TypeError: sync() got an unexpected keyword argument 'embedder'`。

- [ ] **Step 3: 修改 `sync()`**

- 签名增加关键字参数 `embedder=None`、`with_vectors=True`。
- `encoder = None`；若 `with_vectors` 为真，`encoder = embedder or get_embedder()`；只有 `encoder is not None and encoder.available` 才启用。
- 每篇文章删除旧块之前，先 `vectors.delete_post(conn, post_id)`。
- 把 `executemany` 改成逐条 `conn.execute(...)`，再用 `SELECT rowid FROM chunks WHERE post_id = ? ORDER BY rowid` 取回 `rowids`；确认 `len(rowids) == len(rows)`，否则跳过本篇文章的向量写入并记 warning。
- 事务提交后、进入下一篇之前，执行嵌入：

```python
texts = [str(row[6]) for row in rows]
encoded = encoder.encode(texts) if encoder is not None else []
if len(encoded) == len(rowids):
    with conn:
        vectors.upsert(conn, list(zip(rowids, encoded)))
else:
    logger.warning("向量数量与文本块不一致，跳过本篇向量：post_id=%s", post_id)
```

- 打印行增加向量数量：`正文 X 块，图片 Y 张，向量 Z 条`。
- `db.stats` 已经包含 `vectors`，命令行输出增加一行 `向量：{statistics['vectors']} 条`。

- [ ] **Step 4: 增加 `build_vectors()`**

```python
def build_vectors(embedder=None, batch_size: int = 32) -> int:
    """只重建向量：读取现有 chunks，分批嵌入后写入 chunks_vec。"""
```

要求：`db.init_schema()`；取 `embedder or get_embedder()`，`available` 为假时打印提示并返回 0；读取全部 `(rowid, body)`；`vectors.clear(conn)` 后按批 `encoder.encode(texts)`，长度一致才 `vectors.upsert`；返回写入总数。嵌入在事务外，写入用短事务。

- [ ] **Step 5: 更新命令行入口**

`argparse` 增加：

```python
parser.add_argument("--no-vectors", action="store_true", help="不生成向量，只建 FTS 索引")
parser.add_argument("--vectors-only", action="store_true", help="不拉取文章，只为现有文本块补向量")
```

`main()`：`--vectors-only` 时调用 `build_vectors()` 并打印条数后返回；否则 `sync(..., with_vectors=not args.no_vectors)`。

- [ ] **Step 6: 运行测试**

```bash
cd backend
uv run pytest tests/test_kb_sync_vectors.py tests/test_kb_vectors.py tests/test_kb_search.py -v
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/kb/sync.py backend/tests/test_kb_sync_vectors.py
git commit -m "feat(kb): 同步文章时批量生成并写入向量"
```

---

### Task 4: FTS + 向量 RRF 混合检索

**Files:**
- Modify: `backend/app/kb/search.py`
- Test: `backend/tests/test_kb_hybrid_search.py`

**Interfaces:**
- Consumes: `get_embedder()`；`vectors.search()`；`Settings.kb_vector_enabled`、`Settings.kb_hybrid_recall`。
- Produces:
  - 内部函数 `_fts_candidates(conn, tokens, query, limit) -> list[sqlite3.Row]`（把现有召回逻辑抽出，行为不变）
  - 内部函数 `_vector_candidates(conn, query, limit) -> list[sqlite3.Row]`
  - 内部函数 `_rrf_merge(fts_rows, vec_rows, k=60) -> list[sqlite3.Row]`（按 RRF 分数降序）
  - `search_site(query, limit=None)` 行为增强，返回结构不变

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kb_hybrid_search.py
from __future__ import annotations

from types import SimpleNamespace

from app.kb import db, search, vectors


class FakeEmbedder:
    available = True
    dim = 4

    def encode(self, texts, *, query=False):
        # 含“DDS/分布式/中间件”的文本靠拢第一个基向量，其余靠拢第二个。
        return [
            [1.0, 0.0, 0.0, 0.0]
            if any(word in text for word in ("DDS", "分布式", "中间件"))
            else [0.0, 1.0, 0.0, 0.0]
            for text in texts
        ]


class UnavailableEmbedder:
    available = False
    dim = 4

    def encode(self, texts, *, query=False):
        return []


def _settings(tmp_path):
    return SimpleNamespace(
        kb_db_path=str(tmp_path / "kb.sqlite3"),
        kb_top_k=4,
        embedding_dim=4,
        kb_vector_enabled=True,
        kb_hybrid_recall=10,
    )


def _seed(tmp_path, monkeypatch, rows):
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(search, "get_settings", lambda: settings)
    db.init_schema()
    conn = db.connect()
    ids = []
    for post_id, title, body in rows:
        cursor = conn.execute(
            "INSERT INTO chunks (post_id, chunk_index, kind, image_url, url, title, body, tokens) "
            "VALUES (?, 0, 'text', NULL, ?, ?, ?, ?)",
            (post_id, f"https://example.com/{post_id}/", title, body, body),
        )
        ids.append(int(cursor.lastrowid))
    conn.commit()
    conn.close()
    return ids


def test_vector_recall_finds_chunk_without_keyword_overlap(tmp_path, monkeypatch) -> None:
    _seed(
        tmp_path,
        monkeypatch,
        [
            (1, "通信笔记", "DDS 是一种分布式通信中间件。"),
            (2, "天气", "今天天气很好。"),
        ],
    )
    monkeypatch.setattr(search, "get_embedder", lambda: FakeEmbedder())

    results = search.search_site("分布式中间件", limit=1)

    assert results[0]["title"] == "通信笔记"


def test_chunk_hit_by_both_lists_ranks_first(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(search, "get_settings", lambda: settings)
    db.init_schema()
    conn = db.connect()
    try:
        rows = []
        for post_id, title, body in [
            (1, "纯向量命中", "一段完全不含查询词的内容。"),
            (2, "双路命中", "DDS 分布式通信。"),
        ]:
            cursor = conn.execute(
                "INSERT INTO chunks (post_id, chunk_index, kind, image_url, url, title, body, tokens) "
                "VALUES (?, 0, 'text', NULL, ?, ?, ?, ?)",
                (post_id, f"https://example.com/{post_id}/", title, body, body),
            )
            rows.append(int(cursor.lastrowid))
        conn.commit()
        vectors.upsert(
            conn,
            [
                (rows[0], [1.0, 0.0, 0.0, 0.0]),
                (rows[1], [0.9, 0.1, 0.0, 0.0]),
            ],
        )
    finally:
        conn.close()

    monkeypatch.setattr(search, "get_embedder", lambda: FakeEmbedder())
    results = search.search_site("DDS", limit=2)

    assert results[0]["title"] == "双路命中"


def test_unavailable_embedder_keeps_fts_only_behavior(tmp_path, monkeypatch) -> None:
    _seed(tmp_path, monkeypatch, [(1, "DDS 入门", "DDS 是通信中间件。")])
    monkeypatch.setattr(search, "get_embedder", lambda: UnavailableEmbedder())

    results = search.search_site("DDS", limit=1)

    assert results[0]["title"] == "DDS 入门"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd backend
uv run pytest tests/test_kb_hybrid_search.py -v
```

Expected: 第一个测试 FAIL（找不到“通信笔记”，因为 FTS 对“分布式中间件”不命中）；其余可能通过或失败，先确认第一个失败原因正确。

- [ ] **Step 3: 重构 `search.py`**

- `_SELECT` 增加 `rowid` 列（放在 `post_id` 前），否则 RRF 无法从 FTS 行拿到块主键；`bm25()` 的列权重参数不受影响（它按 FTS5 表列顺序，不按 SELECT 顺序）。
- 把现有 `search_site` 中“AND → OR 过滤 → 单词重查 → LIKE 兜底”整段抽成 `_fts_candidates(conn, tokens, query, limit)`，原样搬移，不改排序和过滤条件。
- 新增 `_vector_candidates(conn, query, limit) -> list[dict]`：`encoder = get_embedder()`；`getattr(encoder, "available", False)` 为假返回 `[]`；`encoded = encoder.encode([query], query=True)`；空或维度不符返回 `[]`；`ids = [rowid for rowid, _ in vectors.search(conn, encoded[0], limit)]`；空返回 `[]`；用 `SELECT rowid, post_id, chunk_index, kind, image_url, url, title, body, 0.0 AS rank FROM chunks WHERE rowid IN (...)` 取行，转成普通 dict，按传入的 `ids` 顺序重排。
- 新增 `_rrf_merge(fts_rows, vec_rows, k=60) -> list[dict]`: 用 `row["rowid"]` 做键，`score += 1.0 / (k + rank + 1)`；返回按分数降序、去重后的普通 dict 列表，每项额外带 `_rrf_score`（不要修改 `sqlite3.Row`，它是只读的）。
- `search_site` 在拿到 `rows = _fts_candidates(...)` 后：

```python
vec_rows: list[sqlite3.Row] = []
if getattr(settings, "kb_vector_enabled", False) and tokens:
    vec_rows = _vector_candidates(conn, query, getattr(settings, "kb_hybrid_recall", 40))

if vec_rows:
    merged = _rrf_merge(rows, vec_rows, k=getattr(settings, "kb_rrf_k", 60))
    return _dedupe_by_post(merged, tokens, base_scores={row["rowid"]: row["_rrf_score"] for row in merged})[:limit]

return _dedupe_by_post(rows, tokens)[:limit]
```

- `_dedupe_by_post` 增加可选参数 `base_scores: dict[int, float] | None = None`；有值时用 `base_scores[rowid] + title_bonus` 作为 `score`，没有值时保持现有 `-bm25 + title_bonus`。`rowid` 从每行的 `_rowid`/`rowid` 字段取，取不到时退回 `0`。
- `_row_to_result` 对 `row["rank"]` 做兼容：用 `try/except (IndexError, KeyError)`，缺失时按 `0.0` 处理（`sqlite3.Row` 缺列抛 `IndexError`，dict 缺键抛 `KeyError`）。
- 不要在 `search_site` 里直接 `await`/线程池：该函数由 LangChain 同步工具在 executor 中调用；保持同步接口。

- [ ] **Step 4: 运行混合检索与现有测试**

```bash
cd backend
uv run pytest tests/test_kb_hybrid_search.py tests/test_kb_search.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/kb/search.py backend/tests/test_kb_hybrid_search.py
git commit -m "feat(kb): FTS 与向量召回 RRF 融合检索"
```

---

### Task 5: 运维入口、统计与本地验证

**Files:**
- Modify: `backend/app/kb/sync.py`（若 Task 3 已加，只补打印与错误提示）
- Modify: `backend/app/kb/db.py`（统计输出键）
- Test: `backend/tests/test_kb_sync_vectors.py`（补 `build_vectors` 的边界用例）

**Interfaces:**
- Consumes: Task 1–4 的全部接口。
- Produces: `python -m app.kb.sync --vectors-only` 可用于“模型后下载、只补向量”；`--no-vectors` 可用于临时关闭。

- [ ] **Step 1: 补失败测试**

```python
def test_build_vectors_with_unavailable_embedder_returns_zero(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    sync.sync(with_vision=False, embedder=FakeEmbedder(), with_vectors=False)
    assert sync.build_vectors(embedder=UnavailableEmbedder()) == 0
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend
uv run pytest tests/test_kb_sync_vectors.py::test_build_vectors_with_unavailable_embedder_returns_zero -v
```

Expected: FAIL（当前实现可能抛异常或返回非 0）。

- [ ] **Step 3: 补齐 `build_vectors` 的不可用分支与提示**

`available` 为假时打印：

```
本地嵌入模型不可用（检查 backend/model/bge-small-zh-v1.5/ 与 sentence-transformers 安装）。已跳过向量生成，FTS 检索不受影响。
```

返回 `0`，不抛异常。

- [ ] **Step 4: 更新文档中的运维命令**

只更新 `backend/.env.example` 的注释与 `docs/tutorial.md` 中知识库同步一节；不新增独立文档。内容：

```bash
# 首次同步（生成 FTS + 向量）
uv run python -m app.kb.sync

# 暂时不生成向量
uv run python -m app.kb.sync --no-vectors

# 模型后下载：只补向量，不重新拉文章
uv run python -m app.kb.sync --vectors-only
```

- [ ] **Step 5: 跑完整后端测试**

```bash
cd backend
uv run pytest
```

Expected: 全部 PASS。若现有测试因 `SimpleNamespace` 缺字段失败，按 Review Focus 第 6 条用 `getattr` 修正生产代码，不改测试的意图。

- [ ] **Step 6: 本地真实模型冒烟（可选但建议）**

```bash
cd backend
RUN_EMBEDDING_TESTS=1 uv run pytest tests/test_embeddings.py -v
```

Expected: `test_real_model_encodes_normalized_512_dim_vectors` PASS；输出 512 维、单位长度。

- [ ] **Step 7: 提交**

```bash
git add backend/app/kb/sync.py backend/app/kb/db.py backend/tests/test_kb_sync_vectors.py backend/.env.example docs/tutorial.md
git commit -m "docs(kb): 向量检索运维命令与降级说明"
```

---

## 验证清单

- [ ] `uv run pytest` 全绿。
- [ ] 删除/改名 `backend/model/bge-small-zh-v1.5/` 后，`uv run pytest tests/test_kb_search.py tests/test_kb_hybrid_search.py` 仍全绿（纯 FTS 降级）。
- [ ] `uv run python -m app.kb.sync --vectors-only` 在模型存在时返回大于 0 的条数；模型不存在时打印提示并返回 0，不抛异常。
- [ ] `git status` 不出现 `backend/model/` 下的任何文件。
- [ ] 检索“分布式中间件”能召回正文只写“DDS”的文章（需要真实模型与已同步知识库）。
