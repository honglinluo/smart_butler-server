"""
【模块说明】记忆系统后端工厂

根据环境变量 MEMORY_BACKEND 选择并实例化后端：

  filesystem（默认）— OpenViking 风格，纯文件系统存储，无需 Redis/MySQL/ES
  vectordb          — 现有实现（Redis + MySQL + Elasticsearch），代码位于 app/memory/backends/vectordb/

用法（main.py）：
    from app.memory.factory import create_memory_backend, create_rag_pipeline, MEMORY_BACKEND

    memory_manager = create_memory_backend(system_config)
    rag_pipeline   = create_rag_pipeline(memory_manager, system_config,
                                         embedding_service, vector_store)
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 从环境变量读取，默认 filesystem
MEMORY_BACKEND: str = os.getenv("MEMORY_BACKEND", "filesystem").lower().strip()

_VALID_BACKENDS = ("filesystem", "vectordb")


def create_memory_backend(config: Dict[str, Any]):
    """根据 MEMORY_BACKEND 创建对应的记忆存储后端。

    filesystem → FilesystemMemoryBackend（OpenViking 风格，文件系统）
    vectordb   → MemoryManager（Redis + MySQL + ES，代码位于 app/memory/backends/vectordb/）
    """
    if MEMORY_BACKEND not in _VALID_BACKENDS:
        raise ValueError(
            f"未知的 MEMORY_BACKEND={MEMORY_BACKEND!r}，"
            f"可选值：{_VALID_BACKENDS}"
        )

    if MEMORY_BACKEND == "filesystem":
        from app.memory.backends.filesystem.backend import FilesystemMemoryBackend
        logger.info(
            "🗂  记忆后端：FilesystemMemoryBackend（OpenViking 文件系统）"
        )
        return FilesystemMemoryBackend(config)

    # vectordb — 从 app.memory.backends.vectordb 导入（规范路径）
    from app.memory.backends.vectordb.memory_manager import MemoryManager
    logger.info("🗄  记忆后端：VectorDB MemoryManager（Redis + MySQL + ES）")
    return MemoryManager(config)


def create_rag_pipeline(
    memory_backend,
    config: Dict[str, Any],
    embedding_service=None,
    vector_store=None,
):
    """根据 MEMORY_BACKEND 创建对应的 RAG 流水线。

    filesystem → FilesystemRagPipeline（关键词检索，无外部依赖）
    vectordb   → 现有 RagPipeline（ES + 向量检索，代码保留不变）
    """
    if MEMORY_BACKEND == "filesystem":
        from app.memory.backends.filesystem.rag import FilesystemRagPipeline
        logger.info(
            "🔍 RAG 流水线：FilesystemRagPipeline（关键词检索）"
        )
        return FilesystemRagPipeline(memory_backend, config)

    # vectordb — 使用现有 RagPipeline
    from app.rag import RagPipeline
    logger.info("🔍 RAG 流水线：VectorDB RagPipeline（ES + 向量检索）")
    return RagPipeline(embedding_service, vector_store, memory_backend, config)


def is_filesystem_backend() -> bool:
    """快捷判断当前是否使用 filesystem 后端。"""
    return MEMORY_BACKEND == "filesystem"
