"""
记忆系统包 — 支持可插拔后端（filesystem / vectordb）

快速使用：
    from app.memory.factory import create_memory_backend, create_rag_pipeline, MEMORY_BACKEND
"""

from app.memory.base import MemoryBackend, RagBackend
from app.memory.factory import (
    MEMORY_BACKEND,
    create_memory_backend,
    create_rag_pipeline,
    is_filesystem_backend,
)

__all__ = [
    "MemoryBackend",
    "RagBackend",
    "MEMORY_BACKEND",
    "create_memory_backend",
    "create_rag_pipeline",
    "is_filesystem_backend",
]
