"""对话轮次向量索引器（RAG 写入路径）。

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
