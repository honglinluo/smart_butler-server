"""
【模块说明】VectorDB 后端适配层

说明：VectorDB 后端的实现代码位于同包的 memory_manager.py：
  - 记忆存储：app/memory/backends/vectordb/memory_manager.py::MemoryManager
  - RAG 流水线：app/rag/pipeline.py::RagPipeline

本文件仅做统一入口导出，便于通过 backends.vectordb 命名空间访问。
后续如需对 VectorDB 后端进行重构或迁移，在此替换导入即可，
调用方（factory.py）无需修改。
"""

from app.memory.backends.vectordb.memory_manager import MemoryManager as VectorDBMemoryBackend
from app.rag.pipeline import RagPipeline as VectorDBRagPipeline

__all__ = ["VectorDBMemoryBackend", "VectorDBRagPipeline"]
