"""
【模块说明】对话向量索引器（Indexer）— 把对话"写进记忆"的流水线

每次 AI 完成一轮对话后，系统会在后台把这轮对话"记住"——
具体来说就是把对话文本转换成向量，写入 Elasticsearch 的向量搜索索引。
下次用户有相关问题时，就能通过向量相似度检索到这段历史。

【写入流程】
  一轮对话文本
    → 切片（Chunker）：拆成多个小段
    → 向量化（EmbeddingService）：每段文字转成数字向量
    → 存储（VectorStore → Elasticsearch）：写入搜索索引

【两个对外接口】
  index_turn()    — 索引一轮新对话（对话结束后后台触发）
  revectorize()   — 重新索引历史对话（模型更新或手动触发时用）

对话轮次向量索引器（RAG 写入路径）。

封装切片 → Embedding → ES 向量写入的完整链路，对外提供 index_turn() 和 revectorize() 两个接口。
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TurnIndexer:
    """对话轮次向量索引器。

    底层依赖 VectorStore 完成 ES 操作；VectorStore 内部已集成 TurnChunker + EmbeddingService，
    此处仅做路径封装，屏蔽 VectorStore 细节供 RagPipeline 调用。
    """

    def __init__(self, vector_store) -> None:
        self._vs = vector_store

    async def index_turn(
        self,
        user_id:            str,
        turn_id:            str,
        user_input:         str,
        assistant_response: str,
        agent_outputs:      Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """对一轮对话切片 → embedding → 写入向量索引。

        Returns:
            成功存储的向量块数量；embedding 服务未启用时返回 0。
        """
        if self._vs is None:
            return 0
        return await self._vs.store_turn_vectors(
            user_id, turn_id, user_input, assistant_response,
            agent_outputs=agent_outputs,
        )

    async def revectorize(
        self,
        user_id:  Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按条件重向量化历史对话记录。

        Args:
            user_id:  指定用户 ID，None 表示全量。
            date_str: 指定日期 YYYY-MM-DD，None 表示不限日期。

        Returns:
            {"users": N, "turns": M, "chunks": K}
        """
        if self._vs is None:
            return {"users": 0, "turns": 0, "chunks": 0}
        return await self._vs.revectorize_filtered(user_id=user_id, date_str=date_str)

    async def delete_all_indices(self) -> int:
        """删除所有用户的向量索引（更换 embedding 模型时调用）。

        Returns:
            删除的索引数量。
        """
        if self._vs is None:
            return 0
        return await self._vs.delete_all_vector_indices()
