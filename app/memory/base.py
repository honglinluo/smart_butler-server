"""
【模块说明】记忆系统抽象接口

定义两个 ABC：
  - MemoryBackend  — 对话存储与检索
  - RagBackend     — RAG 上下文组装与向量索引

所有后端实现（filesystem / vectordb）均须实现这两个接口，
调用方（hermes_engine、RagPipeline）只依赖这里定义的接口，不感知具体实现。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.rag.types import RagContext


class MemoryBackend(ABC):
    """对话存储后端抽象接口。

    子类必须实现：store_turn、get_recent_turns、retrieve_memory。
    其余方法提供无操作默认实现，后端按需覆盖。
    """

    # hermes_engine 通过 getattr 读取该属性决定是否触发压缩
    context_length_limit: int = 20_000

    # ── 核心存储 ────────────────────────────────────────────────────

    @abstractmethod
    async def store_turn(
        self,
        user_id: str,
        turn_id: str,
        user_input: str,
        assistant_response: str,
        metadata: Optional[Dict[str, Any]] = None,
        agent_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """持久化一轮对话。"""

    @abstractmethod
    async def get_recent_turns(
        self,
        user_id: str,
        client_type: str = "",
    ) -> List[Dict[str, Any]]:
        """返回最近 N 轮对话（时间升序）。

        client_type 非空时仅返回同平台的对话（会话隔离）。
        返回格式与 MemoryManager 保持一致：
          [{"turn_id": ..., "user_input": ..., "assistant_response": ..., ...}]
        """

    @abstractmethod
    async def retrieve_memory(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """检索与 query 最相关的历史对话，最多返回 top_k 条。"""

    # ── 扩展功能（可选覆盖）────────────────────────────────────────

    async def build_system_prompt_block(self, user_id: str) -> str:
        """构造用户画像系统提示块，无画像时返回空字符串。"""
        return ""

    async def on_delegation(
        self,
        user_id: str,
        agent_name: str,
        task: str,
        result: str,
        turn_id: str = "",
    ) -> None:
        """记录 Agent 委派事件（可选）。"""

    async def flush_turns_to_es(self, user_id: str) -> None:
        """将缓存中的对话轮次强制写入持久化存储。

        vectordb 后端：同步 Redis → ES；filesystem 后端：无操作。
        用户最后一个 session 退出登录时调用，确保数据不丢失。
        """

    async def compress_immediately(
        self,
        user_id: str,
        reason: str = "context_overflow",
    ) -> None:
        """触发记忆压缩（可选，filesystem 后端默认无操作）。"""

    def queue_prefetch(self, user_id: str, query: str) -> None:
        """异步预取记忆（同步接口，内部可启动 asyncio Task）。"""

    async def get_prefetched_context(self, user_id: str) -> List[Dict[str, Any]]:
        """读取预取结果（读后清除，避免重复使用）。"""
        return []

    # ── 注入钩子（hermes_engine / main.py 调用，后端按需覆盖）──────

    def set_vector_store(self, vector_store: Any) -> None:
        """注入 VectorStore（vectordb 后端用，filesystem 后端无操作）。"""

    def set_default_llm(self, llm: Any) -> None:
        """注入默认 LLM（vectordb 后端用）。"""

    def set_archiver(self, archiver: Any) -> None:
        """注入记忆归档器（vectordb 后端用）。"""

    async def close(self) -> None:
        """释放资源（服务关闭时调用）。"""


class ChatHistoryBackend(ABC):
    """聊天历史存储后端抽象接口。

    封装聊天记录的持久化操作，使调用方（HermesEngine / API 层）
    不感知底层存储介质（Elasticsearch、文件系统等）。

    子类必须实现：save_turn、get_recent_messages、get_recent_turns。
    其余方法提供无操作默认实现，后端按需覆盖。
    """

    @abstractmethod
    async def save_turn(
        self,
        user_id: str,
        user_input: str,
        assistant_response: str,
        turn_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """将一轮对话持久化。返回写入成功的 turn_id，失败时返回空字符串。"""

    @abstractmethod
    async def get_recent_messages(
        self,
        user_id: str,
        size: int = 5,
        from_: int = 0,
    ) -> List[Dict[str, Any]]:
        """按 timestamp 降序分页返回展开后的消息列表（role/content 格式）。"""

    @abstractmethod
    async def get_recent_turns(
        self,
        user_id: str,
        size: int = 20,
        from_: int = 0,
        client_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按 timestamp 降序分页返回对话轮次列表（每条含 user_input + assistant_response）。"""

    # ── 扩展功能（可选覆盖）────────────────────────────────────────

    async def add_embedding(
        self,
        user_id: str,
        doc_id: str,
        embedding: List[float],
        vector_field: str = "embedding",
    ) -> bool:
        """向已存文档附加向量字段（供 RAG 检索使用）。"""
        return False

    async def vector_search(
        self,
        user_id: str,
        vector: List[float],
        top_k: int = 10,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """基于向量字段执行近邻搜索。"""
        return []

    async def list_indices(self, prefix: Optional[str] = None) -> List[str]:
        """列出存储后端中的索引/目录（可按前缀过滤）。"""
        return []

    async def count_index_docs(
        self, user_id: str, client_type: Optional[str] = None
    ) -> int:
        """返回指定 user_id 下的文档/记录数量。"""
        return 0

    async def summarize_recent(
        self, user_id: str, hours: int = 24, max_messages: int = 200
    ) -> str:
        """读取最近时间范围内的聊天记录，返回拼接文本（供摘要任务使用）。"""
        return ""


class RagBackend(ABC):
    """RAG 流水线后端抽象接口。

    子类必须实现 build_context。
    index_turn / queue_prefetch / revectorize 提供无操作默认实现。
    """

    @abstractmethod
    async def build_context(
        self,
        user_id: str,
        user_input: str,
        base_context: Optional[Dict[str, Any]] = None,
        top_k: int = 3,
    ) -> "RagContext":
        """组装 RAG 上下文（历史 + 相关记忆 + 格式化文本）。"""

    async def index_turn(
        self,
        user_id: str,
        turn_id: str,
        user_input: str,
        assistant_response: str,
        agent_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """向量化并写入索引（filesystem 后端默认跳过，返回 0）。"""
        return 0

    def queue_prefetch(self, user_id: str, query: str) -> None:
        """异步预取（可选）。"""

    async def revectorize(
        self,
        user_id: Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """重建向量索引（filesystem 后端不支持）。"""
        return {"status": "not_supported", "backend": "filesystem"}

    async def delete_all_vector_indices(self) -> int:
        """删除所有向量索引（filesystem 后端不支持）。"""
        return 0
