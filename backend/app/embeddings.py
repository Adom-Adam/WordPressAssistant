"""本地中文嵌入编码器。

====================================================================
为什么单独一层
====================================================================

检索层只关心“把一批文本变成一批向量”，不关心用的是
sentence-transformers、ONNX 还是别的运行时。这个文件把重依赖
（torch / sentence-transformers）挡在检索代码之外：

- 服务启动时不会 import torch，只有一个轻量对象；
- 第一次真正编码时才加载模型；
- 模型缺失、依赖没装、加载失败时返回空列表，由调用方降级为纯 FTS。

模型文件放在 `backend/model/bge-small-zh-v1.5/`，不提交到仓库。
BGE 中文小模型的推荐查询前缀是固定的，查询侧加、文档侧不加；
两边不一致会让相似度明显变差。
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# app/embeddings.py → parents[1] 就是 backend/。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Encoder(Protocol):
    """检索层依赖的最小接口；测试可以用任意假实现替换。"""

    available: bool
    dim: int

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        ...


class Embedder:
    """基于 sentence-transformers 的 CPU 编码器。

    线程安全说明：模型加载用锁保护；推理本身是只读的，允许多个请求
    同时调用。服务器只有 2 vCPU，所以 torch 线程数固定为 2，
    避免嵌入把 DeepSeek 请求所在的进程拖慢。
    """

    def __init__(self, model_path: str | Path, dim: int = 512) -> None:
        path = Path(model_path).expanduser()
        if not path.is_absolute():
            path = _BACKEND_ROOT / path
        self.model_path = path
        self.dim = dim
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """模型文件看起来可用吗？

        这里只做轻量检查，不 import torch；真正加载失败会在 encode 时
        被捕获并标记 _failed，后续调用直接返回空列表。
        """
        if self._failed:
            return False
        if self._model is not None:
            return True
        return self.model_path.is_dir() and (self.model_path / "config.json").is_file()

    def _load(self):
        with self._lock:
            if self._model is not None:
                return self._model
            if self._failed:
                raise RuntimeError("嵌入模型之前加载失败")

            try:
                import torch
                from sentence_transformers import SentenceTransformer

                torch.set_num_threads(2)
                self._model = SentenceTransformer(
                    str(self.model_path),
                    device="cpu",
                    local_files_only=True,
                )
            except Exception:
                # 标记失败，避免每次检索都重试加载、刷日志。
                self._failed = True
                logger.warning("嵌入模型加载失败，后续检索降级为纯 FTS：%s", self.model_path)
                raise
            return self._model

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """把文本编码成归一化向量；任何失败都返回空列表。"""
        if not texts or not self.available:
            return []

        payload = [QUERY_INSTRUCTION + text for text in texts] if query else list(texts)
        try:
            model = self._load()
            vectors = model.encode(
                payload,
                batch_size=32,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [[float(value) for value in vector] for vector in vectors]
        except Exception:
            self._failed = True
            logger.warning("文本嵌入失败，本次检索降级为纯 FTS", exc_info=True)
            return []


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """全局唯一的编码器。测试改配置后需要 `get_embedder.cache_clear()`。"""
    settings = get_settings()
    return Embedder(settings.embedding_model_path, dim=settings.embedding_dim)
