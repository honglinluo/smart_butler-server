"""
【模块说明】记忆压缩 Agent（MemoryArchiver）— 把"太多对话"压缩成精华摘要

每次对话都会存入记忆，随着时间推移，记忆越积越多会占用大量空间，而且会影响 AI 搜索速度。
这个 Agent 负责在必要时把多轮对话"压缩"：把 N 条对话记录合并成一条摘要记录。

【工作流程（Saga 模式，确保数据安全）】
  1. 从 Redis 读取当前对话轮次数据
  2. 把 Redis 中的数据先保存到 Elasticsearch（确保有备份，之后出错也能恢复）
  3. 调用 SummarizerAgent 生成摘要内容
  4. 把摘要写入 Elasticsearch（达到检查点后，才允许删除原始数据）
  5. 清空 Redis 旧轮次，写入摘要，重置轮次计数器
  6. 删除 Elasticsearch 中的旧原始数据
  7. 后台：更新用户画像、向量化摘要、记录 MySQL 统计信息

【为什么用 Saga 模式？】
  数据跨多个存储系统（Redis / Elasticsearch / MySQL），任何一步出错都要能安全回滚。
  Saga 模式通过"先写后删"和"检查点确认"保证数据一致性，不会因为中途失败而丢数据。

内置记忆归档智能体 — 仅供 Hermes 引擎调用，不注册到 AgentRegistry。

步骤顺序（Saga 模式，先写后删）：
  1. 从 Redis 读取当前轮次
  2. 将 Redis 轮次强制持久化到 ES（确保原始数据可恢复），同时记录 turn_id 列表
  3. 调用 SummarizerAgent 生成对话摘要
  4. 摘要 turn 写入 ES（关键 Saga 检查点：确认写入后才清 Redis）
  5. 清空 Redis 旧轮次，写入摘要 turn，重置计数器
  6. 删除 ES 中的原始旧轮次（chat 索引 + vector 索引），即 ES 删除旧数据
  7. （后台）提取用户画像并写入 MySQL + Redis
  8. （后台）向量化摘要 turn
  9. （后台）MySQL 初始化摘要引用计数

Saga 数据一致性保证：
  - 归档开始时在 memory_compress_jobs 创建任务记录（job_id / status / 各步数据）
  - Step 4（ES 写入）成功确认后才执行 Step 5（清 Redis），防止数据丢失
  - Step 6（删除旧数据）在 Redis 替换后执行，此时摘要已安全写入，可幂等重试
  - compressed_turn_ids 字段记录原始 turn_id 列表，中断后可从该列表恢复删除
  - 任何关键步骤失败：更新 status=failed，原始数据在 ES 中完整保留
  - resume_failed_jobs() 在服务启动时扫描并从中断点续跑

所需数据库表：
  CREATE TABLE memory_compress_jobs (
    job_id              VARCHAR(32)  PRIMARY KEY,
    user_id             VARCHAR(36)  NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'pending'
                        COMMENT 'pending|flushing|summarizing|writing_summary|replacing_redis|deleting_es|done|failed',
    reason              VARCHAR(50)  COMMENT '触发原因',
    compressed_turns    INT          DEFAULT 0,
    compressed_turn_ids TEXT         COMMENT 'JSON 数组，被压缩的 turn_id 列表（用于删除恢复）',
    summary_turn_id     VARCHAR(32),
    summary_text        TEXT,
    error_info          TEXT,
    started_at          DATETIME     NOT NULL DEFAULT NOW(),
    updated_at          DATETIME     NOT NULL DEFAULT NOW() ON UPDATE NOW(),
    INDEX idx_user_status (user_id, status),
    INDEX idx_updated     (updated_at)
  );
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.database.pool import get_connection, release_connection
from app.database.redis_keys import MEMORY_TURNS as _KEY_TURNS, MEMORY_TOTAL as _KEY_TOTAL, USER_INIT, INIT_TTL
from app.utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

# ── 提示词模板 ──────────────────────────────────────────────────
_TEMPLATE_DIR  = PROJECT_ROOT / "config" / "templates"
_TAG_TEMPLATE  = _TEMPLATE_DIR / "archiver_extract_tags.txt"


def _load_tag_prompt() -> str:
    """从模板文件加载画像提取提示词，文件缺失时使用内置兜底文本。"""
    try:
        return _TAG_TEMPLATE.read_text(encoding="utf-8").strip()
    except Exception:
        return (
            "请从以下对话中提取用户的关键画像信息，以纯 JSON 格式输出。\n"
            '{"preferences": ["..."], "personal_info": ["..."], "work_content": ["..."]}'
        )


class MemoryArchiverAgent:
    """记忆归档智能体（系统内置，不注册到 AgentRegistry）。

    由 HermesEngine 在 set_memory_manager() 中实例化，并通过
    MemoryManager.set_archiver() 注入。MemoryManager 在满足压缩条件时
    调用 run()，整个流程对路由层和用户完全不可见。
    """

    name = "memory_archiver"

    # 任务状态常量
    _S_PENDING         = "pending"
    _S_FLUSHING        = "flushing"
    _S_SUMMARIZING     = "summarizing"
    _S_WRITING_SUMMARY = "writing_summary"
    _S_REPLACING_REDIS = "replacing_redis"
    _S_DELETING_ES     = "deleting_es"
    _S_DONE            = "done"
    _S_FAILED          = "failed"

    async def run(
        self,
        user_id:        str,
        llm,
        memory_manager,
        vector_store=None,
        reason:         str = "",
    ) -> Dict[str, Any]:
        """执行记忆归档全流程（Saga 模式，先写 ES 再清 Redis）。"""
        logger.info(f"[MemoryArchiver] 开始归档 user={user_id} reason={reason}")

        if llm is None:
            logger.warning("[MemoryArchiver] 未提供 LLM，跳过归档")
            return {"status": "skipped", "reason": "no_llm"}

        job_id = uuid.uuid4().hex
        await self._create_job(job_id, user_id, reason)

        try:
            # ── Step 1: 读取当前 Redis 轮次 ──────────────────────
            turns = await memory_manager.get_recent_turns(user_id)
            if not turns:
                await self._update_job(job_id, self._S_DONE, error_info="no_turns")
                return {"status": "skipped", "reason": "no_turns"}

            # ── Step 2: 压缩前持久化（原始数据 → ES）────────────
            turn_ids = [t["turn_id"] for t in turns if t.get("turn_id")]
            await self._update_job(
                job_id, self._S_FLUSHING,
                compressed_turns=len(turns),
                compressed_turn_ids=json.dumps(turn_ids),
            )
            await self._flush_turns_to_es(user_id, turns)

            # ── Step 3: 生成压缩摘要（固定结构，不保留上下文）──────
            await self._update_job(job_id, self._S_SUMMARIZING)
            pre_facts    = await memory_manager.on_pre_compress(turns)
            summary_text = await self._compress(turns, llm, pre_facts)
            if not summary_text:
                await self._update_job(job_id, self._S_FAILED, error_info="compress_failed")
                return {"status": "error", "reason": "compress_failed"}

            # 计算被压缩轮次的时间范围，写入 metadata 方便后续月度/年度归档查询
            ts_list = [
                t.get("timestamp", "") for t in turns if t.get("timestamp")
            ]
            ts_list.sort()
            summary_turn_id = uuid.uuid4().hex
            summary_turn: Dict[str, Any] = {
                "turn_id":            summary_turn_id,
                "user_input":         "[系统摘要]",
                "assistant_response": summary_text,
                "metadata": {
                    "type":             "compression_summary",
                    "compressed_turns": len(turns),
                    "time_start":       ts_list[0]  if ts_list else "",
                    "time_end":         ts_list[-1] if ts_list else "",
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            # ── Step 4: 摘要写入 ES（Saga 关键检查点）───────────
            # 只有确认写入成功后才清 Redis，防止数据丢失
            await self._update_job(
                job_id, self._S_WRITING_SUMMARY,
                summary_turn_id=summary_turn_id,
                summary_text=summary_text,
            )
            if not await self._write_es_turn(user_id, summary_turn):
                await self._update_job(job_id, self._S_FAILED, error_info="es_write_failed")
                # 补偿：原始轮次已在 ES（Step 2），Redis 未修改，无需额外操作
                logger.error(
                    f"[MemoryArchiver] 摘要写入 ES 失败，归档中止，原始数据已保留 user={user_id}"
                )
                return {"status": "error", "reason": "es_write_failed"}

            # ── Step 5: 清空 Redis，写入摘要（ES 已确认，操作安全）
            await self._update_job(job_id, self._S_REPLACING_REDIS)
            await self._replace_redis(user_id, summary_turn)

            # ── Step 6: 删除 ES 中的原始旧轮次（摘要已安全，可幂等重试）
            await self._update_job(job_id, self._S_DELETING_ES)
            deleted_ok = await self._delete_old_es_turns(user_id, turn_ids, vector_store)
            if not deleted_ok:
                await self._update_job(
                    job_id, self._S_FAILED,
                    error_info="es_delete_failed; summary and redis are consistent, safe to retry",
                )
                return {
                    "status":           "partial",
                    "reason":           "es_delete_failed",
                    "compressed_turns": len(turns),
                    "summary_preview":  summary_text[:120],
                }
            await self._update_job(job_id, self._S_DONE)

            # ── 后台任务：互不依赖，并发启动 ─────────────────────
            asyncio.create_task(
                self._extract_and_store_profile(user_id, turns, llm)
            )
            if vector_store is not None:
                asyncio.create_task(
                    vector_store.store_turn_vectors(
                        user_id, summary_turn_id,
                        summary_turn["user_input"],
                        summary_turn["assistant_response"],
                    )
                )
            if hasattr(memory_manager, "_mysql_init_ref"):
                asyncio.create_task(
                    memory_manager._mysql_init_ref(user_id, summary_turn_id)
                )

            logger.info(
                f"[MemoryArchiver] 归档完成 user={user_id}: "
                f"{len(turns)} 轮 → 摘要 {summary_turn_id[:8]}..."
            )
            return {
                "status":           "ok",
                "compressed_turns": len(turns),
                "summary_preview":  summary_text[:120],
            }

        except Exception as e:
            logger.error(f"[MemoryArchiver] 归档异常 user={user_id}: {e}")
            await self._update_job(job_id, self._S_FAILED, error_info=str(e)[:500])
            return {"status": "error", "reason": str(e)}

    # ══════════════════════════════════════════════════════════
    # 断点续跑（服务启动时调用）
    # ══════════════════════════════════════════════════════════

    @classmethod
    async def resume_failed_jobs(
        cls,
        memory_manager,
        llm,
        vector_store=None,
        max_age_hours: int = 24,
    ) -> None:
        """扫描 MySQL 中未完成的归档任务并从中断点续跑。

        服务启动后由 HermesEngine 调用一次。只处理 max_age_hours 以内的任务，
        更旧的直接标记 failed（认为数据已过期或人工干预）。
        每个 user_id 只恢复最近一条任务，防止并发归档导致数据混乱。
        """
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            rows = await mysql_conn.execute_raw(
                """
                SELECT job_id, user_id, status, summary_turn_id, summary_text,
                       compressed_turns, compressed_turn_ids
                FROM memory_compress_jobs
                WHERE status NOT IN ('done', 'failed')
                  AND started_at >= NOW() - INTERVAL :hours HOUR
                ORDER BY started_at DESC
                """,
                {"hours": max_age_hours},
            )
        except Exception as e:
            logger.warning(f"[MemoryArchiver] 查询未完成归档任务失败: {e}")
            return
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

        if rows is None or len(rows) == 0:
            return

        archiver = cls()
        seen_users: set = set()  # 每个用户只恢复最近一条
        for _, row in rows.iterrows():
            uid = row["user_id"]
            if uid in seen_users:
                # 同一用户的旧任务直接标记 failed
                asyncio.create_task(
                    archiver._update_job(row["job_id"], cls._S_FAILED,
                                         error_info="superseded_by_newer_job")
                )
                continue
            seen_users.add(uid)
            logger.info(
                f"[MemoryArchiver] 发现未完成任务 job={row['job_id'][:8]} "
                f"user={uid} status={row['status']}"
            )
            raw_ids = row.get("compressed_turn_ids")
            try:
                saved_turn_ids: Optional[List[str]] = json.loads(raw_ids) if raw_ids else None
            except Exception:
                saved_turn_ids = None
            asyncio.create_task(
                archiver._resume_job(
                    job_id              = row["job_id"],
                    user_id             = uid,
                    status              = row["status"],
                    summary_turn_id     = row.get("summary_turn_id"),
                    summary_text        = row.get("summary_text"),
                    compressed_turn_ids = saved_turn_ids,
                    memory_manager      = memory_manager,
                    llm                 = llm,
                    vector_store        = vector_store,
                )
            )

    async def _resume_job(
        self,
        job_id:              str,
        user_id:             str,
        status:              str,
        summary_turn_id:     Optional[str],
        summary_text:        Optional[str],
        compressed_turn_ids: Optional[List[str]],
        memory_manager,
        llm,
        vector_store=None,
    ) -> None:
        """根据任务状态从中断点续跑。"""
        logger.info(
            f"[MemoryArchiver] 续跑任务 job={job_id[:8]} user={user_id} from={status}"
        )
        try:
            if status in (self._S_PENDING, self._S_FLUSHING, self._S_SUMMARIZING):
                # 未到关键步骤：重新完整执行（新 run() 会创建新 job 记录）
                await self.run(user_id, llm, memory_manager, vector_store,
                               reason="resume_from_" + status)
                await self._update_job(job_id, self._S_FAILED,
                                       error_info="superseded_by_resume_run")

            elif status == self._S_WRITING_SUMMARY:
                # 摘要已生成，但未确认写入 ES
                if not summary_turn_id or not summary_text:
                    await self.run(user_id, llm, memory_manager, vector_store,
                                   reason="resume_no_summary_data")
                    await self._update_job(job_id, self._S_FAILED,
                                           error_info="missing_summary_on_resume")
                    return
                summary_turn = self._build_summary_turn(summary_turn_id, summary_text)
                if not await self._write_es_turn(user_id, summary_turn):
                    await self._update_job(job_id, self._S_FAILED,
                                           error_info="es_write_failed_on_resume")
                    return
                await self._update_job(job_id, self._S_REPLACING_REDIS)
                await self._replace_redis(user_id, summary_turn)
                await self._resume_deleting_es(
                    job_id, user_id, compressed_turn_ids, vector_store
                )
                if hasattr(memory_manager, "_mysql_init_ref"):
                    asyncio.create_task(
                        memory_manager._mysql_init_ref(user_id, summary_turn_id)
                    )

            elif status == self._S_REPLACING_REDIS:
                # ES 摘要已确认写入，只是 Redis 替换未完成
                if not summary_turn_id or not summary_text:
                    await self._update_job(job_id, self._S_FAILED,
                                           error_info="missing_summary_on_resume")
                    return
                summary_turn = self._build_summary_turn(summary_turn_id, summary_text)
                await self._replace_redis(user_id, summary_turn)
                await self._resume_deleting_es(
                    job_id, user_id, compressed_turn_ids, vector_store
                )
                if hasattr(memory_manager, "_mysql_init_ref"):
                    asyncio.create_task(
                        memory_manager._mysql_init_ref(user_id, summary_turn_id)
                    )

            elif status == self._S_DELETING_ES:
                # Redis 已替换，摘要安全，仅需重试旧数据删除
                await self._resume_deleting_es(
                    job_id, user_id, compressed_turn_ids, vector_store
                )

        except Exception as e:
            logger.error(f"[MemoryArchiver] 续跑任务异常 job={job_id[:8]}: {e}")
            await self._update_job(job_id, self._S_FAILED,
                                   error_info=f"resume_error: {str(e)[:300]}")

    async def _resume_deleting_es(
        self,
        job_id:      str,
        user_id:     str,
        turn_ids:    Optional[List[str]],
        vector_store,
    ) -> None:
        """在续跑路径中执行/重试 ES 旧数据删除，并更新任务状态。"""
        await self._update_job(job_id, self._S_DELETING_ES)
        if turn_ids:
            deleted_ok = await self._delete_old_es_turns(user_id, turn_ids, vector_store)
        else:
            logger.warning(
                f"[MemoryArchiver] 续跑删除：无 turn_ids 记录，跳过删除 job={job_id[:8]}"
            )
            deleted_ok = True
        if deleted_ok:
            await self._update_job(job_id, self._S_DONE)
        else:
            await self._update_job(
                job_id, self._S_FAILED,
                error_info="es_delete_failed_on_resume; safe to retry again",
            )

    @staticmethod
    def _build_summary_turn(summary_turn_id: str, summary_text: str) -> Dict[str, Any]:
        """从已保存的字段重建 summary_turn 字典（用于续跑）。"""
        import re as _re
        # 从 summary_text 元数据注释中提取时间范围（若有）
        time_start = time_end = ""
        m = _re.search(r"start=([^\s>]+)", summary_text or "")
        if m:
            time_start = m.group(1)
        m = _re.search(r"end=([^\s>]+)", summary_text or "")
        if m:
            time_end = m.group(1)
        return {
            "turn_id":            summary_turn_id,
            "user_input":         "[系统摘要]",
            "assistant_response": summary_text,
            "metadata": {
                "type":       "compression_summary",
                "time_start": time_start,
                "time_end":   time_end,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ══════════════════════════════════════════════════════════
    # MySQL 任务状态管理
    # ══════════════════════════════════════════════════════════

    async def _create_job(self, job_id: str, user_id: str, reason: str) -> None:
        """在 memory_compress_jobs 插入任务记录（失败不影响归档主流程）。"""
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            await mysql_conn.execute_raw(
                """
                INSERT INTO memory_compress_jobs (job_id, user_id, status, reason)
                VALUES (:job_id, :user_id, 'pending', :reason)
                """,
                {"job_id": job_id, "user_id": user_id, "reason": reason},
            )
        except Exception as e:
            logger.warning(f"[MemoryArchiver] 创建任务记录失败 job={job_id[:8]}: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

    async def _update_job(
        self,
        job_id:          str,
        status:          str,
        *,
        compressed_turns:    Optional[int] = None,
        compressed_turn_ids: Optional[str] = None,
        summary_turn_id:     Optional[str] = None,
        summary_text:        Optional[str] = None,
        error_info:          Optional[str] = None,
    ) -> None:
        """更新任务状态（任意字段可选，仅传入需要变更的字段）。"""
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            sets   = ["status = :status", "updated_at = NOW()"]
            params: Dict[str, Any] = {"job_id": job_id, "status": status}
            if compressed_turns is not None:
                sets.append("compressed_turns = :compressed_turns")
                params["compressed_turns"] = compressed_turns
            if compressed_turn_ids is not None:
                sets.append("compressed_turn_ids = :compressed_turn_ids")
                params["compressed_turn_ids"] = compressed_turn_ids
            if summary_turn_id is not None:
                sets.append("summary_turn_id = :summary_turn_id")
                params["summary_turn_id"] = summary_turn_id
            if summary_text is not None:
                sets.append("summary_text = :summary_text")
                params["summary_text"] = summary_text[:2000]
            if error_info is not None:
                sets.append("error_info = :error_info")
                params["error_info"] = error_info[:500]
            await mysql_conn.execute_raw(
                f"UPDATE memory_compress_jobs SET {', '.join(sets)} WHERE job_id = :job_id",
                params,
            )
        except Exception as e:
            logger.warning(
                f"[MemoryArchiver] 更新任务状态失败 job={job_id[:8]} status={status}: {e}"
            )
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

    # ══════════════════════════════════════════════════════════
    # 内部步骤（核心归档逻辑）
    # ══════════════════════════════════════════════════════════

    async def _compress(
        self, turns: List[Dict[str, Any]], llm, pre_facts: str = ""
    ) -> str:
        """调用 SummarizerAgent 生成固定结构摘要（不保留原始上下文）。"""
        try:
            from app.agents.workers.summarizer import SummarizerAgent
            return await SummarizerAgent().summarize_conversation(
                turns, llm, key_facts=pre_facts
            )
        except Exception as e:
            logger.error(f"[MemoryArchiver] 摘要生成失败: {e}")
            return ""

    async def _flush_turns_to_es(
        self, user_id: str, turns: List[Dict[str, Any]]
    ) -> None:
        """将 Redis 轮次直接写入 ES，跳过同步锁（压缩前最终持久化）。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            flushed = 0
            for turn in turns:
                tid = turn.get("turn_id")
                if not tid:
                    continue
                try:
                    if await es_conn.read(index=user_id, doc_id=tid):
                        continue
                except Exception:
                    pass
                try:
                    await es_conn.create(
                        index=user_id, doc_id=tid, document=turn, refresh=False
                    )
                    flushed += 1
                except Exception as e:
                    logger.warning(f"[MemoryArchiver] flush turn={tid[:8]} 失败: {e}")
            if flushed:
                logger.info(
                    f"[MemoryArchiver] 压缩前持久化 {flushed}/{len(turns)} 条 → ES"
                )
        except Exception as e:
            logger.warning(f"[MemoryArchiver] _flush_turns_to_es 失败 user={user_id}: {e}")
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _write_es_turn(
        self, user_id: str, summary_turn: Dict[str, Any]
    ) -> bool:
        """将摘要 turn 写入 ES 聊天历史索引。返回 True 表示写入成功。

        Saga 关键检查点：返回 False 时调用方应中止归档，不清除 Redis。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            await es_conn.create(
                index=user_id,
                doc_id=summary_turn["turn_id"],
                document=summary_turn,
                refresh=False,
            )
            logger.info(f"[MemoryArchiver] 摘要已写入 ES user={user_id}")
            return True
        except Exception as e:
            logger.error(f"[MemoryArchiver] 摘要写入 ES 失败 user={user_id}: {e}")
            return False
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _replace_redis(
        self, user_id: str, summary_turn: Dict[str, Any]
    ) -> None:
        """清空 Redis 旧轮次，写入摘要 turn，重置累计计数器为 1。"""
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            await redis_conn.delete_list(_KEY_TURNS.format(user_id=user_id))
            await redis_conn.push_to_list(
                _KEY_TURNS.format(user_id=user_id),
                summary_turn,
                ttl=3600 * 24 * 30,
            )
            # 重置为 1（摘要 turn 本身），避免下轮 store_turn 立即再次触发归档
            await redis_conn.create(_KEY_TOTAL.format(user_id=user_id), 1)
            logger.info(
                f"[MemoryArchiver] Redis 已替换为摘要并重置计数器 user={user_id}"
            )
        except Exception as e:
            logger.warning(f"[MemoryArchiver] Redis 替换失败 user={user_id}: {e}")
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)

    async def _delete_old_es_turns(
        self,
        user_id:     str,
        turn_ids:    List[str],
        vector_store=None,
    ) -> bool:
        """删除 ES 聊天索引中的原始旧轮次文档，并清理对应向量索引中的分块。

        先删向量分块（best-effort），再删聊天文档。任一 turn 删除失败不中止，
        全部成功返回 True，任意失败返回 False（调用方可重试）。
        """
        if not turn_ids:
            return True

        all_ok = True

        # ── 1. 向量索引：按 ref_doc_id 删除分块（best-effort）──
        if vector_store is not None:
            for tid in turn_ids:
                try:
                    await vector_store.delete_turn_vectors(user_id, tid)
                except Exception as e:
                    logger.warning(
                        f"[MemoryArchiver] 向量分块删除失败 turn={tid[:8]} user={user_id}: {e}"
                    )
                    # 向量删除失败不影响主流程一致性，仅记录

        # ── 2. 聊天历史索引：逐条删除 turn 文档 ─────────────────
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            failed_ids: List[str] = []
            for tid in turn_ids:
                try:
                    await es_conn.delete(index=user_id, doc_id=tid)
                except Exception as e:
                    logger.warning(
                        f"[MemoryArchiver] 旧轮次删除失败 turn={tid[:8]} user={user_id}: {e}"
                    )
                    failed_ids.append(tid)
            if failed_ids:
                logger.error(
                    f"[MemoryArchiver] {len(failed_ids)}/{len(turn_ids)} 条旧轮次未能删除 "
                    f"user={user_id}: {failed_ids[:5]}"
                )
                all_ok = False
            else:
                logger.info(
                    f"[MemoryArchiver] 已删除 {len(turn_ids)} 条旧轮次 user={user_id}"
                )
        except Exception as e:
            logger.error(f"[MemoryArchiver] _delete_old_es_turns 异常 user={user_id}: {e}")
            all_ok = False
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

        return all_ok

    # ── 用户画像提取与存储 ──────────────────────────────────────

    async def _extract_and_store_profile(
        self, user_id: str, turns: List[Dict[str, Any]], llm
    ) -> None:
        """从对话中提取用户画像标签，合并历史画像后持久化到 MySQL，并更新 Redis 缓存。"""
        conversation_text = "\n".join(
            f"用户: {t.get('user_input', '')}\n助手: {t.get('assistant_response', '')}"
            for t in turns[-15:]
        )
        system_msg = SystemMessage(content=_load_tag_prompt())
        human_msg  = HumanMessage(content=f"对话内容：\n{conversation_text}")
        try:
            result   = await llm.ainvoke([system_msg, human_msg])
            raw_text = result.content if hasattr(result, "content") else str(result)
            start    = raw_text.find("{")
            end      = raw_text.rfind("}") + 1
            if start == -1 or end == 0:
                logger.warning(
                    f"[MemoryArchiver] 画像提取无 JSON user={user_id}: {raw_text[:100]}"
                )
                return
            new_profile: Dict[str, Any] = json.loads(raw_text[start:end])
        except Exception as e:
            logger.warning(f"[MemoryArchiver] 画像提取失败 user={user_id}: {e}")
            return

        merged = await self._merge_profile(user_id, new_profile)
        await self._store_profile(user_id, merged)

        tag_count = sum(len(v) for v in merged.values() if isinstance(v, list))
        logger.info(
            f"[MemoryArchiver] 用户画像已更新 user={user_id}: {tag_count} 个标签"
        )

    async def _merge_profile(
        self, user_id: str, new_profile: Dict[str, Any]
    ) -> Dict[str, Any]:
        """读取已有画像（MySQL 优先，Redis 兜底），与本次提取结果按字段去重合并。"""
        existing: Dict[str, Any] = {}

        # ── 主读：MySQL ───────────────────────────────────────
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            if mysql_conn:
                df = await mysql_conn.execute_raw(
                    "SELECT profile FROM user_profiles WHERE user_id = :uid",
                    {"uid": user_id},
                )
                if df is not None and len(df) > 0:
                    raw = df.iloc[0]["profile"]
                    existing = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:
            logger.debug(f"[MemoryArchiver] MySQL 画像读取失败，尝试 Redis: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

        # ── 兜底：Redis 缓存（MySQL 无记录时使用）──────────────
        if not existing:
            redis_conn = None
            try:
                redis_conn = await get_connection("redis", None)
                if redis_conn:
                    init_data = await redis_conn.read(USER_INIT.format(user_id=user_id))
                    if isinstance(init_data, dict) and isinstance(init_data.get("profile"), dict):
                        existing = init_data["profile"]
            except Exception:
                pass
            finally:
                if redis_conn:
                    await release_connection("redis", redis_conn)

        # ── 合并：列表字段去重，标量字段覆盖 ─────────────────
        merged: Dict[str, Any] = dict(existing)
        for key, values in new_profile.items():
            if isinstance(values, list):
                merged[key] = list(set(merged.get(key, []) + values))
            else:
                merged[key] = values
        merged["last_updated"] = datetime.now(timezone.utc).isoformat()
        return merged

    async def _store_profile(self, user_id: str, profile: Dict[str, Any]) -> None:
        """将用户画像写入 MySQL（持久化）和 Redis（读缓存）。"""
        profile_json = json.dumps(profile, ensure_ascii=False)

        # ── MySQL：INSERT ... ON DUPLICATE KEY UPDATE（UPSERT）─
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            if mysql_conn:
                await mysql_conn.execute_raw(
                    """
                    INSERT INTO user_profiles (user_id, profile)
                    VALUES (:uid, :profile)
                    ON DUPLICATE KEY UPDATE
                        profile    = VALUES(profile),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    {"uid": user_id, "profile": profile_json},
                )
                logger.info(f"[MemoryArchiver] 用户画像已写入 MySQL user={user_id}")
        except Exception as e:
            logger.warning(f"[MemoryArchiver] 画像写入 MySQL 失败 user={user_id}: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

        # ── Redis：将 profile 字段合入 user:{user_id}:init ──────
        redis_conn = None
        try:
            redis_conn = await get_connection("redis", None)
            if redis_conn:
                init_key  = USER_INIT.format(user_id=user_id)
                init_data = await redis_conn.read(init_key) or {}
                ttl       = await redis_conn.get_ttl(init_key)
                init_data["profile"] = profile
                await redis_conn.create(
                    init_key,
                    init_data,
                    ttl=ttl if ttl > 0 else INIT_TTL,
                )
        except Exception as e:
            logger.warning(f"[MemoryArchiver] 画像写入 Redis 失败 user={user_id}: {e}")
        finally:
            if redis_conn:
                await release_connection("redis", redis_conn)
