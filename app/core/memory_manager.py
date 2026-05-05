"""记忆管理模块 - 多层记忆存储、同步与检索

层级说明:
  L1  Redis      — 最近 N 轮对话列表，快速加载上下文
  L2  MySQL      — 会话引用统计，用于长期管理与清理决策
  L3  ES         — 全量会话存档 + 全文检索 + 可选向量检索

压缩触发条件（三选一）：
  count_exceeded   — 累计对话轮次达到 max_total_turns（默认 30）
  inactive_days    — 距上次对话超过 max_inactive_days 天（默认 3）
  context_overflow — 当前上下文字符数超过 context_length_limit（默认 20 000）

执行模式：
  count_exceeded / inactive_days  → 挂起（延迟 defer_seconds 秒后在后台执行）
  context_overflow                → 立即执行（取消所有挂起任务后直接触发）

多条件并发规则：
  1. 同一用户已有挂起任务时，再次触发 count/inactive 不重复创建
  2. context_overflow 发生时，取消挂起任务并立即执行（防止重复归档）
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database.pool import get_connection, release_connection
from app.core.redis_keys import (
    MEMORY_TURNS            as _KEY_TURNS,
    MEMORY_TOTAL            as _KEY_TOTAL,
    MEMORY_LOCK             as _KEY_LOCK,
    MEMORY_LAST_ACTIVITY    as _KEY_LAST_ACTIVITY,
    MEMORY_COMPRESS_PENDING as _KEY_COMPRESS_PENDING,
    MEMORY_DELEGATIONS      as _KEY_DELEGATIONS,
    MEMORY_PREFETCH_RESULT  as _KEY_PREFETCH_RESULT,
    USER_INIT               as _KEY_USER_INIT,
)

logger = logging.getLogger(__name__)


class MemoryManager:
    """多层记忆管理器。

    初始化参数 config 为 system_config.yaml 解析后的完整字典，
    即包含 system / database / agents 等顶层键。
    VectorStore 通过 set_vector_store() 注入（main.py 在两者初始化后调用）。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        sys_cfg  = config.get("system", config)
        comp_cfg = sys_cfg.get("compression", {})
        es_cfg   = config.get("database", {}).get("elasticsearch", {})

        # ── 阈值配置 ──────────────────────────────────────────
        # Redis 中保存的最近对话轮次上限（由 config 控制）
        self.redis_recent_turns: int = int(sys_cfg.get("redis_recent_turns", 10))

        # Redis 列表长度达到 recent_turns 的此比例时触发向 ES 同步
        sync_pct = float(sys_cfg.get("es_sync_threshold_pct", 0.6))
        self.es_sync_threshold: int = max(1, int(self.redis_recent_turns * sync_pct))

        # 累计轮次超过此值时触发压缩（挂起模式）
        self.max_total_turns: int = int(
            comp_cfg.get("trigger_message_count",
                         sys_cfg.get("max_recent_messages", 30))
        )
        # 距上次对话超过此天数时触发压缩（挂起模式）
        self.max_inactive_days: int = int(comp_cfg.get("max_inactive_days", 3))
        # 上下文字符数超过此值时立即触发压缩
        self.context_length_limit: int = int(comp_cfg.get("context_length_limit", 20_000))
        # 挂起任务的延迟秒数（等服务器空闲后执行）
        self._compress_defer_seconds: int = int(comp_cfg.get("defer_seconds", 60))

        # ── 挂起任务追踪（内存级，服务重启后失效）───────────────
        # user_id → asyncio.Task；仅用于取消挂起任务
        self._compress_tasks: Dict[str, "asyncio.Task[None]"] = {}

        # ── 向量检索配置 ───────────────────────────────────────
        # 仅当配置文件中存在 elasticsearch.vector_field 时启用向量检索
        self.vector_field: Optional[str] = es_cfg.get("vector_field") or None
        self.embedding_model_name: str = sys_cfg.get("embedding_model", "bge-large-zh-v1.5")
        self._embedder = None  # sentence-transformers 实例（懒加载）

        # ── 检索结果上限 ───────────────────────────────────────
        self.retrieval_top_k: int = 3  # 对外始终返回最多 3 条

        # ── 懒注入依赖 ────────────────────────────────────────
        self._vector_store = None   # 由 set_vector_store() 注入
        self._default_llm  = None   # 由 set_default_llm() 注入
        self._archiver     = None   # 由 set_archiver() 注入（MemoryArchiverAgent）

    def set_vector_store(self, vector_store) -> None:
        """注入 VectorStore（main.py 在两者初始化后调用）。

        VectorStore 接管向量检索后，停用 MemoryManager 内置的 sentence-transformers 路径，
        避免重复调用和 ModuleNotFoundError。
        """
        self._vector_store = vector_store
        # 禁用旧的 sentence-transformers 向量检索路径（VectorStore 负责向量部分）
        self.vector_field = None
        logger.info("VectorStore 已注入 MemoryManager，内置向量检索路径已停用")

    def set_default_llm(self, llm) -> None:
        """注入默认 LLM，供归档任务使用。"""
        self._default_llm = llm

    def set_archiver(self, archiver) -> None:
        """注入 MemoryArchiverAgent（由 HermesEngine 在初始化时调用）。"""
        self._archiver = archiver
        logger.info("MemoryArchiverAgent 已注入 MemoryManager")

    # ══════════════════════════════════════════════════════════
    # 对外接口
    # ══════════════════════════════════════════════════════════

    async def store_turn(
        self,
        user_id: str,
        turn_id: str,
        user_input: str,
        assistant_response: str,
        metadata: Optional[Dict[str, Any]] = None,
        agent_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """存储一轮完整对话。

        流程：
          1. 写入 Redis 最近对话列表，保持不超过 redis_recent_turns 条
          2. 累计计数器 +1，判断是否触发 ES 同步
          3. 累计总量超过阈值时触发压缩任务
          4. 在 MySQL 初始化引用统计记录
        """
        turn: Dict[str, Any] = {
            "turn_id": turn_id,
            "user_input": user_input,
            "assistant_response": assistant_response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(metadata or {}),
        }

        await self._redis_push_turn(user_id, turn)

        total     = await self._redis_increment_total(user_id)
        redis_len = await self._redis_recent_length(user_id)

        # Redis 列表长度达到阈值 → 同步到 ES（后台，不阻塞响应）
        if redis_len >= self.es_sync_threshold:
            asyncio.create_task(self._sync_recent_to_es(user_id))

        # 时间触发检查：读取上次活跃时间并更新为现在
        # 必须在 count 检查前完成，避免阻塞
        time_triggered = await self._check_and_update_last_activity(user_id)

        # 压缩触发优先级：count_exceeded > inactive_days
        # 两者均为挂起模式，context_overflow 由 HermesEngine 检测后调 compress_immediately()
        if total >= self.max_total_turns:
            asyncio.create_task(self._schedule_compress(user_id, "count_exceeded"))
        elif time_triggered:
            asyncio.create_task(self._schedule_compress(user_id, "inactive_days"))

        # MySQL 初始化引用记录（后台）
        asyncio.create_task(self._mysql_init_ref(user_id, turn_id))

        # 向量化：切片 → embedding → 写入独立向量索引（后台）
        if self._vector_store is not None:
            asyncio.create_task(
                self._vector_store.store_turn_vectors(
                    user_id, turn_id, user_input, assistant_response,
                    agent_outputs=agent_outputs,
                )
            )


    async def retrieve_memory(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """检索与 query 最相关的历史对话，最多返回 top_k（≤3）条。

        检索策略：
          1. 向量检索（仅在 config 中配置了 vector_field 时执行）
          2. ES 全文检索（multi_match user_input + assistant_response）
          3. 去重合并，按得分取前 top_k 条
          4. 异步记录每条结果的引用计数
        """
        top_k = min(top_k, self.retrieval_top_k)
        results:  List[Dict[str, Any]] = []
        seen_ids: set = set()

        # ── 向量检索 ──────────────────────────────────────────
        if self.vector_field:
            for hit in await self._vector_search(user_id, query, top_k):
                tid = hit.get("turn_id") or hit.get("_id", "")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    results.append(hit)

        # ── 全文检索（补足剩余名额）───────────────────────────
        if len(results) < top_k:
            for hit in await self._es_text_search(user_id, query, (top_k - len(results)) * 2):
                tid = hit.get("turn_id") or hit.get("_id", "")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    results.append(hit)
                    if len(results) >= top_k:
                        break

        results = results[:top_k]

        # 异步记录引用
        for hit in results:
            tid = hit.get("turn_id") or hit.get("_id")
            if tid:
                asyncio.create_task(self._mysql_increment_ref(user_id, tid))

        return results

    async def get_recent_turns(self, user_id: str) -> List[Dict[str, Any]]:
        """从 Redis 加载最近 N 轮对话，供上下文拼接使用（按时间升序返回）。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return []
            key   = _KEY_TURNS.format(user_id=user_id)
            turns = await redis_conn.read_list(key, 0, self.redis_recent_turns - 1)
            # LPUSH 使最新在前，reverse 后得到时间升序（旧→新）
            return list(reversed(turns)) if turns else []
        except Exception as e:
            logger.warning(f"读取 Redis 最近对话失败 user={user_id}: {e}")
            return []
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def record_reference(self, user_id: str, turn_id: str) -> None:
        """手动记录一次会话被引用（retrieve_memory 已自动调用，无需重复）。"""
        await self._mysql_increment_ref(user_id, turn_id)

    async def flush_turns_to_es(self, user_id: str) -> None:
        """强制将 Redis 中所有未同步对话写入 ES。

        用户最后一个 session 退出登录时调用，确保对话数据不丢失。
        """
        await self._sync_recent_to_es(user_id)

    # ══════════════════════════════════════════════════════════
    # Redis 操作
    # ══════════════════════════════════════════════════════════

    async def _redis_push_turn(self, user_id: str, turn: Dict[str, Any]) -> None:
        """LPUSH 新轮次，LTRIM 保持列表不超过 redis_recent_turns 条。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return
            key = _KEY_TURNS.format(user_id=user_id)
            await redis_conn.push_to_list(key, turn, ttl=3600 * 24 * 30)
            # LTRIM 保留最新的 N 条（index 0 = 最新）
            client = getattr(redis_conn, "redis_client", None)
            if client:
                client.ltrim(key, 0, self.redis_recent_turns - 1)
        except Exception as e:
            logger.warning(f"Redis push_turn 失败 user={user_id}: {e}")
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _redis_increment_total(self, user_id: str) -> int:
        """累计总轮次计数器 +1，返回当前值。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return 0
            count = await redis_conn.increment(_KEY_TOTAL.format(user_id=user_id))
            return count or 0
        except Exception as e:
            logger.warning(f"Redis increment 失败 user={user_id}: {e}")
            return 0
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _redis_recent_length(self, user_id: str) -> int:
        """返回 Redis 最近对话列表的当前长度。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return 0
            return await redis_conn.get_list_length(_KEY_TURNS.format(user_id=user_id)) or 0
        except Exception as e:
            logger.warning(f"Redis list length 失败 user={user_id}: {e}")
            return 0
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    # ══════════════════════════════════════════════════════════
    # ES 同步
    # ══════════════════════════════════════════════════════════

    async def _sync_recent_to_es(self, user_id: str) -> None:
        """将 Redis 中的最近对话全量同步到 ES（去重写入，使用 Redis 锁防并发）。"""
        # 获取同步锁（TTL=30s，防止并发重复同步）
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return
            lock_key = _KEY_LOCK.format(user_id=user_id)
            locked = await redis_conn.create(lock_key, "1", ttl=30)
            if not locked:
                return  # 另一个协程正在同步
        except Exception:
            return
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

        try:
            turns = await self.get_recent_turns(user_id)
            if not turns:
                return

            es_conn = None
            try:
                es_conn = await get_connection("elasticsearch", None)
                if not es_conn:
                    return
                synced = 0
                for turn in turns:
                    turn_id = turn.get("turn_id")
                    if not turn_id:
                        continue
                    # 已存在则跳过，避免覆盖
                    try:
                        existing = await es_conn.read(index=user_id, doc_id=turn_id)
                        if existing:
                            continue
                    except Exception:
                        pass
                    try:
                        await es_conn.create(
                            index=user_id,
                            doc_id=turn_id,
                            document=turn,
                            refresh=False,
                        )
                        synced += 1
                    except Exception as e:
                        logger.warning(f"ES 写入单条失败 turn={turn_id}: {e}")
                logger.info(f"ES sync 完成 user={user_id}, synced={synced}/{len(turns)}")
            except Exception as e:
                logger.warning(f"ES sync 失败 user={user_id}: {e}")
            finally:
                if es_conn:
                    await release_connection("elasticsearch", es_conn)
        finally:
            # 释放锁
            redis_conn = None
            try:
                redis_conn = await get_connection("redis", None)
                if redis_conn:
                    await redis_conn.delete(_KEY_LOCK.format(user_id=user_id))
            except Exception:
                pass
            finally:
                if redis_conn:
                    await release_connection("redis", redis_conn)

    # ══════════════════════════════════════════════════════════
    # 归档触发（委托给 MemoryArchiverAgent）
    # ══════════════════════════════════════════════════════════

    async def _trigger_archiver(self, user_id: str, current_count: int) -> None:
        """归档触发器：将控制权交给 MemoryArchiverAgent。"""
        logger.info(
            f"[MemoryManager] 触发记忆归档 user={user_id} "
            f"total={current_count} threshold={self.max_total_turns}"
        )
        if self._archiver is None:
            logger.warning("[MemoryManager] MemoryArchiverAgent 未注入，跳过归档")
            return
        if self._default_llm is None:
            logger.warning("[MemoryManager] 未配置默认 LLM，跳过归档")
            return
        asyncio.create_task(
            self._archiver.run(
                user_id,
                self._default_llm,
                self,
                self._vector_store,
            )
        )

    # ══════════════════════════════════════════════════════════
    # 压缩调度：挂起 / 立即执行
    # ══════════════════════════════════════════════════════════

    async def compress_immediately(self, user_id: str, reason: str = "context_overflow") -> None:
        """立即执行记忆压缩（context_overflow 专用路径）。

        取消该用户所有挂起的延迟压缩任务，清除 Redis 挂起标志，
        随即调用 _trigger_archiver 将归档任务派发到后台。
        """
        # 取消内存中的延迟任务
        task = self._compress_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
            logger.info(f"[MemoryManager] 已取消挂起压缩任务 user={user_id}")

        # 清除 Redis 挂起标志
        await self._clear_compress_pending(user_id)

        # 立即派发归档任务（_trigger_archiver 本身是非阻塞的）
        total = await self._redis_get_total(user_id)
        await self._trigger_archiver(user_id, total)
        logger.info(f"[MemoryManager] 立即触发记忆压缩 user={user_id} reason={reason}")

    async def _schedule_compress(self, user_id: str, reason: str) -> None:
        """挂起压缩任务，等服务器空闲后执行。

        若该用户已有挂起任务（Redis 标志存在），直接返回不重复创建。
        """
        # 幂等检查：已有挂起任务时跳过
        existing = await self._get_compress_pending(user_id)
        if existing:
            logger.debug(
                f"[MemoryManager] 已有挂起压缩任务，跳过 user={user_id} "
                f"existing_reason={existing.get('reason')}"
            )
            return

        # 设置 Redis 挂起标志
        await self._set_compress_pending(user_id, reason)

        # 取消旧任务（理论上此时不应存在，防御性处理）
        old_task = self._compress_tasks.get(user_id)
        if old_task and not old_task.done():
            old_task.cancel()

        # 创建延迟后台任务
        task = asyncio.create_task(
            self._deferred_compress_worker(user_id, reason, self._compress_defer_seconds)
        )
        self._compress_tasks[user_id] = task
        logger.info(
            f"[MemoryManager] 压缩任务已挂起 user={user_id} "
            f"reason={reason} delay={self._compress_defer_seconds}s"
        )

    async def _deferred_compress_worker(
        self, user_id: str, reason: str, delay: int
    ) -> None:
        """延迟压缩协程：睡眠 delay 秒后检查挂起标志，若有效则执行归档。"""
        try:
            await asyncio.sleep(delay)

            # 二次检查：context_overflow 可能在睡眠期间已经清除了挂起标志
            pending = await self._get_compress_pending(user_id)
            if not pending:
                logger.debug(f"[MemoryManager] 挂起标志已清除，放弃延迟压缩 user={user_id}")
                return

            # 清除标志并执行
            await self._clear_compress_pending(user_id)
            self._compress_tasks.pop(user_id, None)

            total = await self._redis_get_total(user_id)
            await self._trigger_archiver(user_id, total)
            logger.info(
                f"[MemoryManager] 延迟压缩任务执行完毕 user={user_id} reason={reason}"
            )
        except asyncio.CancelledError:
            logger.debug(f"[MemoryManager] 延迟压缩任务被取消 user={user_id}")
        except Exception as e:
            logger.warning(f"[MemoryManager] 延迟压缩任务异常 user={user_id}: {e}")

    # ══════════════════════════════════════════════════════════
    # 活跃时间 + 挂起标志 辅助方法
    # ══════════════════════════════════════════════════════════

    async def _check_and_update_last_activity(self, user_id: str) -> bool:
        """读取并更新 last_activity，返回是否触发时间条件（≥ max_inactive_days 天）。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return False
            key     = _KEY_LAST_ACTIVITY.format(user_id=user_id)
            last_ts = await redis_conn.read(key)
            triggered = False
            if last_ts:
                try:
                    ts_str  = last_ts if isinstance(last_ts, str) else str(last_ts)
                    last_dt = datetime.fromisoformat(ts_str)
                    delta   = datetime.now(timezone.utc) - last_dt
                    triggered = delta.days >= self.max_inactive_days
                except Exception:
                    pass
            # 更新为当前时间（TTL 90 天，保证长期不活跃用户的记录不丢失）
            now_str = datetime.now(timezone.utc).isoformat()
            await redis_conn.create(key, now_str, ttl=3600 * 24 * 90)
            return triggered
        except Exception as e:
            logger.debug(f"[MemoryManager] 活跃时间检查失败 user={user_id}: {e}")
            return False
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _set_compress_pending(self, user_id: str, reason: str) -> None:
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if redis_conn:
                data = {
                    "reason":       reason,
                    "scheduled_at": datetime.now(timezone.utc).isoformat(),
                }
                await redis_conn.create(
                    _KEY_COMPRESS_PENDING.format(user_id=user_id),
                    data,
                    ttl=3600 * 24 * 7,
                )
        except Exception as e:
            logger.debug(f"[MemoryManager] 设置挂起标志失败 user={user_id}: {e}")
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _get_compress_pending(self, user_id: str) -> Optional[Dict[str, Any]]:
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return None
            raw = await redis_conn.read(_KEY_COMPRESS_PENDING.format(user_id=user_id))
            if not raw:
                return None
            if isinstance(raw, dict):
                return raw
            return json.loads(raw)
        except Exception:
            return None
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _clear_compress_pending(self, user_id: str) -> None:
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if redis_conn:
                await redis_conn.delete(_KEY_COMPRESS_PENDING.format(user_id=user_id))
        except Exception:
            pass
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _redis_get_total(self, user_id: str) -> int:
        """读取用户累计轮次计数（只读，不递增）。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return 0
            val = await redis_conn.read(_KEY_TOTAL.format(user_id=user_id))
            return int(val) if val else 0
        except Exception:
            return 0
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    # ══════════════════════════════════════════════════════════
    # ES 检索
    # ══════════════════════════════════════════════════════════

    async def _es_text_search(
        self, user_id: str, query: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """ES multi_match 全文检索，同时搜索 user_input 和 assistant_response 字段。

        请求 top_k * 3 个候选并在返回前按 turn_id 折叠，与向量检索保持一致。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []
            raw = await es_conn.search(
                index=user_id,
                query={
                    "multi_match": {
                        "query": query,
                        "fields": ["user_input", "assistant_response"],
                        "type": "best_fields",
                    }
                },
                size=top_k * 3,
            )
            hits = raw.get("hits", {}).get("hits", []) if raw is not None else []
            results = [self._hit_to_result(h, source="es_text") for h in hits]
            return self._merge_by_turn(results)
        except Exception as e:
            logger.warning(f"ES 文本检索失败 user={user_id}: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _vector_search(
        self, user_id: str, query: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """生成 query 向量并执行 KNN 检索。向量模型不可用时返回空列表。

        向 ES 请求 top_k * 3 个候选，合并同一 turn 的多个 chunk 命中后
        按最高分排序返回，确保调用方总能拿到 top_k 个不重复 turn。
        """
        embedding = await self._get_embedding(query)
        if not embedding:
            return []
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []
            raw_hits = await es_conn.vector_search(
                index=user_id,
                vector=embedding,
                top_k=top_k * 3,
                vector_field=self.vector_field,
            )
            hits = [
                self._hit_to_result(h, source="vector")
                for h in (raw_hits or [])
                if isinstance(h, dict)
            ]
            return self._merge_by_turn(hits)
        except Exception as e:
            logger.warning(f"向量检索失败 user={user_id}: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """懒加载 sentence-transformers 并生成文本向量。

        若 vector_field 未配置或模型加载失败，返回 None 以跳过向量检索。
        """
        if not self.vector_field:
            return None
        try:
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer  # type: ignore
                loop = asyncio.get_event_loop()
                self._embedder = await loop.run_in_executor(
                    None, lambda: SentenceTransformer(self.embedding_model_name)
                )
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: self._embedder.encode(text).tolist()
            )
        except Exception as e:
            logger.warning(f"生成向量失败（向量检索将跳过）: {e}")
            self.vector_field = None  # 本次启动内不再重试
            return None

    @staticmethod
    def _hit_to_result(hit: Dict[str, Any], source: str) -> Dict[str, Any]:
        """将 ES hit 统一转换为标准 result 格式。"""
        src = hit.get("_source", hit)
        return {
            "turn_id": src.get("turn_id") or hit.get("_id", ""),
            "user_input": src.get("user_input", ""),
            "assistant_response": src.get("assistant_response", ""),
            "timestamp": src.get("timestamp"),
            "intent": src.get("intent"),
            "_score": hit.get("_score"),
            "_source": source,
        }

    @staticmethod
    def _merge_by_turn(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将同一 turn_id 的多个 chunk 命中合并为单条，按最高分降序返回。

        合并规则：
        - _score：取所有 chunk 中的最高值
        - user_input / assistant_response：内容相同则直接复用；
          不同（chunk 场景）则换行拼接，保留完整上下文
        """
        merged: Dict[str, Dict[str, Any]] = {}
        for hit in hits:
            tid = hit.get("turn_id") or ""
            if not tid:
                continue
            if tid not in merged:
                merged[tid] = hit.copy()
                continue

            existing = merged[tid]
            # 保留更高分
            if (hit.get("_score") or 0) > (existing.get("_score") or 0):
                existing["_score"] = hit["_score"]
            # chunk 内容与已有内容不同时拼接
            for field in ("user_input", "assistant_response"):
                old = (existing.get(field) or "").strip()
                new = (hit.get(field) or "").strip()
                if new and new not in old:
                    existing[field] = f"{old}\n{new}".strip() if old else new

        return sorted(merged.values(), key=lambda x: x.get("_score") or 0, reverse=True)

    # ══════════════════════════════════════════════════════════
    # MySQL 引用统计
    # ══════════════════════════════════════════════════════════

    async def _mysql_init_ref(self, user_id: str, turn_id: str) -> None:
        """在 memory_references 表中为新 turn 创建初始记录（ref_count=0）。"""
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", "agent_db")
            if not mysql_conn:
                return
            await mysql_conn.execute_raw(
                """
                INSERT IGNORE INTO memory_references (turn_id, user_id, ref_count)
                VALUES (:turn_id, :user_id, 0)
                """,
                {"turn_id": turn_id, "user_id": user_id},
            )
        except Exception as e:
            logger.warning(f"MySQL init_ref 失败 user={user_id} turn={turn_id}: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

    async def _mysql_increment_ref(self, user_id: str, turn_id: str) -> None:
        """引用计数 +1，若记录不存在则插入（ref_count=1）。"""
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", "agent_db")
            if not mysql_conn:
                return
            await mysql_conn.execute_raw(
                """
                INSERT INTO memory_references (turn_id, user_id, ref_count)
                VALUES (:turn_id, :user_id, 1)
                ON DUPLICATE KEY UPDATE
                    ref_count  = ref_count + 1,
                    last_ref_at = NOW()
                """,
                {"turn_id": turn_id, "user_id": user_id},
            )
        except Exception as e:
            logger.warning(f"MySQL increment_ref 失败 user={user_id} turn={turn_id}: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

    # ══════════════════════════════════════════════════════════
    # on_delegation — 多智能体委派记录（参考 hermes-agent）
    # ══════════════════════════════════════════════════════════

    async def on_delegation(
        self,
        user_id:    str,
        agent_name: str,
        task_desc:  str,
        result:     str,
    ) -> None:
        """记录子智能体执行结果到 Redis 委派队列。

        在串行/并行 pipeline 中，每个 Agent 完成后调用此方法，
        使记忆系统能观察到整个多智能体协作的中间过程。
        记录格式：{agent_name, task_desc, result_preview, timestamp}，
        最多保留 50 条，TTL 7 天。
        """
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return
            record = {
                "agent_name":    agent_name,
                "task_desc":     task_desc[:300],
                "result_preview": result[:500],
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            }
            key    = _KEY_DELEGATIONS.format(user_id=user_id)
            client = getattr(redis_conn, "redis_client", None)
            if client:
                client.lpush(key, json.dumps(record, ensure_ascii=False))
                client.ltrim(key, 0, 49)          # 最多 50 条
                client.expire(key, 3600 * 24 * 7)  # TTL 7 天
                logger.debug(
                    "[MemoryManager] on_delegation user=%s agent=%s task=%r",
                    user_id, agent_name, task_desc[:80],
                )
        except Exception as e:
            logger.debug(f"[MemoryManager] on_delegation 失败 user={user_id}: {e}")
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def get_delegation_history(
        self, user_id: str, max_count: int = 10
    ) -> List[Dict[str, Any]]:
        """读取最近 max_count 条委派记录（调试/检索用）。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return []
            key    = _KEY_DELEGATIONS.format(user_id=user_id)
            client = getattr(redis_conn, "redis_client", None)
            if not client:
                return []
            raw_list = client.lrange(key, 0, max_count - 1)
            result   = []
            for raw in raw_list:
                try:
                    result.append(json.loads(raw))
                except Exception:
                    pass
            return result
        except Exception as e:
            logger.debug(f"[MemoryManager] get_delegation_history 失败: {e}")
            return []
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    # ══════════════════════════════════════════════════════════
    # on_pre_compress — 压缩前关键信息提取（参考 hermes-agent）
    # ══════════════════════════════════════════════════════════

    async def on_pre_compress(self, turns: List[Dict[str, Any]]) -> str:
        """压缩前从即将被归档的轮次中提取关键事实。

        调用方（MemoryArchiverAgent）在生成摘要之前调用本方法，
        将返回文本追加到摘要 Prompt 中，确保即使轮次被压缩也不丢失关键信息。

        提取维度：
          - 用户明确提及的偏好、决策、承诺
          - 重要的实体（人物、项目、产品名称等）
          - 需要在后续对话中持续记住的约束或规则

        LLM 不可用时降级为基于规则的关键词提取，返回最多 10 条摘要句。
        """
        if not turns:
            return ""

        if self._default_llm is not None:
            return await self._llm_extract_key_facts(turns)

        # 降级：规则提取（无 LLM 时兜底）
        return self._rule_extract_key_facts(turns)

    async def _llm_extract_key_facts(self, turns: List[Dict[str, Any]]) -> str:
        """用 LLM 从轮次列表中提取关键事实，返回 bullet-point 文本。"""
        sample  = turns[:15]  # 最多取 15 轮，避免 prompt 过长
        dialog  = "\n".join(
            f"用户: {t.get('user_input', '')[:200]}\n"
            f"回答: {t.get('assistant_response', '')[:300]}"
            for t in sample
        )
        prompt  = (
            "以下是一段即将被压缩归档的对话历史。\n"
            "请提取其中最重要的关键信息（用户偏好、决策、重要实体、持续约束等），"
            "每条用「- 」开头，最多 10 条，每条不超过 50 字。\n\n"
            f"{dialog}"
        )
        try:
            from langchain_core.messages import HumanMessage
            resp = await self._default_llm.ainvoke([HumanMessage(content=prompt)])
            content = getattr(resp, "content", str(resp))
            logger.debug("[MemoryManager] on_pre_compress LLM 提取完成，长度=%d", len(content))
            return content.strip()
        except Exception as e:
            logger.warning("[MemoryManager] on_pre_compress LLM 调用失败: %s，降级", e)
            return self._rule_extract_key_facts(turns)

    @staticmethod
    def _rule_extract_key_facts(turns: List[Dict[str, Any]]) -> str:
        """基于规则的降级提取：截取每轮 user_input 前 80 字作为关键摘要句。"""
        lines = []
        for t in turns[:10]:
            q = (t.get("user_input") or "").strip()
            if q:
                lines.append(f"- {q[:80]}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    # Background prefetch — 下轮记忆预取（参考 hermes-agent queue_prefetch）
    # ══════════════════════════════════════════════════════════

    def queue_prefetch(self, user_id: str, query: str) -> None:
        """在后台异步预取本轮查询对应的记忆，供下一轮 build_context() 使用。

        fire-and-forget：不 await，直接创建异步任务。
        """
        asyncio.create_task(self._run_prefetch(user_id, query))

    async def _run_prefetch(self, user_id: str, query: str) -> None:
        """执行预取并将结果缓存到 Redis（TTL 5 分钟）。"""
        try:
            results = await self.retrieve_memory(user_id, query, top_k=self.retrieval_top_k)
            if not results:
                # ES 尚未同步或为空时，用 Redis 近期对话兜底
                recent = await self.get_recent_turns(user_id)
                if not recent:
                    return
                results = recent[-self.retrieval_top_k:]
            redis_conn = None
            try:
                redis_conn = await get_connection("redis", None)
                if not redis_conn:
                    return
                key    = _KEY_PREFETCH_RESULT.format(user_id=user_id)
                client = getattr(redis_conn, "redis_client", None)
                if client:
                    client.set(key, json.dumps(results, ensure_ascii=False), ex=300)
                    logger.debug(
                        "[MemoryManager] 预取完成 user=%s 命中=%d", user_id, len(results)
                    )
            except Exception as e:
                logger.debug(f"[MemoryManager] 预取结果写入 Redis 失败: {e}")
            finally:
                if redis_conn:
                    await release_connection("redis", redis_conn)
        except Exception as e:
            logger.debug(f"[MemoryManager] _run_prefetch 失败 user={user_id}: {e}")

    async def get_prefetched_context(self, user_id: str) -> List[Dict[str, Any]]:
        """读取并消费上一轮预取的记忆结果（读后删除，避免陈旧）。

        返回空列表时表示预取未命中或已过期，调用方应执行正常检索。
        """
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return []
            key    = _KEY_PREFETCH_RESULT.format(user_id=user_id)
            client = getattr(redis_conn, "redis_client", None)
            if not client:
                return []
            raw = client.getdel(key)   # 原子读取并删除
            if not raw:
                return []
            return json.loads(raw)
        except Exception as e:
            logger.debug(f"[MemoryManager] get_prefetched_context 失败 user={user_id}: {e}")
            return []
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    # ══════════════════════════════════════════════════════════
    # build_system_prompt_block — 用户画像注入系统提示
    # ══════════════════════════════════════════════════════════

    async def build_system_prompt_block(self, user_id: str) -> str:
        """加载用户画像，格式化为可注入 LLM 系统提示的文本块。

        数据来源：Redis user:{user_id}:init（字段 profile）。
        画像包含 preferences / personal_info / work_content 三个维度。
        无画像或画像为空时返回空字符串。
        """
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if not redis_conn:
                return ""
            raw = await redis_conn.read(_KEY_USER_INIT.format(user_id=user_id))
            if not raw:
                return ""
            data    = raw if isinstance(raw, dict) else json.loads(raw)
            profile = data.get("profile") or {}
            if not profile:
                return ""

            lines = ["【用户画像参考】"]
            prefs = profile.get("preferences") or []
            pinfo = profile.get("personal_info") or []
            work  = profile.get("work_content") or []

            if prefs:
                lines.append("偏好: " + "；".join(str(p) for p in prefs[:5]))
            if pinfo:
                lines.append("个人信息: " + "；".join(str(p) for p in pinfo[:5]))
            if work:
                lines.append("工作背景: " + "；".join(str(p) for p in work[:5]))

            if len(lines) == 1:
                return ""

            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"[MemoryManager] build_system_prompt_block 失败 user={user_id}: {e}")
            return ""
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)


