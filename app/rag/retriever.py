"""混合检索器：预取缓存 > 向量检索 > BM25 全文检索。

从 ContextManager._retrieve_memories / _filter_by_score 提取的检索与评分逻辑。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HybridRetriever:
    """三路混合检索器（预取缓存 / 向量语义 / BM25 全文，按优先级合并去重）。

    检索优先级：
      0. Redis prefetch cache     — 上一轮结束后异步预取的结果（TTL 5 min）
      1. VectorStore.search()     — KNN 向量语义检索（已启用时）
      2. MemoryManager.retrieve_memory() — ES BM25 全文检索（补足或兜底）

    过滤策略：
      - 向量结果：余弦相似度绝对阈值（默认 0.7）
      - 全文结果：相对阈值（最高分 × ratio）+ 绝对下限（0.5）
      - 无分数字段的结果默认保留

    Session 分数提升：
      - 同 client_type 的结果乘以 session_score_boost（默认 1.5）
      - 提升后按分数重排，确保同 session 的相关记忆优先出现在上下文中
    """

    def __init__(
        self,
        vector_store,
        memory_manager,
        config: Dict[str, Any],
    ) -> None:
        self.vector_store = vector_store
        self.memory       = memory_manager

        sys_cfg       = config.get("system", config)
        retrieval_cfg = sys_cfg.get("retrieval", {})
        self.vector_score_threshold: float = float(
            retrieval_cfg.get("confidence_threshold", 0.7)
        )
        self.text_relative_min: float = float(
            retrieval_cfg.get("text_relative_min", 0.3)
        )
        self.text_abs_floor: float = float(
            retrieval_cfg.get("text_abs_floor", 0.5)
        )
        # 同 session（client_type）命中时的分数放大倍率（>1 提升排名）
        self.session_score_boost: float = float(
            retrieval_cfg.get("session_score_boost", 1.5)
        )

    # ══════════════════════════════════════════════════════════
    # 检索
    # ══════════════════════════════════════════════════════════

    async def retrieve(
        self,
        user_id:             str,
        query:               str,
        top_k:               int = 3,
        session_client_type: str = "",
    ) -> List[Dict[str, Any]]:
        """三路混合检索，返回去重后最多 top_k 条结果。

        session_client_type: 当前请求的客户端类型（如 "lark"/"wechat"/"api"）。
            非空时对同 client_type 的命中结果乘以 session_score_boost，提升排名。
        """
        results:  List[Dict[str, Any]] = []
        seen_ids: set = set()

        # ── Step 0: 消费预取缓存（原子 GETDEL）────────────────
        try:
            prefetched = await self.memory.get_prefetched_context(user_id)
            if prefetched:
                for hit in prefetched:
                    tid = hit.get("turn_id") or hit.get("_id", "")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        results.append(hit)
                logger.debug("预取缓存命中 user=%s: %d 条", user_id, len(results))
                if len(results) >= top_k:
                    return self._apply_session_boost(results[:top_k], session_client_type)
        except Exception as e:
            logger.warning("读取预取缓存失败 user=%s: %s", user_id, e)

        # ── Step 1: 向量检索 ───────────────────────────────────
        if self.vector_store is not None and getattr(
            self.vector_store.embed, "enabled", False
        ):
            try:
                vec_hits = await self.vector_store.search(user_id, query, top_k=top_k)
                for hit in vec_hits:
                    tid = (
                        hit.get("turn_id")
                        or hit.get("ref_doc_id")
                        or hit.get("chunk_id", "")
                    )
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        results.append(hit)
                        asyncio.create_task(
                            self.memory._mysql_increment_ref(user_id, tid)
                        )
                logger.debug(
                    "向量检索 user=%s: 命中 %d 条，去重后 %d 条",
                    user_id, len(vec_hits), len(results),
                )
            except Exception as e:
                logger.warning("VectorStore 检索失败 user=%s: %s", user_id, e)

        # ── Step 2: BM25 全文补足 ─────────────────────────────
        remaining = top_k - len(results)
        if remaining > 0:
            try:
                text_hits = await self.memory.retrieve_memory(
                    user_id, query, top_k=remaining * 2
                )
                for hit in text_hits:
                    tid = hit.get("turn_id") or hit.get("_id", "")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        results.append(hit)
                        if len(results) >= top_k:
                            break
            except Exception as e:
                logger.warning("全文检索失败 user=%s: %s", user_id, e)

        return self._apply_session_boost(results[:top_k], session_client_type)

    def _apply_session_boost(
        self,
        results: List[Dict[str, Any]],
        session_client_type: str,
    ) -> List[Dict[str, Any]]:
        """对同 session（client_type）的命中结果乘以 session_score_boost 并重排。

        仅当 session_client_type 非空且结果中存在 client_type 字段时生效。
        无分数字段（score=None）的结果不参与排序，保持原位追加在末尾。
        """
        if not session_client_type or self.session_score_boost <= 1.0:
            return results

        boosted = False
        for hit in results:
            if hit.get("client_type") == session_client_type:
                score = hit.get("_score")
                if score is not None:
                    hit["_score"] = score * self.session_score_boost
                    boosted = True

        if not boosted:
            return results

        scored   = [h for h in results if h.get("_score") is not None]
        unscored = [h for h in results if h.get("_score") is None]
        scored.sort(key=lambda x: x["_score"], reverse=True)
        logger.debug(
            "session boost 应用 user client_type=%s boost=%.1f",
            session_client_type, self.session_score_boost,
        )
        return scored + unscored

    # ══════════════════════════════════════════════════════════
    # 相关性过滤
    # ══════════════════════════════════════════════════════════

    def filter_by_score(
        self,
        memories: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """按相关性阈值过滤记忆，低匹配度结果直接丢弃。"""
        if not memories:
            return []

        vector_hits = [m for m in memories if m.get("_source") == "vector"]
        text_hits   = [m for m in memories if m.get("_source") == "es_text"]
        no_src_hits = [m for m in memories if m.get("_source") not in ("vector", "es_text")]

        kept: List[Dict[str, Any]] = []

        # 向量结果：余弦相似度绝对阈值
        for m in vector_hits:
            score = m.get("_score")
            if score is None or score >= self.vector_score_threshold:
                kept.append(m)
            else:
                logger.debug(
                    "向量记忆过滤 turn=%s score=%.3f < %.3f",
                    m.get("turn_id", "?"), score, self.vector_score_threshold,
                )

        # 全文结果：相对阈值 + 绝对下限
        if text_hits:
            scores  = [m.get("_score") or 0.0 for m in text_hits]
            max_s   = max(scores)
            rel_min = max_s * self.text_relative_min
            floor   = max(rel_min, self.text_abs_floor)
            for m, s in zip(text_hits, scores):
                if m.get("_score") is None or s >= floor:
                    kept.append(m)
                else:
                    logger.debug(
                        "文本记忆过滤 turn=%s score=%.3f < floor=%.3f",
                        m.get("turn_id", "?"), s, floor,
                    )

        # 无来源信息的结果默认保留
        kept.extend(no_src_hits)
        return kept
