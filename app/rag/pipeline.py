"""
【模块说明】RAG 流水线（RagPipeline）— RAG 系统的统一入口，外部只与它交互

HermesEngine（AI 引擎）需要在每次对话前"检索相关记忆"、每次对话后"存储新内容"，
它不直接操作 VectorStore / EmbeddingService 等底层组件，只调用这个类提供的接口。

【四个主要操作】
  build_context()    — 检索阶段：在 AI 回答前，找到相关历史，组装上下文
  index_turn()       — 写入阶段：对话结束后，后台把本轮对话异步写入记忆索引
  revectorize()      — 重建索引：模型更换或管理员手动触发时，重新向量化所有历史
  queue_prefetch()   — 预取优化：当前对话结束时，提前计算下轮可能需要的记忆，存入 Redis 缓存

RagPipeline — RAG 系统对外唯一入口类。

对外暴露四条路径：
  1. build_context()        — 检索 + 历史加载 + 上下文组装（LLM 推理前调用）
  2. index_turn()           — 对话向量化写入（对话存储后后台触发）
  3. revectorize()          — 重向量化（模型变更 / 管理员手动触发）
  4. queue_prefetch()       — 提交预取任务（优化下轮检索延迟）

调用方（HermesEngine）只与此类交互，不直接依赖 VectorStore / EmbeddingService 等底层组件。
内部实现细节（HybridRetriever、TurnIndexer、格式化函数）对调用方透明。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.rag.types     import RagContext
from app.rag.retriever import HybridRetriever
from app.rag.indexer   import TurnIndexer
from app.rag.formatter import format_memories

logger = logging.getLogger(__name__)


class RagPipeline:
    """RAG 统一入口，调用方唯一依赖的接口类。

    Usage::

        # main.py 初始化
        rag = RagPipeline(embedding_service, vector_store, memory_manager, config)
        hermes_engine.set_rag_pipeline(rag)

        # process_user_input 推理前
        ctx = await rag.build_context(user_id, user_input, base_context)
        context = ctx.to_prompt_context()

        # 对话存储后异步触发
        asyncio.create_task(rag.index_turn(user_id, turn_id, user_input, response))

        # 管理员重向量化
        stats = await rag.revectorize(user_id="user_xxx", date_str="2025-01-01")
    """

    def __init__(
        self,
        embedding_service,
        vector_store,
        memory_manager,
        config: Dict[str, Any],
    ) -> None:
        self._memory    = memory_manager
        self._retriever = HybridRetriever(vector_store, memory_manager, config)
        self._indexer   = TurnIndexer(vector_store)

    # ══════════════════════════════════════════════════════════
    # 1. 上下文组装（检索路径）
    # ══════════════════════════════════════════════════════════

    async def build_context(
        self,
        user_id:      str,
        user_input:   str,
        base_context: Optional[Dict[str, Any]] = None,
        top_k:        int = 3,
    ) -> RagContext:
        """组装本次对话的完整 RAG 上下文。

        流程：
          1. 从 Redis 拉取当前 session（client_type）的最近 N 轮对话作为 history
          2. 三路混合检索（预取缓存 > 向量 > BM25）取 top_k 条相关记忆
             — 同 session 的记忆命中会获得分数提升，确保优先进入上下文
          3. 相关性过滤（阈值剔除低分记忆）
          4. 格式化为 memory_text（<memory-context> 围栏标签）

        Returns:
            RagContext，调用 .to_prompt_context() 可直接传入 LLM。
        """
        base_context = base_context or {}
        client_type  = base_context.get("_client_type", "") or ""

        # Step 1: 近期历史（session 隔离：仅加载同 client_type 的轮次）
        history = await self._load_recent_history(user_id, base_context, client_type)

        # Step 2: 检索（同 session 命中获得分数提升）
        raw_memories = await self._retriever.retrieve(
            user_id, user_input, top_k, session_client_type=client_type
        )

        # Step 3: 过滤
        memories = self._retriever.filter_by_score(raw_memories)
        if len(raw_memories) > len(memories):
            logger.debug(
                "记忆过滤 user=%s: %d 条检索 → %d 条保留",
                user_id, len(raw_memories), len(memories),
            )

        # Step 4: 格式化为提示词文本
        memory_text = format_memories(memories)

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
        client_type:  str = "",
    ) -> List[Dict[str, Any]]:
        """从 Redis 拉取最近对话，与 base_context 中的 history 合并。

        client_type 非空时读取 session 隔离列表，仅返回同客户端的历史对话。
        """
        try:
            redis_turns = await self._memory.get_recent_turns(user_id, client_type=client_type)
            flat: List[Dict[str, Any]] = []
            for turn in redis_turns:
                if turn.get("user_input"):
                    flat.append({"role": "user",      "content": turn["user_input"]})
                if turn.get("assistant_response"):
                    flat.append({"role": "assistant", "content": turn["assistant_response"]})
                if turn.get("turn_id"):
                    asyncio.create_task(
                        self._memory._mysql_increment_ref(user_id, turn["turn_id"])
                    )
            caller_history = base_context.get("history") or []
            if isinstance(caller_history, list):
                flat = flat + caller_history
            return flat
        except Exception as e:
            logger.warning("加载近期历史失败 user=%s: %s", user_id, e)
            return list(base_context.get("history") or [])

    # ══════════════════════════════════════════════════════════
    # 2. 向量索引写入（索引路径）
    # ══════════════════════════════════════════════════════════

    async def index_turn(
        self,
        user_id:            str,
        turn_id:            str,
        user_input:         str,
        assistant_response: str,
        agent_outputs:      Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """对一轮对话向量化写入（切片 → embedding → ES 向量索引）。

        Returns:
            成功存储的向量块数量；embedding 未启用时返回 0。
        """
        return await self._indexer.index_turn(
            user_id, turn_id, user_input, assistant_response,
            agent_outputs=agent_outputs,
        )

    # ══════════════════════════════════════════════════════════
    # 3. 重向量化（管理路径）
    # ══════════════════════════════════════════════════════════

    async def revectorize(
        self,
        user_id:  Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """重向量化历史对话记录（模型变更 / 管理员触发）。

        Args:
            user_id:  指定用户 ID，None 表示全量。
            date_str: 指定日期 YYYY-MM-DD，None 表示不限日期。

        Returns:
            {"users": N, "turns": M, "chunks": K}
        """
        return await self._indexer.revectorize(user_id=user_id, date_str=date_str)

    async def delete_all_vector_indices(self) -> int:
        """删除所有用户的向量索引（更换 embedding 模型前调用）。"""
        return await self._indexer.delete_all_indices()

    # ══════════════════════════════════════════════════════════
    # 4. 预取优化（加速路径）
    # ══════════════════════════════════════════════════════════

    def queue_prefetch(self, user_id: str, query: str) -> None:
        """提交下轮预取任务（异步执行，减少下次检索延迟）。"""
        try:
            self._memory.queue_prefetch(user_id, query)
        except Exception as e:
            logger.debug("queue_prefetch 失败 user=%s: %s", user_id, e)
