"""
【模块说明】上下文管理器 — 已被 RagPipeline 取代，保留兼容

【历史背景】
  这个模块原来负责在 AI 回答之前，从记忆系统中取出相关的历史对话，
  组装成 AI 需要的"上下文包"（ContextBundle）。

【现状】
  核心逻辑已迁移到更完善的 `app/rag/` 模块：
  - 记忆检索和相关性打分 → app.rag.retriever.HybridRetriever
  - 记忆文本格式化       → app.rag.formatter
  - 最终上下文组装       → app.rag.pipeline.RagPipeline.build_context()

  本模块仅作为过渡层保留，供老代码使用。
  新开发的功能请直接使用 `app.rag.RagPipeline`。
"""


import logging
from typing import Any, Dict, List, Optional

# 向后兼容：重新导出核心类型和格式化函数
from app.rag.types     import RagContext
from app.rag.formatter import (
    sanitize_memory_content,
    build_memory_context_block,
    format_memories,
)

logger = logging.getLogger(__name__)

# ContextBundle 作为 RagContext 的别名保留向后兼容
ContextBundle = RagContext


class ContextManager:
    """
    上下文管理器（已被 RagPipeline 取代，保留向后兼容）。

    功能：在 AI 处理用户消息之前，从记忆系统取出相关历史，
    组装成供 AI 使用的上下文信息包（ContextBundle）。

    注意：新代码请直接使用 app.rag.RagPipeline.build_context()。
    """

    def __init__(self, memory_manager, config: Dict[str, Any]) -> None:
        self.memory       = memory_manager
        self.vector_store = None
        self._rag_pipeline = None

        sys_cfg       = config.get("system", config)
        retrieval_cfg = sys_cfg.get("retrieval", {})
        self._config  = config
        self._vector_score_threshold = float(retrieval_cfg.get("confidence_threshold", 0.7))
        self._text_relative_min      = float(retrieval_cfg.get("text_relative_min", 0.3))
        self._text_abs_floor         = float(retrieval_cfg.get("text_abs_floor", 0.5))

    def set_vector_store(self, vector_store) -> None:
        """注入 VectorStore（由 main.py 在两者初始化后调用）。"""
        self.vector_store = vector_store
        logger.info("VectorStore 已注入 ContextManager（兼容模式）")

    def set_rag_pipeline(self, rag_pipeline) -> None:
        """注入 RagPipeline，后续 build_context() 将委托给它。"""
        self._rag_pipeline = rag_pipeline

    async def build_context(
        self,
        user_id:      str,
        user_input:   str,
        base_context: Optional[Dict[str, Any]] = None,
        top_k:        int = 3,
    ) -> RagContext:
        """组装对话上下文（委托给 RagPipeline；未注入时使用内联实现兜底）。"""
        if self._rag_pipeline is not None:
            return await self._rag_pipeline.build_context(
                user_id=user_id,
                user_input=user_input,
                base_context=base_context,
                top_k=top_k,
            )

        # ── 兜底：RagPipeline 未注入时的内联实现 ───────────────
        logger.warning(
            "ContextManager.build_context: RagPipeline 未注入，使用兜底实现 user=%s", user_id
        )
        from app.rag.retriever import HybridRetriever
        retriever = HybridRetriever(self.vector_store, self.memory, self._config)
        base_context = base_context or {}

        history      = await self._load_recent_history(user_id, base_context)
        raw_memories = await retriever.retrieve(user_id, user_input, top_k)
        memories     = retriever.filter_by_score(raw_memories)
        memory_text  = format_memories(memories)

        return RagContext(
            history=history,
            memories=memories,
            memory_text=memory_text,
            base_context=base_context,
        )

    async def _load_recent_history(
        self,
        user_id:      str,
        base_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        import asyncio
        client_type = base_context.get("_client_type", "") or ""
        try:
            redis_turns = await self.memory.get_recent_turns(user_id, client_type=client_type)
            flat: List[Dict[str, Any]] = []
            for turn in redis_turns:
                if turn.get("user_input"):
                    flat.append({"role": "user",      "content": turn["user_input"]})
                if turn.get("assistant_response"):
                    flat.append({"role": "assistant", "content": turn["assistant_response"]})
                if turn.get("turn_id"):
                    asyncio.create_task(
                        self.memory._mysql_increment_ref(user_id, turn["turn_id"])
                    )
            caller_history = base_context.get("history") or []
            if isinstance(caller_history, list):
                flat = flat + caller_history
            return flat
        except Exception as e:
            logger.warning("加载近期历史失败 user=%s: %s", user_id, e)
            return list(base_context.get("history") or [])
