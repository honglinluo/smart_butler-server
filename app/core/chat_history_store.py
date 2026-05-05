"""ChatHistoryStore - 封装对 Elasticsearch 的聊天记录操作

提供保存、查询、向量更新、向量检索以及简单的时间范围摘要等方法。
使用项目已有的连接池接口 `get_connection` / `release_connection`。每次操作会独立获取并释放 ES 连接。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import uuid

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)


class ChatHistoryStore:
    """封装聊天记录在 Elasticsearch 中的增删改查和向量接口。

    索引命名：使用 `user_id` 作为索引名（内部会加上全局 index_prefix）。
    """

    def __init__(self):
        pass

    async def _write_document(self, es_conn, user_id: str, doc_id: str, document: dict) -> bool:
        """内部方法：将文档写入 ES，失败时回退到底层 client 直接写入。"""
        try:
            success = await es_conn.create(index=user_id, doc_id=doc_id, document=document, refresh=True)
        except Exception as e:
            success = False
            logger.warning(f"es_conn.create 抛出异常: {e}")

        if success:
            return True

        # 回退：直接使用底层 client 写入
        client = getattr(es_conn, "es_client", None)
        if not client:
            return False
        try:
            if hasattr(es_conn, "_get_index_name"):
                full_index = es_conn._get_index_name(user_id)
            else:
                prefix = getattr(es_conn, "index_prefix", "") or ""
                full_index = f"{prefix}_{user_id}" if prefix else user_id

            resp = client.index(index=full_index, id=doc_id, document=document, refresh=True)
            logger.debug(f"底层 client.index 响应: {resp}")
            if resp is not None and resp.get("result") in ("created", "updated"):
                return True
        except Exception as e:
            logger.warning(f"底层 client.index 写入失败: {e}")
        return False

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
        单文档结构便于向量检索命中后直接获取完整上下文。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                logger.warning("无法获取 Elasticsearch 连接，跳过保存对话轮次")
                return ""

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
        """获取聊天消息，按 timestamp 降序分页返回。

        Args:
            user_id: 用户 ID（对应 ES 索引名）
            size:    每页条数
            from_:   分页偏移（turn 级别，非消息级别）
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []

            res = await es_conn.search(
                index=user_id,
                query={"match_all": {}},
                size=size,
                from_=from_,
                sort=[{"timestamp": {"order": "desc", "unmapped_type": "date"}}],
            )
            hits = res.get("hits", {}).get("hits", []) if res is not None else []

            _SYSTEM_TYPES = {"compression_summary", "monthly_summary", "yearly_summary"}

            turns: List[Dict[str, Any]] = []
            for h in hits:
                src = h.get("_source", h) if isinstance(h, dict) else {}
                if not isinstance(src, dict):
                    continue

                # 跳过系统内部归档记录（记忆压缩、月度/年度摘要）
                meta = src.get("metadata") or {}
                if isinstance(meta, dict) and meta.get("type") in _SYSTEM_TYPES:
                    continue
                if src.get("user_input") == "[系统摘要]":
                    continue

                doc_id = h.get("_id") if isinstance(h, dict) else None
                ts = src.get("timestamp")

                if "user_input" in src:
                    # 新格式：一个 turn 文档包含问答双方
                    turns.append({
                        "_doc_type": "turn",
                        "turn_id": src.get("turn_id", doc_id),
                        "user_input": src.get("user_input", ""),
                        "assistant_response": src.get("assistant_response", ""),
                        "timestamp": ts,
                        "_id": doc_id,
                    })
                else:
                    # 旧格式：单条 role/content 消息，包装成单轮 turn
                    role = src.get("role") or src.get("type") or "user"
                    content = src.get("content") or src.get("question") or src.get("text") or ""
                    turns.append({
                        "_doc_type": "legacy",
                        "role": role,
                        "content": content,
                        "timestamp": ts,
                        "_id": doc_id,
                    })

            # ES 已按 timestamp desc 排序，无需二次排序
            # 展开为调用方期望的 [{"role": ..., "content": ...}] 格式
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
    ) -> List[Dict[str, Any]]:
        """获取对话轮次列表，每条包含 user_input + assistant_response，按 timestamp 降序分页。

        供历史记录 API 使用；引擎内部构建 LLM 上下文请继续使用 get_recent_messages。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []

            res = await es_conn.search(
                index=user_id,
                query={"match_all": {}},
                size=size,
                from_=from_,
                sort=[{"timestamp": {"order": "desc", "unmapped_type": "date"}}],
            )
            hits = res.get("hits", {}).get("hits", []) if res is not None else []

            _SYSTEM_TYPES = {"compression_summary", "monthly_summary", "yearly_summary"}

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
                })

            return turns

        except Exception as e:
            logger.warning(f"从 ES 获取对话轮次失败: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def add_embedding(self, user_id: str, doc_id: str, embedding: List[float], vector_field: str = "embedding") -> bool:
        """向已有文档添加/更新向量字段，以便后续向量检索使用。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return False
            return await es_conn.update(index=user_id, doc_id=doc_id, document={vector_field: embedding})
        except Exception as e:
            logger.warning(f"为文档添加向量失败: {e}")
            return False
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def vector_search(self, user_id: str, vector: List[float], top_k: int = 10, filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """基于向量字段执行近邻搜索（封装底层 vector_search）。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []
            return await es_conn.vector_search(index=user_id, vector=vector, top_k=top_k, filter=filter)
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
            if not es_conn:
                return []

            # 直接使用底层 client 列出索引
            client = getattr(es_conn, "es_client", None)
            if not client:
                return []

            pattern = f"{prefix}*" if prefix else "*"
            # 使用关键字参数避免 Positional arguments 错误
            indices_dict = client.indices.get(index=pattern)
            indices = list(indices_dict.keys())
            return indices
        except Exception as e:
            logger.warning(f"列出索引失败: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def count_index_docs(self, user_id: str) -> int:
        """返回指定索引（user_id）中的文档数量。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return 0

            client = getattr(es_conn, "es_client", None)
            if not client:
                return 0

            full_index = f"{client.options.index_prefix}/{user_id}" if False else None
            # 使用封装的 count_documents 方法如果可用
            try:
                # ElasticsearchDatabase 提供了 count_documents
                return await es_conn.count_documents(index=user_id)
            except Exception:
                # 回退：直接使用底层 client
                try:
                    resp = client.count(index=f"{es_conn.index_prefix}_{user_id}")
                    return int(resp.get('count', 0))
                except Exception:
                    return 0

        except Exception as e:
            logger.warning(f"统计索引文档数失败: {e}")
            return 0
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def summarize_recent(self, user_id: str, hours: int = 24, max_messages: int = 200) -> str:
        """读取最近时间范围内的聊天记录并返回一个简单的拼接文本（可替换为模型摘要流程）。"""
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

            # 简单拼接 role + content，供后续摘要使用
            parts = [f"[{m.get('role')}] {m.get('content')}" for m in reversed(filtered)]
            return "\n".join(parts)
        except Exception as e:
            logger.warning(f"摘要最近聊天记录失败: {e}")
            return ""
