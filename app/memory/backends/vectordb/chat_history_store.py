"""
【模块说明】VectorDB 聊天历史存储后端（ChatHistoryStore）

基于 Elasticsearch 实现 ChatHistoryBackend 接口，封装聊天记录的增删改查与向量操作。
每位用户的对话历史存在 ES 中独立的索引（Index）里，互相隔离。

【主要操作】
  save_turn()           — 保存一轮对话（用户问题 + AI 回复）
  get_recent_messages() — 获取最近几条消息（role/content 展开格式，供 LLM 上下文使用）
  get_recent_turns()    — 获取最近几轮对话（含完整元数据，供历史记录 API 使用）
  add_embedding()       — 向已存文档附加向量字段（RAG 检索用）
  vector_search()       — 基于向量字段执行近邻搜索
  list_indices()        — 列出 ES 中所有索引
  count_index_docs()    — 统计指定索引的文档数量
  summarize_recent()    — 拼接最近时间段内的聊天记录文本
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.database.pool import get_connection, release_connection
from app.memory.base import ChatHistoryBackend

logger = logging.getLogger(__name__)

_SYSTEM_TYPES = {"compression_summary", "monthly_summary", "yearly_summary"}


class ChatHistoryStore(ChatHistoryBackend):
    """基于 Elasticsearch 的聊天历史存储，实现 ChatHistoryBackend 接口。

    索引命名：使用 `user_id` 作为索引名（底层连接池内部会加全局 index_prefix）。
    每次操作独立获取并释放 ES 连接，与连接池生命周期解耦。
    """

    def __init__(self) -> None:
        pass

    # ── 内部写入辅助 ──────────────────────────────────────────────────────────

    async def _write_document(
        self, es_conn, user_id: str, doc_id: str, document: dict
    ) -> bool:
        """将文档写入 ES；失败时回退到底层 client 直接写入。"""
        try:
            success = await es_conn.create(
                index=user_id, doc_id=doc_id, document=document, refresh=True
            )
        except Exception as e:
            success = False
            logger.warning(f"es_conn.create 抛出异常: {e}")

        if success:
            return True

        client = getattr(es_conn, "es_client", None)
        if not client:
            return False
        try:
            if hasattr(es_conn, "_get_index_name"):
                full_index = es_conn._get_index_name(user_id)
            else:
                prefix = getattr(es_conn, "index_prefix", "") or ""
                full_index = f"{prefix}_{user_id}" if prefix else user_id

            resp = client.index(
                index=full_index, id=doc_id, document=document, refresh=True
            )
            if resp is not None and resp.get("result") in ("created", "updated"):
                return True
        except Exception as e:
            logger.warning(f"底层 client.index 写入失败: {e}")
        return False

    # ── ChatHistoryBackend 核心接口 ───────────────────────────────────────────

    async def save_turn(
        self,
        user_id: str,
        user_input: str,
        assistant_response: str,
        turn_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """将一轮对话（用户输入 + 模型回复）作为单一文档写入 ES。

        返回写入成功的 turn_id，失败时返回空字符串。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            turn_id = turn_id or uuid.uuid4().hex
            document: Dict[str, Any] = {
                "turn_id": turn_id,
                "user_input": user_input,
                "assistant_response": assistant_response,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if metadata:
                document.update(metadata)

            ok = await self._write_document(es_conn, user_id, turn_id, document)
            if ok:
                logger.debug(f"已保存对话轮次到 ES: index={user_id} turn_id={turn_id}")
                return turn_id

            logger.warning(f"保存对话轮次到 ES 失败: index={user_id} turn_id={turn_id}")
            return ""
        except Exception as e:
            logger.warning(f"保存对话轮次到 ES 出错: {e}")
            return ""
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def get_recent_messages(
        self,
        user_id: str,
        size: int = 5,
        from_: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取聊天消息，按 timestamp 降序分页返回展开后的 role/content 列表。

        Args:
            user_id: 用户 ID（对应 ES 索引名）
            size:    每页轮次数
            from_:   分页偏移（turn 级别）
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)

            res = await es_conn.search(
                index=user_id,
                query={"match_all": {}},
                size=size,
                from_=from_,
                sort=[{"timestamp": {"order": "desc", "unmapped_type": "date"}}],
            )
            hits = res.get("hits", {}).get("hits", []) if res is not None else []

            turns: List[Dict[str, Any]] = []
            for h in hits:
                src = h.get("_source", h) if isinstance(h, dict) else {}
                if not isinstance(src, dict):
                    continue

                meta = src.get("metadata") or {}
                if isinstance(meta, dict) and meta.get("type") in _SYSTEM_TYPES:
                    continue
                if src.get("user_input") == "[系统摘要]":
                    continue

                doc_id = h.get("_id") if isinstance(h, dict) else None
                ts = src.get("timestamp")

                if "user_input" in src:
                    turns.append({
                        "_doc_type": "turn",
                        "turn_id": src.get("turn_id", doc_id),
                        "user_input": src.get("user_input", ""),
                        "assistant_response": src.get("assistant_response", ""),
                        "timestamp": ts,
                        "_id": doc_id,
                    })
                else:
                    role = src.get("role") or src.get("type") or "user"
                    content = (
                        src.get("content")
                        or src.get("question")
                        or src.get("text")
                        or ""
                    )
                    turns.append({
                        "_doc_type": "legacy",
                        "role": role,
                        "content": content,
                        "timestamp": ts,
                        "_id": doc_id,
                    })

            messages: List[Dict[str, Any]] = []
            for item in turns:
                if item["_doc_type"] == "turn":
                    messages.append({
                        "role": "user",
                        "content": item["user_input"],
                        "timestamp": item["timestamp"],
                        "turn_id": item["turn_id"],
                    })
                    messages.append({
                        "role": "assistant",
                        "content": item["assistant_response"],
                        "timestamp": item["timestamp"],
                        "turn_id": item["turn_id"],
                    })
                else:
                    messages.append({
                        "role": item["role"],
                        "content": item["content"],
                        "timestamp": item["timestamp"],
                        "_id": item["_id"],
                    })
            return messages

        except Exception as e:
            logger.warning(f"从 ES 获取最近消息失败: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def get_recent_turns(
        self,
        user_id: str,
        size: int = 20,
        from_: int = 0,
        client_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取对话轮次列表，每条包含 user_input + assistant_response，按 timestamp 降序分页。

        供历史记录 API 使用；LLM 上下文构建请使用 get_recent_messages。
        client_type 非空时只返回该客户端的对话。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)

            query: dict = (
                {"term": {"client_type": client_type}} if client_type else {"match_all": {}}
            )

            res = await es_conn.search(
                index=user_id,
                query=query,
                size=size,
                from_=from_,
                sort=[{"timestamp": {"order": "desc", "unmapped_type": "date"}}],
            )
            hits = res.get("hits", {}).get("hits", []) if res is not None else []

            turns: List[Dict[str, Any]] = []
            for h in hits:
                src = h.get("_source", h) if isinstance(h, dict) else {}
                if not isinstance(src, dict):
                    continue

                meta = src.get("metadata") or {}
                if isinstance(meta, dict) and meta.get("type") in _SYSTEM_TYPES:
                    continue
                if src.get("user_input") == "[系统摘要]":
                    continue

                doc_id = h.get("_id") if isinstance(h, dict) else None
                turns.append({
                    "turn_id":            src.get("turn_id", doc_id),
                    "user_input":         src.get("user_input", ""),
                    "assistant_response": src.get("assistant_response", ""),
                    "timestamp":          src.get("timestamp"),
                    "intent":             src.get("intent", ""),
                    "mode":               src.get("mode", ""),
                    "pipeline":           src.get("pipeline", []),
                    "agent_outputs":      src.get("agent_outputs", []),
                    "client_type":        src.get("client_type", ""),
                })

            return turns

        except Exception as e:
            logger.warning(f"从 ES 获取对话轮次失败: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    # ── 扩展功能 ──────────────────────────────────────────────────────────────

    async def add_embedding(
        self,
        user_id: str,
        doc_id: str,
        embedding: List[float],
        vector_field: str = "embedding",
    ) -> bool:
        """向已有文档添加/更新向量字段，以便后续向量检索使用。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            return await es_conn.update(
                index=user_id, doc_id=doc_id, document={vector_field: embedding}
            )
        except Exception as e:
            logger.warning(f"为文档添加向量失败: {e}")
            return False
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def vector_search(
        self,
        user_id: str,
        vector: List[float],
        top_k: int = 10,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """基于向量字段执行近邻搜索（封装底层 vector_search）。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            return await es_conn.vector_search(
                index=user_id, vector=vector, top_k=top_k, filter=filter
            )
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def list_indices(self, prefix: Optional[str] = None) -> List[str]:
        """列出 ES 中的索引（可按前缀过滤）。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            client = getattr(es_conn, "es_client", None)
            if not client:
                return []

            pattern = f"{prefix}*" if prefix else "*"
            indices_dict = client.indices.get(index=pattern)
            return list(indices_dict.keys())
        except Exception as e:
            logger.warning(f"列出索引失败: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def count_index_docs(
        self, user_id: str, client_type: Optional[str] = None
    ) -> int:
        """返回指定索引（user_id）中的文档数量。client_type 非空时只统计该客户端。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            client = getattr(es_conn, "es_client", None)
            if not client:
                return 0

            query: Optional[dict] = (
                {"term": {"client_type": client_type}} if client_type else None
            )
            try:
                return await es_conn.count_documents(index=user_id, query=query)
            except Exception:
                try:
                    resp = client.count(index=f"{es_conn.index_prefix}_{user_id}")
                    return int(resp.get("count", 0))
                except Exception:
                    return 0

        except Exception as e:
            logger.warning(f"统计索引文档数失败: {e}")
            return 0
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def summarize_recent(
        self, user_id: str, hours: int = 24, max_messages: int = 200
    ) -> str:
        """读取最近时间范围内的聊天记录并返回拼接文本（可替换为模型摘要流程）。"""
        try:
            msgs = await self.get_recent_messages(user_id, size=max_messages)
            if not msgs:
                return ""

            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            filtered = []
            for m in msgs:
                ts = m.get("timestamp")
                if ts:
                    try:
                        t = datetime.fromisoformat(ts)
                    except Exception:
                        t = None
                else:
                    t = None
                if t is None or t >= cutoff:
                    filtered.append(m)

            parts = [f"[{m.get('role')}] {m.get('content')}" for m in reversed(filtered)]
            return "\n".join(parts)
        except Exception as e:
            logger.warning(f"摘要最近聊天记录失败: {e}")
            return ""
