"""
【模块说明】文件系统后端 — RAG 流水线

实现 RagBackend 接口，基于 FilesystemMemoryBackend 构建 RAG 上下文。

与 VectorDB RagPipeline 的区别：
  - 检索：关键词评分（search.py），而非 ES + 向量搜索
  - 索引：写入 manifest.jsonl（store_turn 时已完成），无需额外向量化步骤
  - 预取：调用 FilesystemMemoryBackend.queue_prefetch()
  - 重向量化：不支持，返回说明信息

输出格式与 VectorDB RagPipeline 完全兼容（返回 RagContext），
hermes_engine 无需区分后端。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.memory.base import RagBackend
from app.rag.formatter import format_memories
from app.rag.types import RagContext

logger = logging.getLogger(__name__)


class FilesystemRagPipeline(RagBackend):
    """文件系统 RAG 流水线，基于关键词检索，无外部依赖。

    对外接口与 app.rag.pipeline.RagPipeline 完全兼容。
    """

    def __init__(self, memory_backend, config: Dict[str, Any]) -> None:
        self._memory = memory_backend
        self._recent_n: int = int(config.get("recent_turns", 10))

    # ── 核心：组装 RAG 上下文 ───────────────────────────────────────────────

    async def build_context(
        self,
        user_id: str,
        user_input: str,
        base_context: Optional[Dict[str, Any]] = None,
        top_k: int = 3,
    ) -> RagContext:
        """组装本次对话的完整 RAG 上下文。

        流程：
          1. 从 manifest 加载最近 N 轮对话（session 隔离）
          2. 尝试读取预取缓存，命中则直接使用；否则实时关键词检索
          3. 将对话列表转为 message 格式，格式化 memory_text
        """
        base_context = base_context or {}
        client_type = base_context.get("_client_type", "") or ""

        # Step 1: 近期历史（session 隔离）
        history = await self._load_recent_history(user_id, base_context, client_type)

        # Step 2: 检索相关记忆
        memories: List[Dict[str, Any]] = []
        if user_input:
            # 优先消费预取缓存
            cached = await self._memory.get_prefetched_context(user_id)
            if len(cached) >= top_k:
                memories = cached[:top_k]
            else:
                memories = await self._memory.retrieve_memory(
                    user_id, user_input, top_k=top_k
                )

        # Step 3: 格式化记忆文本
        memory_text = format_memories(memories)

        return RagContext(
            history=history,
            memories=memories,
            memory_text=memory_text,
            base_context=base_context,
        )

    async def _load_recent_history(
        self,
        user_id: str,
        base_context: Dict[str, Any],
        client_type: str = "",
    ) -> List[Dict[str, Any]]:
        """加载最近 N 轮对话并转换为 LLM message 格式。

        与调用方传入的 base_context["history"] 合并（调用方优先追加在后）。
        """
        try:
            raw_turns = await self._memory.get_recent_turns(
                user_id, client_type=client_type
            )
            flat: List[Dict[str, Any]] = []
            for turn in raw_turns:
                if turn.get("user_input"):
                    flat.append({"role": "user", "content": turn["user_input"]})
                if turn.get("assistant_response"):
                    flat.append({"role": "assistant", "content": turn["assistant_response"]})

            caller_history = base_context.get("history") or []
            if isinstance(caller_history, list):
                flat = flat + caller_history
            return flat

        except Exception as e:
            logger.warning(
                "FilesystemRagPipeline._load_recent_history 失败 user=%s: %s",
                user_id, e,
            )
            return list(base_context.get("history") or [])

    # ── 索引（文件系统后端已在 store_turn 写入 manifest，无需额外步骤）───────

    async def index_turn(
        self,
        user_id: str,
        turn_id: str,
        user_input: str,
        assistant_response: str,
        agent_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """文件系统后端：索引已在 store_turn 完成，此处无操作。"""
        return 0

    # ── 预取 ─────────────────────────────────────────────────────────────────

    def queue_prefetch(self, user_id: str, query: str) -> None:
        """将预取任务委派给 memory backend。"""
        self._memory.queue_prefetch(user_id, query)

    # ── 重向量化（不支持）──────────────────────────────────────────────────

    async def revectorize(
        self,
        user_id: Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "status":  "not_supported",
            "backend": "filesystem",
            "message": "文件系统后端不使用向量索引，无需重向量化。",
        }

    async def delete_all_vector_indices(self) -> int:
        return 0
