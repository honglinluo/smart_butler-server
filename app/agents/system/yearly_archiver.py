"""
【模块说明】年度归档 Agent（YearlyArchiver）— 把三年前的月度摘要按年打包

每年年末自动运行，把 3 年前的 12 条月度摘要合并成 1 条年度摘要，进一步压缩。

【归档结果格式（固定 4 段结构）】
  年度主题 — 这一年总体的主要使用方向和关注点
  重要里程碑 — 这一年中发生的重要事件
  常用 Agent — 这一年最常调用的 AI 能力
  年度总结 — 对这一年整体情况的提炼与总结

年度归档智能体 — 将 3 年以上的月度摘要按年汇总。

触发时机：每年最后一天（12 月 31 日），由 MemoryManager.check_and_schedule_periodic_archives() 触发。
归档对象：创建时间超过 3 年的 monthly_summary turn。
归档粒度：按自然年分组，每年生成一条 yearly_summary turn。
归档内容：固定 4 段结构（年度主题 / 重要里程碑 / 常用 Agent / 年度总结）+ 对话次数/活跃月份元数据。

数据流（Saga 模式）：
  ES 查询 3 年前 monthly_summary turns → 按年分组 → 每年依次：
    1. 调用 SummarizerAgent.summarize_yearly() 生成摘要
    2. 写入 yearly_summary turn 到 ES（Saga 检查点）
    3. 删除该年月度摘要（ES）
  全部年份处理完 → MySQL 更新任务为 done

所需数据库表：
  CREATE TABLE memory_yearly_jobs (
    job_id        VARCHAR(32)  PRIMARY KEY,
    user_id       VARCHAR(36)  NOT NULL,
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending'
                  COMMENT 'pending|running|done|failed',
    target_year   SMALLINT     NOT NULL COMMENT '归档目标年',
    turn_count    INT          DEFAULT 0 COMMENT '该年原始对话次数',
    active_months TINYINT      DEFAULT 0 COMMENT '该年活跃月份数',
    summary_turn_id VARCHAR(32),
    error_info    TEXT,
    started_at    DATETIME     NOT NULL DEFAULT NOW(),
    updated_at    DATETIME     NOT NULL DEFAULT NOW() ON UPDATE NOW(),
    UNIQUE KEY uq_user_year (user_id, target_year),
    INDEX idx_user_status (user_id, status)
  );
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

# 超过多少天的月度摘要才纳入年度归档（3 × 365 天）
_YEARLY_MIN_AGE_DAYS = 3 * 365


class YearlyArchiverAgent:
    """年度归档智能体（系统内置，不注册到 AgentRegistry）。

    由 MemoryManager.check_and_schedule_periodic_archives() 在年底触发，
    对当前日期减去 3 年之前的所有 monthly_summary 按年分组，
    为每个自然年生成一条 yearly_summary。
    """

    name = "yearly_archiver"

    _S_PENDING = "pending"
    _S_RUNNING = "running"
    _S_DONE    = "done"
    _S_FAILED  = "failed"

    # ══════════════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════════════

    async def run(
        self,
        user_id:     str,
        llm,
        vector_store=None,
    ) -> Dict[str, Any]:
        """扫描并归档指定用户 3 年以前的月度摘要。"""
        logger.info(f"[YearlyArchiver] 开始年度归档 user={user_id}")

        cutoff = datetime.now(timezone.utc) - timedelta(days=_YEARLY_MIN_AGE_DAYS)

        years_to_archive = await self._get_years_to_archive(user_id, cutoff)
        if not years_to_archive:
            logger.info(f"[YearlyArchiver] 无需归档的年份 user={user_id}")
            return {"status": "skipped", "reason": "no_years"}

        results: List[Dict[str, Any]] = []
        for year in years_to_archive:
            res = await self._archive_one_year(user_id, year, llm, vector_store)
            results.append({"year": year, **res})

        ok_count   = sum(1 for r in results if r.get("status") == "ok")
        fail_count = len(results) - ok_count
        logger.info(
            f"[YearlyArchiver] 年度归档完成 user={user_id}: "
            f"{ok_count}/{len(results)} 成功，{fail_count} 失败"
        )
        return {
            "status":  "ok" if fail_count == 0 else "partial",
            "years":   len(results),
            "ok":      ok_count,
            "failed":  fail_count,
            "details": results,
        }

    # ══════════════════════════════════════════════════════════
    # 单年归档
    # ══════════════════════════════════════════════════════════

    async def _archive_one_year(
        self,
        user_id:     str,
        year:        int,
        llm,
        vector_store,
    ) -> Dict[str, Any]:
        """归档单个自然年的月度摘要。"""
        job_id = uuid.uuid4().hex
        await self._upsert_job(job_id, user_id, year)

        try:
            # ── Step 1: 查询该年全部 monthly_summary ────────────
            monthly_turns, turn_ids = await self._query_year_monthly_turns(user_id, year)
            if not monthly_turns:
                await self._update_job(job_id, self._S_DONE)
                return {"status": "skipped", "reason": "no_monthly_turns"}

            # 统计原始对话次数和活跃月份数
            total_turn_count = sum(
                int((t.get("metadata") or {}).get("turn_count", 0))
                for t in monthly_turns
            )
            active_months = len(monthly_turns)

            await self._update_job(
                job_id, self._S_RUNNING,
                turn_count    = total_turn_count,
                active_months = active_months,
            )

            # ── Step 2: 生成年度摘要 ─────────────────────────────
            from app.agents.workers.summarizer import SummarizerAgent
            summary_text = await SummarizerAgent().summarize_yearly(
                year          = year,
                monthly_turns = monthly_turns,
                llm           = llm,
                turn_count    = total_turn_count,
                active_months = active_months,
            )
            if not summary_text:
                await self._update_job(job_id, self._S_FAILED, error_info="summarize_failed")
                return {"status": "error", "reason": "summarize_failed"}

            summary_turn_id = uuid.uuid4().hex
            # 提取活跃月份列表
            month_nums = sorted(
                int((t.get("metadata") or {}).get("month", 0))
                for t in monthly_turns
                if (t.get("metadata") or {}).get("month")
            )
            summary_turn: Dict[str, Any] = {
                "turn_id":            summary_turn_id,
                "user_input":         f"[年度摘要 {year}年]",
                "assistant_response": summary_text,
                "metadata": {
                    "type":          "yearly_summary",
                    "year":          year,
                    "turn_count":    total_turn_count,
                    "active_months": active_months,
                    "month_list":    month_nums,
                    "time_start":    f"{year}-01-01T00:00:00+00:00",
                    "time_end":      f"{year}-12-31T23:59:59+00:00",
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            # ── Step 3 (Saga 检查点): 写入 yearly_summary → ES ──
            if not await self._write_es_turn(user_id, summary_turn):
                await self._update_job(job_id, self._S_FAILED, error_info="es_write_failed")
                return {"status": "error", "reason": "es_write_failed"}

            await self._update_job(
                job_id, self._S_DONE, summary_turn_id=summary_turn_id
            )

            # ── Step 4: 删除已归档的月度摘要（best-effort）──────
            await self._delete_old_turns(user_id, turn_ids, vector_store)

            # 后台向量化年度摘要
            if vector_store is not None:
                asyncio.create_task(
                    vector_store.store_turn_vectors(
                        user_id, summary_turn_id,
                        summary_turn["user_input"],
                        summary_turn["assistant_response"],
                    )
                )

            logger.info(
                f"[YearlyArchiver] {year} 年度归档完成 "
                f"months={active_months} turns={total_turn_count} user={user_id}"
            )
            return {
                "status":        "ok",
                "active_months": active_months,
                "turn_count":    total_turn_count,
            }

        except Exception as e:
            logger.error(f"[YearlyArchiver] {year} 年度归档异常 user={user_id}: {e}")
            await self._update_job(job_id, self._S_FAILED, error_info=str(e)[:500])
            return {"status": "error", "reason": str(e)}

    # ══════════════════════════════════════════════════════════
    # ES 查询与写入
    # ══════════════════════════════════════════════════════════

    async def _get_years_to_archive(
        self, user_id: str, cutoff: datetime
    ) -> List[int]:
        """获取需要归档的年份列表（已有 yearly_summary 的跳过）。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)

            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

            # 查询 cutoff 之前的 monthly_summary
            result = await es_conn.search(
                index  = user_id,
                query  = {
                    "bool": {
                        "filter": [
                            {"term":  {"metadata.type": "monthly_summary"}},
                            {"range": {"timestamp": {"lt": cutoff_str}}},
                        ]
                    }
                },
                size = 500,
            )
            hits = (result.get("hits") or {}).get("hits") or []
            years_with_data: set = set()
            for hit in hits:
                meta = (hit.get("_source") or {}).get("metadata") or {}
                y = meta.get("year")
                if y:
                    years_with_data.add(int(y))

            if not years_with_data:
                return []

            # 查询已有 yearly_summary 的年份
            archived_result = await es_conn.search(
                index = user_id,
                query = {"term": {"metadata.type": "yearly_summary"}},
                size  = 100,
            )
            archived_hits = (archived_result.get("hits") or {}).get("hits") or []
            archived_years: set = set()
            for hit in archived_hits:
                meta = (hit.get("_source") or {}).get("metadata") or {}
                y = meta.get("year")
                if y:
                    archived_years.add(int(y))

            return sorted(years_with_data - archived_years)

        except Exception as e:
            logger.error(f"[YearlyArchiver] 查询待归档年份失败 user={user_id}: {e}")
            return []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _query_year_monthly_turns(
        self, user_id: str, year: int
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """查询指定年份的全部 monthly_summary turns。"""
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)

            result = await es_conn.search(
                index  = user_id,
                query  = {
                    "bool": {
                        "filter": [
                            {"term":  {"metadata.type": "monthly_summary"}},
                            {"term":  {"metadata.year": year}},
                        ]
                    }
                },
                size = 12,  # 最多 12 个月
                sort = [{"metadata.month": {"order": "asc"}}],
            )
            hits     = (result.get("hits") or {}).get("hits") or []
            turns    = [h["_source"] for h in hits if h.get("_source")]
            turn_ids = [h["_id"]     for h in hits if h.get("_id")]
            return turns, turn_ids

        except Exception as e:
            logger.error(
                f"[YearlyArchiver] 查询 {year} 年月度数据失败 user={user_id}: {e}"
            )
            return [], []
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _write_es_turn(self, user_id: str, turn: Dict[str, Any]) -> bool:
        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            await es_conn.create(
                index    = user_id,
                doc_id   = turn["turn_id"],
                document = turn,
                refresh  = False,
            )
            return True
        except Exception as e:
            logger.error(f"[YearlyArchiver] 年度摘要写入 ES 失败 user={user_id}: {e}")
            return False
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    async def _delete_old_turns(
        self, user_id: str, turn_ids: List[str], vector_store
    ) -> None:
        """删除已归档的月度摘要 turns（best-effort）。"""
        if vector_store is not None:
            for tid in turn_ids:
                try:
                    await vector_store.delete_turn_vectors(user_id, tid)
                except Exception as e:
                    logger.warning(
                        f"[YearlyArchiver] 向量删除失败 turn={tid[:8]}: {e}"
                    )

        es_conn = None
        try:
            es_conn = await get_connection("elasticsearch", None)
            for tid in turn_ids:
                try:
                    await es_conn.delete(index=user_id, doc_id=tid)
                except Exception as e:
                    logger.warning(
                        f"[YearlyArchiver] 月度摘要删除失败 turn={tid[:8]}: {e}"
                    )
        except Exception as e:
            logger.error(f"[YearlyArchiver] _delete_old_turns 异常 user={user_id}: {e}")
        finally:
            if es_conn:
                await release_connection("elasticsearch", es_conn)

    # ══════════════════════════════════════════════════════════
    # MySQL 任务状态管理
    # ══════════════════════════════════════════════════════════

    async def _upsert_job(
        self, job_id: str, user_id: str, year: int
    ) -> None:
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            await mysql_conn.execute_raw(
                """
                INSERT INTO memory_yearly_jobs
                  (job_id, user_id, status, target_year)
                VALUES (:job_id, :user_id, 'pending', :year)
                ON DUPLICATE KEY UPDATE
                  job_id = :job_id, status = 'pending', updated_at = NOW()
                """,
                {"job_id": job_id, "user_id": user_id, "year": year},
            )
        except Exception as e:
            logger.warning(f"[YearlyArchiver] upsert_job 失败: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)

    async def _update_job(
        self,
        job_id: str,
        status: str,
        *,
        turn_count:     Optional[int] = None,
        active_months:  Optional[int] = None,
        summary_turn_id: Optional[str] = None,
        error_info:     Optional[str] = None,
    ) -> None:
        mysql_conn = None
        try:
            mysql_conn = await get_connection("mysql", None)
            sets   = ["status = :status", "updated_at = NOW()"]
            params: Dict[str, Any] = {"job_id": job_id, "status": status}
            if turn_count is not None:
                sets.append("turn_count = :turn_count")
                params["turn_count"] = turn_count
            if active_months is not None:
                sets.append("active_months = :active_months")
                params["active_months"] = active_months
            if summary_turn_id is not None:
                sets.append("summary_turn_id = :summary_turn_id")
                params["summary_turn_id"] = summary_turn_id
            if error_info is not None:
                sets.append("error_info = :error_info")
                params["error_info"] = error_info[:500]
            await mysql_conn.execute_raw(
                f"UPDATE memory_yearly_jobs SET {', '.join(sets)} WHERE job_id = :job_id",
                params,
            )
        except Exception as e:
            logger.warning(f"[YearlyArchiver] update_job 失败: {e}")
        finally:
            if mysql_conn:
                await release_connection("mysql", mysql_conn)
