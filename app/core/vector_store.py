"""VectorStore - 管理会话向量数据在 ES 中的独立索引。

索引命名规则（与聊天历史严格分离）：
  聊天历史: {es_prefix}_{user_id}            例: hermes_chat_user123
  向量索引: {es_prefix}_v_{user_id}          例: hermes_chat_v_user123

向量文档结构：
  chunk_id        : ES 文档 ID，格式 {turn_id}_q0 / _a0 / _0
  user_id         : 所属用户（用于安全过滤）
  chunk_text      : 参与 embedding 的文本片段
  chunk_type      : qa_combined | question | answer_part
  chunk_index     : 本 turn 内的位置
  total_chunks    : 本 turn 的 chunk 总数
  ref_doc_id      : 关联聊天历史的 turn_id
  ref_chat_index  : 关联聊天历史的完整 ES 索引名
  timestamp       : 写入时间
  {vector_field}  : dense_vector

跨用户脱敏（占位）：检索结果 user_id 与请求 user_id 不一致时，
  调用 _desensitize() 清空文本字段，具体脱敏逻辑待后续实现。
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

_VEC_PARAM_PREFIX = "v_"   # 传给 ES 的 index 参数前缀，完整名 = {es_prefix}_v_{user_id}


# ── 脱敏占位 ───────────────────────────────────────────────────

def _desensitize(result: Dict[str, Any]) -> Dict[str, Any]:
    """跨用户命中时清空敏感字段（PII 脱敏待完整实现）。"""
    out = result.copy()
    out["chunk_text"]          = "[内容已脱敏]"
    out["user_input"]          = "[内容已脱敏]"
    out["assistant_response"]  = "[内容已脱敏]"
    out["_desensitized"]       = True
    return out


# ── 向量索引 Mapping ───────────────────────────────────────────

def _build_mapping(vector_field: str, dim: int, es_major_version: int = 8) -> Dict[str, Any]:
    # ES 8.x 支持 index+similarity；ES 7.x 的 dense_vector 仅接受 dims
    vec_field_mapping: Dict[str, Any] = {"type": "dense_vector", "dims": dim}
    if es_major_version >= 8:
        vec_field_mapping.update({"index": True, "similarity": "cosine"})
    return {
        "properties": {
            vector_field:      vec_field_mapping,
            "user_id":         {"type": "keyword"},
            "chunk_text":      {"type": "text", "analyzer": "standard"},
            "chunk_type":      {"type": "keyword"},
            "chunk_index":     {"type": "integer"},
            "total_chunks":    {"type": "integer"},
            "ref_doc_id":      {"type": "keyword"},
            "ref_chat_index":  {"type": "keyword"},
            "timestamp":       {"type": "date"},
        }
    }


class VectorStore:
    """向量索引的写入、检索与维护。由 main.py 初始化后注入 MemoryManager 和 ContextManager。"""

    def __init__(self, embedding_service, config: Dict[str, Any]):
        self.embed        = embedding_service
        es_cfg            = config.get("database", {}).get("elasticsearch", {})
        self.es_prefix    = es_cfg.get("index_prefix", "hermes_chat")
        self.vector_field = es_cfg.get("vector_field", "message_vector")
        embed_cfg         = config.get("embedding", {})
        self.vector_dim   = int(embed_cfg.get("model_dim", 768))

    # ── 辅助：索引名计算 ───────────────────────────────────────

    def _vec_param(self, user_id: str) -> str:
        """传给 ES 的 index 参数（ElasticsearchDatabase 会自动加 prefix）。"""
        return f"{_VEC_PARAM_PREFIX}{user_id}"

    def _chat_index_full(self, user_id: str) -> str:
        """聊天历史的完整 ES 索引名（用于写入 ref_chat_index 字段）。"""
        return f"{self.es_prefix}_{user_id}"

    def _vec_index_full(self, user_id: str) -> str:
        """向量索引的完整 ES 索引名。"""
        return f"{self.es_prefix}_{_VEC_PARAM_PREFIX}{user_id}"

    # ── 索引管理 ───────────────────────────────────────────────

    async def ensure_user_index(self, user_id: str, es_conn) -> bool:
        """确保用户向量索引存在，不存在则创建（含 dense_vector mapping）。"""
        es_ver = getattr(es_conn, "_es_major_version", 7)
        mapping = _build_mapping(self.vector_field, self.vector_dim, es_major_version=es_ver)
        return await es_conn.create_index(self._vec_param(user_id), mappings=mapping)

    async def delete_all_vector_indices(self) -> int:
        """删除所有用户的向量索引（更换向量模型时调用）。返回删除数量。"""
        es_conn = None
        deleted = 0
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return 0
            client = getattr(es_conn, "es_client", None)
            if not client:
                return 0
            pattern = f"{self.es_prefix}_{_VEC_PARAM_PREFIX}*"
            try:
                existing = client.indices.get(index=pattern)
                for idx in list(existing.keys()):
                    client.indices.delete(index=idx)
                    deleted += 1
                    logger.info(f"已删除向量索引: {idx}")
            except Exception as e:
                # 索引不存在时忽略 404
                if "index_not_found" not in str(e).lower() and "404" not in str(e):
                    raise
        except Exception as e:
            logger.error(f"删除向量索引失败: {e}")
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)
        logger.info(f"共删除向量索引 {deleted} 个")
        return deleted

    async def delete_turn_vectors(self, user_id: str, turn_id: str) -> int:
        """删除指定 turn 在向量索引中的所有分块（按 ref_doc_id 匹配）。返回删除数量。"""
        es_conn = None
        deleted = 0
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return 0
            client = getattr(es_conn, "es_client", None)
            if not client:
                return 0
            vec_index = self._vec_param(user_id)
            resp = client.delete_by_query(
                index=vec_index,
                body={"query": {"term": {"ref_doc_id": turn_id}}},
                refresh=False,
                ignore_unavailable=True,
            )
            deleted = resp.get("deleted", 0)
            if deleted:
                logger.info(
                    f"向量分块已删除 user={user_id} turn={turn_id[:8]}: {deleted} 块"
                )
        except Exception as e:
            logger.warning(
                f"向量分块删除失败 user={user_id} turn={turn_id[:8]}: {e}"
            )
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)
        return deleted

    # ── 写入 ───────────────────────────────────────────────────

    async def store_turn_vectors(
        self,
        user_id: str,
        turn_id: str,
        user_input: str,
        assistant_response: str,
        agent_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """对一轮对话切片 → embedding → 写入向量索引。返回成功存储的 chunk 数量。"""
        if not self.embed.enabled:
            return 0

        chat_index = self._chat_index_full(user_id)
        chunks = self.embed.chunk_turn(
            user_input, assistant_response, turn_id, chat_index,
            agent_outputs=agent_outputs,
        )
        if not chunks:
            return 0

        embeddings = await self.embed.embed_batch([c.chunk_text for c in chunks])

        es_conn = None
        stored = 0
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return 0
            await self.ensure_user_index(user_id, es_conn)

            for chunk, vec in zip(chunks, embeddings):
                if vec is None:
                    logger.debug(f"chunk {chunk.chunk_id} embedding 为空，跳过")
                    continue
                doc: Dict[str, Any] = {
                    "user_id":         user_id,
                    "chunk_text":      chunk.chunk_text,
                    "chunk_type":      chunk.chunk_type,
                    "chunk_index":     chunk.chunk_index,
                    "total_chunks":    chunk.total_chunks,
                    "ref_doc_id":      chunk.ref_doc_id,
                    "ref_chat_index":  chunk.ref_chat_index,
                    "timestamp":       datetime.now(timezone.utc).isoformat(),
                    self.vector_field: vec,
                }
                ok = await es_conn.create(
                    index=self._vec_param(user_id),
                    doc_id=chunk.chunk_id,
                    document=doc,
                )
                if ok:
                    stored += 1

        except Exception as e:
            logger.error(f"向量写入失败 user={user_id} turn={turn_id}: {e}")
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

        logger.info(f"向量存储完成 user={user_id} turn={turn_id}: {stored}/{len(chunks)} 块")
        return stored

    # ── 检索 ───────────────────────────────────────────────────

    async def search(
        self,
        user_id: str,
        query_text: str,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """向量检索用户自己的历史，返回最相关的 top_k 条（含关联的完整会话内容）。

        安全策略：
          1. KNN filter 限定 user_id == 当前用户（主要屏障）
          2. 结果逐条校验 user_id，发现跨用户命中时执行脱敏（防御性兜底）
        """
        if not self.embed.enabled:
            return []

        query_vec = await self.embed.embed(query_text)
        if not query_vec:
            return []

        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []
            client = getattr(es_conn, "es_client", None)
            if not client:
                return []

            full_index = self._vec_index_full(user_id)
            if not client.indices.exists(index=full_index):
                logger.debug(f"向量索引不存在 user={user_id}，跳过检索")
                return []

            es_ver = getattr(es_conn, "_es_major_version", 7)
            user_filter = {"term": {"user_id": user_id}}
            if es_ver >= 8:
                resp = client.search(
                    index=full_index,
                    knn={
                        "field":          self.vector_field,
                        "query_vector":   query_vec,
                        "k":              top_k,
                        "num_candidates": top_k * 10,
                        "filter":         user_filter,
                    },
                    size=top_k,
                    _source={"excludes": [self.vector_field]},
                )
            else:
                resp = client.search(
                    index=full_index,
                    body={
                        "query": {
                            "script_score": {
                                "query": user_filter,
                                "script": {
                                    "source": (
                                        f"cosineSimilarity(params.query_vector,"
                                        f" '{self.vector_field}') + 1.0"
                                    ),
                                    "params": {"query_vector": query_vec},
                                },
                            }
                        },
                        "size": top_k,
                        "_source": {"excludes": [self.vector_field]},
                    },
                )

            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return []

            # ── 批量回查聊天历史，补全 user_input / assistant_response ──
            raw_results = []
            ref_lookups: Dict[str, List[int]] = {}  # ref_chat_index -> [result_idx]
            for h in hits:
                src = h.get("_source", {})
                hit_user = src.get("user_id", "")
                r: Dict[str, Any] = {
                    "_score":        h.get("_score"),
                    "_source":       "vector",
                    "chunk_id":      h.get("_id"),
                    "chunk_text":    src.get("chunk_text", ""),
                    "chunk_type":    src.get("chunk_type", ""),
                    "ref_doc_id":    src.get("ref_doc_id", ""),
                    "ref_chat_index": src.get("ref_chat_index", ""),
                    "turn_id":       src.get("ref_doc_id", ""),
                    "timestamp":     src.get("timestamp"),
                    "user_id":       hit_user,
                    # 占位，下方回查后填充
                    "user_input":          "",
                    "assistant_response":  "",
                }
                # 脱敏：跨用户命中
                if hit_user and hit_user != user_id:
                    r = _desensitize(r)
                    raw_results.append(r)
                    continue

                raw_results.append(r)
                ref_idx = src.get("ref_chat_index", "")
                if ref_idx:
                    ref_lookups.setdefault(ref_idx, []).append(len(raw_results) - 1)

            # ── 回查聊天历史补全完整 Q&A ──────────────────────────────
            for chat_idx_name, positions in ref_lookups.items():
                doc_ids = [raw_results[p]["ref_doc_id"] for p in positions if raw_results[p].get("ref_doc_id")]
                if not doc_ids:
                    continue
                try:
                    mget_resp = client.mget(index=chat_idx_name, ids=doc_ids)
                    id_to_src: Dict[str, Dict] = {}
                    for doc in mget_resp.get("docs", []):
                        if doc.get("found"):
                            id_to_src[doc["_id"]] = doc.get("_source", {})
                    for pos in positions:
                        r = raw_results[pos]
                        turn_src = id_to_src.get(r.get("ref_doc_id", ""), {})
                        r["user_input"]         = turn_src.get("user_input", "")
                        r["assistant_response"] = turn_src.get("assistant_response", "")
                except Exception as e:
                    logger.warning(f"回查聊天历史失败 index={chat_idx_name}: {e}")

            # ── 按相似度倒排（ES 已倒排，此处仅做兜底确认）────────────
            raw_results.sort(key=lambda x: x.get("_score") or 0, reverse=True)
            return raw_results[:top_k]

        except Exception as e:
            logger.warning(f"向量检索失败 user={user_id}: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    # ── 重向量化（全量 / 按条件）──────────────────────────────

    async def revectorize_all_history(self) -> None:
        """后台任务：全量重向量化所有用户历史（换模型后由 main.py 调用）。"""
        await self.revectorize_filtered()

    async def revectorize_filtered(
        self,
        user_id:  Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按条件重新向量化聊天历史，并更新 ES 向量索引。

        Args:
            user_id:  指定用户 ID，None 表示所有用户。
            date_str: 指定日期（YYYY-MM-DD），None 表示不限日期。

        Returns:
            {"users": N, "turns": M, "chunks": K}
        """
        tag = f"user={user_id or '*'} date={date_str or '*'}"
        logger.info(f"▶ 开始重向量化 {tag}")

        stats = {"users": 0, "turns": 0, "chunks": 0}
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                logger.error("无法获取 ES 连接，重向量化中止")
                return stats
            client = getattr(es_conn, "es_client", None)
            if not client:
                return stats

            # 确定要处理的聊天历史索引
            if user_id:
                full_idx = f"{self.es_prefix}_{user_id}"
                try:
                    client.indices.get(index=full_idx)
                    chat_indices = [(user_id, full_idx)]
                except Exception:
                    logger.warning(f"用户 {user_id} 的聊天历史索引不存在")
                    return stats
            else:
                pattern = f"{self.es_prefix}_*"
                vec_pfx = f"{self.es_prefix}_{_VEC_PARAM_PREFIX}"
                try:
                    all_idx = list(client.indices.get(index=pattern).keys())
                except Exception:
                    all_idx = []
                pfx = f"{self.es_prefix}_"
                chat_indices = [
                    (n[len(pfx):] if n.startswith(pfx) else n, n)
                    for n in all_idx if not n.startswith(vec_pfx)
                ]

            logger.info(f"共 {len(chat_indices)} 个聊天历史索引")

            for uid, full_idx in chat_indices:
                try:
                    turns, chunks = await self._revectorize_index(
                        uid, full_idx, client, date_str=date_str
                    )
                    stats["users"]  += 1
                    stats["turns"]  += turns
                    stats["chunks"] += chunks
                except Exception as e:
                    logger.error(f"重向量化索引 {full_idx} 失败: {e}")

        except Exception as e:
            logger.error(f"重向量化任务失败: {e}")
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

        logger.info(
            f"✅ 重向量化完成 {tag}：处理 {stats['users']} 用户 "
            f"/ {stats['turns']} 轮次 / {stats['chunks']} 向量块"
        )
        return stats

    async def _revectorize_index(
        self,
        user_id:    str,
        full_index: str,
        client,
        date_str:   Optional[str] = None,
    ) -> tuple:
        """对单个用户的聊天历史索引分页重向量化。

        先删除该 turn 已有的向量块，再重新嵌入写入，避免模型切换后遗留孤儿 chunk。
        返回 (处理轮次数, 存储块数)。
        """
        # 构造 ES 日期范围过滤
        if date_str:
            from datetime import date as _date, timedelta
            try:
                d = _date.fromisoformat(date_str)
                es_query = {
                    "range": {
                        "timestamp": {
                            "gte": d.isoformat() + "T00:00:00",
                            "lt":  (d + timedelta(days=1)).isoformat() + "T00:00:00",
                        }
                    }
                }
            except ValueError:
                logger.warning(f"日期格式不合法，忽略日期过滤: {date_str}")
                es_query = {"match_all": {}}
        else:
            es_query = {"match_all": {}}

        vec_index_full = self._vec_index_full(user_id)
        vec_exists = client.indices.exists(index=vec_index_full)

        turns_done = 0
        chunks_stored = 0
        page_size = 50
        from_ = 0

        while True:
            try:
                resp = client.search(
                    index=full_index,
                    query=es_query,
                    size=page_size,
                    from_=from_,
                    _source=["turn_id", "user_input", "assistant_response"],
                    sort=[{"timestamp": {"order": "asc", "missing": "_last"}}],
                )
            except Exception as e:
                logger.warning(f"分页查询 {full_index} 失败: {e}")
                break

            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break

            for h in hits:
                src     = h.get("_source", {})
                turn_id = src.get("turn_id") or h.get("_id", "")
                u_input = src.get("user_input", "")
                a_resp  = src.get("assistant_response", "")
                if not u_input and not a_resp:
                    continue

                # 删除该 turn 的旧向量块（避免模型切换后 chunk 数不同遗留孤儿文档）
                if vec_exists:
                    try:
                        client.delete_by_query(
                            index=vec_index_full,
                            query={"term": {"ref_doc_id": turn_id}},
                        )
                    except Exception:
                        pass

                n = await self.store_turn_vectors(user_id, turn_id, u_input, a_resp)
                chunks_stored += n
                turns_done    += 1
                await asyncio.sleep(0.05)

            from_ += page_size
            if len(hits) < page_size:
                break

        logger.info(f"  用户 {user_id}: {turns_done} 轮次 / {chunks_stored} 块")
        return turns_done, chunks_stored
