"""月度归档智能体 — 将 1 年以上的历史对话按月汇总。

触发时机：每月最后一天，由 MemoryManager.check_and_schedule_periodic_archives() 触发。
归档对象：创建时间超过 1 年的 compression_summary turn（及未被压缩的原始 turn）。
归档粒度：按自然月分组，每月生成一条 monthly_summary turn。
归档内容：固定 4 段结构（主要话题 / 关键事件 / 使用 Agent / 重要结论）+ 对话次数元数据。

数据流（Saga 模式）：
  ES 查询旧 turns → 按月分组 → 每月依次：
    1. 调用 SummarizerAgent.summarize_monthly() 生成摘要
    2. 写入 monthly_summary turn 到 ES（Saga 检查点）
    3. 删除该月原始/压缩 turns（ES）
  全部月份处理完 → MySQL 更新任务为 done

所需数据库表：
  CREATE TABLE memory_monthly_jobs (
    job_id       VARCHAR(32)  PRIMARY KEY,
    user_id      VARCHAR(36)  NOT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending'
                 COMMENT 'pending|running|done|failed',
    target_year  SMALLINT     NOT NULL COMMENT '归档目标年',
    target_month TINYINT      NOT NULL COMMENT '归档目标月',
    turn_count   INT          DEFAULT 0 COMMENT '该月原始对话次数',
    summary_turn_id VARCHAR(32),
    error_info   TEXT,
    started_at   DATETIME     NOT NULL DEFAULT NOW(),
    updated_at   DATETIME     NOT NULL DEFAULT NOW() ON UPDATE NOW(),
    UNIQUE KEY uq_user_ym (user_id, target_year, target_month),
    INDEX idx_user_status (user_id, status)
  );
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from calendar import monthrange
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

# 超过多少天的数据才纳入月度归档（365 天 = 1 年）
_MONTHLY_MIN_AGE_DAYS = 365


class MonthlyArchiverAgent:
    """月度归档智能体（系统内置，不注册到 AgentRegistry）。

    由 MemoryManager.check_and_schedule_periodic_archives() 在月底触发，
    对当前日期减去 1 年之前的所有历史数据（compression_summary 及原始 turn）
    按月分组，为每个自然月生成一条 monthly_summary。
    """

    name = "monthly_archiver"

    _S_PENDING  = "pending"
    _S_RUNNING  = "running"
    _S_DONE     = "done"
    _S_FAILED   = "failed"

    # ══════════════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════════════

    async def run(
        self,
        user_id:     str,
        llm,
        vector_store=None,
    ) -> Dict[str, Any]:
        """扫描并归档指定用户 1 年以前的历史数据。

        每个需要归档的自然月独立处理，任一月失败不影响其他月。
        """
        logger.info(f"[MonthlyArchiver] 开始月度归档 user={user_id}")

        cutoff = datetime.now(timezone.utc) - timedelta(days=_MONTHLY_MIN_AGE_DAYS)

        # 获取需要归档的月份列表（去掉已有 monthly_summary 的月份）
        months_to_archive = await self._get_months_to_archive(user_id, cutoff)
        if not months_to_archive:
            logger.info(f"[MonthlyArchiver] 无需归档的月份 user={user_id}")
            return {"status": "skipped", "reason": "no_months"}

        results: List[Dict[str, Any]] = []
        for year, month in months_to_archive:
            res = await self._archive_one_month(user_id, year, month, llm, vector_store)
            results.append({"year": year, "month": month, **res})

        ok_count   = sum(1 for r in results if r.get("status") == "ok")
        fail_count = len(results) - ok_count
        logger.info(
            f"[MonthlyArchiver] 月度归档完成 user={user_id}: "
            f"{ok_count}/{len(results)} 成功，{fail_count} 失败"
        )
        return {
            "status":     "ok" if fail_count == 0 else "partial",
            "months":     len(results),
            "ok":         ok_count,
            "failed":     fail_count,
            "details":    results,
        }

    # ══════════════════════════════════════════════════════════
    # 单月归档
    # ══════════════════════════════════════════════════════════

    async def _archive_one_month(
        self,
        user_id:     str,
        year:        int,
        month:       int,
        llm,
        vector_store,
    ) -> Dict[str, Any]:
        """归档单个自然月。使用简单三步 Saga：生成→写入→删除。"""
        month_label = f"{year}-{month:02d}"
        job_id      = uuid.uuid4().hex

        await self._upsert_job(job_id, user_id, year, month)

        try:
            # ── Step 1: 查询该月全部 turns ──────────────────────
            turns, turn_ids = await self._query_month_turns(user_id, year, month)
            if not turns:
                await self._update_job(job_id, self._S_DONE)
                return {"status": "skipped", "reason": "no_turns"}

            # 原始对话次数（摘要 turn 不计入）
            raw_turn_count = sum(
                1 for t in turns
                if (t.get("metadata") or {}).get("type") not in
                   ("compression_summary", "monthly_summary", "yearly_summary")
            )
            # 若全部都是摘要（已归档过），只统计 compressed_turns 字段
            if raw_turn_count == 0:
                raw_turn_count = sum(
                    int((t.get("metadata") or {}).get("compressed_turns", 0))
                    for t in turns
                )

            await self._update_job(job_id, self._S_RUNNING, turn_count=raw_turn_count)

            # ── Step 2: 生成月度摘要 ─────────────────────────────
            from app.agents.workers.summarizer import SummarizerAgent
            summary_text = await SummarizerAgent().summarize_monthly(
                year       = year,
                month      = month,
                turns      = turns,
                llm        = llm,
                turn_count = raw_turn_count,
            )
            if not summary_text:
                await self._update_job(job_id, self._S_FAILED, error_info="summarize_failed")
                return {"status": "error", "reason": "summarize_failed"}

            summary_turn_id = uuid.uuid4().hex
            _, last_day = monthrange(year, month)
            summary_turn: Dict[str, Any] = {
                "turn_id":            summary_turn_id,
                "user_input":         f"[月度摘要 {month_label}]",
                "assistant_response": summary_text,
                "metadata": {
                    "type":        "monthly_summary",
                    "year":        year,
                    "month":       month,
                    "turn_count":  raw_turn_count,
                    "time_start":  f"{year}-{month:02d}-01T00:00:00+00:00",
                    "time_end":    f"{year}-{month:02d}-{last_day:02d}T23:59:59+00:00",
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            # ── Step 3 (Saga 检查点): 写入 monthly_summary → ES ─
            if not await self._write_es_turn(user_id, summary_turn):
                await self._update_job(job_id, self._S_FAILED, error_info="es_write_failed")
                return {"status": "error", "reason": "es_write_failed"}

            await self._update_job(
                job_id, self._S_DONE, summary_turn_id=summary_turn_id
            )

            # ── Step 4: 删除原始旧 turns（摘要已安全，可重试）──
            await self._delete_old_turns(user_id, turn_ids, vector_store)

            # 后台向量化月度摘要
            if vector_store is not None:
                asyncio.create_task(
                    vector_store.store_turn_vectors(
                        user_id, summary_turn_id,
                        summary_turn["user_input"],
                        summary_turn["assistant_response"],
                    )
                )

            logger.info(
                f"[MonthlyArchiver] {month_label} 归档完成 "
                f"turns={len(turns)} raw_count={raw_turn_count} user={user_id}"
            )
            return {"status": "ok", "turn_count": raw_turn_count}

        except Exception as e:
            logger.error(f"[MonthlyArchiver] {month_label} 归档异常 user={user_id}: {e}")
            await self._update_job(job_id, self._S_FAILED, error_info=str(e)[:500])
            return {"status": "error", "reason": str(e)}

    # ══════════════════════════════════════════════════════════
    # ES 查询与写入
    # ══════════════════════════════════════════════════════════

    async def _get_months_to_archive(
        self, user_id: str, cutoff: datetime
    ) -> List[Tuple[int, int]]:
        """获取需要归档的月份列表（已有 monthly_summary 的跳过）。

        策略：先查 cutoff 之前有数据的月份，再查已归档的月份，取差集。
        """
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return []

            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

            # ── 1. 查询有数据的月份（bucket aggregation）──────────
            # 由于 ES Python 客户端版本差异，通过 search + 结果日期解析代替 agg
            result = await es_conn.search(
                index  = user_id,
                query  = {
                    "bool": {
                        "filter": [
                            {"range": {"timestamp": {"lt": cutoff_str}}},
                        ],
                        "must_not": [
                            {"terms": {"metadata.type": ["monthly_summary", "yearly_summary"]}},
                        ],
                    }
                },
                size = 1000,  # 最多取 1000 条用于月份提取
                sort = [{"timestamp": {"order": "asc"}}],
            )
            hits   = (result.get("hits") or {}).get("hits") or []
            months_with_data: set = set()
            for hit in hits:
                ts_str = (hit.get("_source") or {}).get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    months_with_data.add((dt.year, dt.month))
                except Exception:
                    pass

            if not months_with_data:
                return []

            # ── 2. 查询已有 monthly_summary 的月份 ───────────────
            archived_result = await es_conn.search(
                index = user_id,
                query = {"term": {"metadata.type": "monthly_summary"}},
                size  = 500,
            )
            archived_hits = (archived_result.get("hits") or {}).get("hits") or []
            archived_months: set = set()
            for hit in archived_hits:
                meta = (hit.get("_source") or {}).get("metadata") or {}
                y = meta.get("year")
                m = meta.get("month")
                if y and m:
                    archived_months.add((int(y), int(m)))

            # 差集：有数据但尚未归档的月份，按时间升序排列
            to_archive = sorted(months_with_data - archived_months)
            return to_archive

        except Exception as e:
            logger.error(f"[MonthlyArchiver] 查询待归档月份失败 user={user_id}: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _query_month_turns(
        self, user_id: str, year: int, month: int
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """查询指定月份的全部 turns（原始 + compression_summary），返回 (turns, turn_ids)。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return [], []

            _, last_day = monthrange(year, month)
            start_str = f"{year}-{month:02d}-01T00:00:00Z"
            end_str   = f"{year}-{month:02d}-{last_day:02d}T23:59:59Z"

            result = await es_conn.search(
                index  = user_id,
                query  = {
                    "bool": {
                        "filter": [
                            {"range": {"timestamp": {"gte": start_str, "lte": end_str}}},
                        ],
                        "must_not": [
                            {"terms": {"metadata.type": ["monthly_summary", "yearly_summary"]}},
                        ],
                    }
                },
                size = 500,
                sort = [{"timestamp": {"order": "asc"}}],
            )
            hits = (result.get("hits") or {}).get("hits") or []
            turns    = [h["_source"] for h in hits if h.get("_source")]
            turn_ids = [h["_id"]     for h in hits if h.get("_id")]
            return turns, turn_ids

        except Exception as e:
            logger.error(
                f"[MonthlyArchiver] 查询 {year}-{month:02d} 数据失败 user={user_id}: {e}"
            )
            return [], []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _write_es_turn(self, user_id: str, turn: Dict[str, Any]) -> bool:
        """写入月度摘要 turn 到 ES，返回是否成功（Saga 检查点）。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return False
            await es_conn.create(
                index    = user_id,
                doc_id   = turn["turn_id"],
                document = turn,
                refresh  = False,
            )
            return True
        except Exception as e:
            logger.error(f"[MonthlyArchiver] 月度摘要写入 ES 失败 user={user_id}: {e}")
            return False
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _delete_old_turns(
        self, user_id: str, turn_ids: List[str], vector_store
    ) -> None:
        """删除已归档的旧 turns（best-effort，失败仅记日志，不影响一致性）。"""
        # 向量索引
        if vector_store is not None:
            for tid in turn_ids:
                try:
                    await vector_store.delete_turn_vectors(user_id, tid)
                except Exception as e:
                    logger.warning(
                        f"[MonthlyArchiver] 向量删除失败 turn={tid[:8]}: {e}"
                    )

        # ES 聊天索引
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            if not es_conn:
                return
            for tid in turn_ids:
                try:
                    await es_conn.delete(index=user_id, doc_id=tid)
                except Exception as e:
                    logger.warning(
                        f"[MonthlyArchiver] 旧 turn 删除失败 turn={tid[:8]}: {e}"
                    )
        except Exception as e:
            logger.error(f"[MonthlyArchiver] _delete_old_turns 异常 user={user_id}: {e}")
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    # ══════════════════════════════════════════════════════════
    # MySQL 任务状态管理
    # ══════════════════════════════════════════════════════════

    async def _upsert_job(
        self, job_id: str, user_id: str, year: int, month: int
    ) -> None:
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            if not mysql_conn:
                return
            await mysql_conn.execute_raw(
                """
                INSERT INTO memory_monthly_jobs
                  (job_id, user_id, status, target_year, target_month)
                VALUES (:job_id, :user_id, 'pending', :year, :month)
                ON DUPLICATE KEY UPDATE
                  job_id = :job_id, status = 'pending', updated_at = NOW()
                """,
                {"job_id": job_id, "user_id": user_id, "year": year, "month": month},
            )
        except Exception as e:
            logger.warning(f"[MonthlyArchiver] upsert_job 失败: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

    async def _update_job(
        self,
        job_id: str,
        status: str,
        *,
        turn_count:     Optional[int] = None,
        summary_turn_id: Optional[str] = None,
        error_info:     Optional[str] = None,
    ) -> None:
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            if not mysql_conn:
                return
            sets   = ["status = :status", "updated_at = NOW()"]
            params: Dict[str, Any] = {"job_id": job_id, "status": status}
            if turn_count is not None:
                sets.append("turn_count = :turn_count")
                params["turn_count"] = turn_count
            if summary_turn_id is not None:
                sets.append("summary_turn_id = :summary_turn_id")
                params["summary_turn_id"] = summary_turn_id
            if error_info is not None:
                sets.append("error_info = :error_info")
                params["error_info"] = error_info[:500]
            await mysql_conn.execute_raw(
                f"UPDATE memory_monthly_jobs SET {', '.join(sets)} WHERE job_id = :job_id",
                params,
            )
        except Exception as e:
            logger.warning(f"[MonthlyArchiver] update_job 失败: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)
