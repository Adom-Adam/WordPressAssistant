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
