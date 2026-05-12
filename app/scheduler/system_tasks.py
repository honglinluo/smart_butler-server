"""
【模块说明】系统级定时任务 — 幕后自动维护的"管家任务"

除了用户自己创建的定时任务，系统本身也有几个需要定期自动运行的维护任务。
这些任务对用户不可见，由系统在服务器后台静默执行。

【四个系统任务】
  月度记忆归档（每月末 23:00 UTC）
    — 遍历所有用户，把 1 年前的老对话历史压缩归档，节省存储空间

  年度记忆归档（每年 12 月 31 日 01:00 UTC）
    — 把 3 年前的月度摘要进一步压缩为年度摘要

  用户文件清理（每天 02:00 UTC）
    — 删除超过配置天数的用户上传文件，释放磁盘空间
    — 如果 cleanup_days = -1，则永不清理

  Skill 自演进（每天 03:00 UTC）
    — 对使用次数达到阈值的 Agent，让 AI 自动优化或生成技能脚本

【注册机制】
  服务启动时调用 register_system_tasks(scheduler)，该函数：
  1. 把每个任务的处理函数（handler）注册到调度器的函数表中
  2. 在数据库中创建任务记录（幂等：已存在则跳过）

系统级定时任务注册 — 月度归档 / 年度归档。

这里的任务对用户不可见（user_id="__system__"，不通过 API 暴露），
由 main.py 在服务启动后调用 register_system_tasks() 完成注册。

Cron 调度计划（UTC 时间）：
  月度归档：0 23 28,29,30,31 * *
    — 每月 28~31 日 23:00 UTC 触发；handler 内判断当日是否为月末，非月末直接跳过。
    — 月末当天遍历所有活跃用户，将 1 年前的历史数据按月归档。

  年度归档：0 1 31 12 *
    — 每年 12 月 31 日 01:00 UTC 触发。
    — 遍历所有活跃用户，将 3 年前的月度摘要按年归档。
"""
from __future__ import annotations

import calendar
import logging
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List

from app.database.pool import get_connection, release_connection
from app.scheduler.models import ActionType, ScheduledTask, TaskType

logger = logging.getLogger(__name__)

# ── 系统任务固定标识（作为 task_id 和 agent_name）─────────────────────────────
_SYS_USER_ID        = "__system__"
_MONTHLY_TASK_ID    = "__sys_monthly_archive__"
_MONTHLY_HANDLER    = "__monthly_archive__"
_YEARLY_TASK_ID     = "__sys_yearly_archive__"
_YEARLY_HANDLER     = "__yearly_archive__"
_FILE_CLEANUP_TASK_ID = "__sys_file_cleanup__"
_FILE_CLEANUP_HANDLER = "__file_cleanup__"
_SKILL_EVOLUTION_TASK_ID = "__sys_skill_evolution__"
_SKILL_EVOLUTION_HANDLER = "__skill_evolution__"


# ══════════════════════════════════════════════════════════════════════════════
# 辅助：查询所有活跃用户
# ══════════════════════════════════════════════════════════════════════════════

async def _get_all_active_user_ids() -> List[str]:
    """从 MySQL memory_references（或 scheduled_tasks）中取所有有记录的 user_id。

    以 memory_references 表为主数据源（有过对话记录的用户）；
    若该表不存在则降级到 scheduled_tasks 中的非系统用户。
    """
    conn = None
    try:
        conn = await get_connection("mysql", None)

        # 优先从 memory_references 取有对话记录的用户
        try:
            rows = await conn.execute_raw(
                "SELECT DISTINCT user_id FROM memory_references LIMIT 10000",
                {},
            )
            if rows is not None and len(rows) > 0:
                return [r for r in rows["user_id"].tolist() if r and r != _SYS_USER_ID]
        except Exception:
            pass

        # 降级：从 scheduled_tasks 取所有非系统用户
        rows = await conn.execute_raw(
            "SELECT DISTINCT user_id FROM scheduled_tasks "
            "WHERE user_id != :sys AND status != 'cancelled' LIMIT 10000",
            {"sys": _SYS_USER_ID},
        )
        if rows is not None and len(rows) > 0:
            return [r for r in rows["user_id"].tolist() if r]
        return []

    except Exception as e:
        logger.error("[SystemTasks] 查询活跃用户失败: %s", e)
        return []
    finally:
        if conn:
            await release_connection("mysql", conn)


def _is_last_day_of_month() -> bool:
    """判断今天（UTC）是否为当月最后一天。"""
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day


# ══════════════════════════════════════════════════════════════════════════════
# Handler：月度归档
# ══════════════════════════════════════════════════════════════════════════════

async def monthly_archive_handler(task: ScheduledTask, hermes_engine) -> str:
    """月度归档系统任务 handler。

    在月末当天遍历所有活跃用户，调用 MonthlyArchiverAgent 将 1 年前的历史
    对话数据按自然月归档。非月末日期直接跳过（cron 28-31 日均触发，handler
    内判断实际执行时机）。

    Returns:
        str: 执行结果摘要（写入 task_run_logs.output）
    """
    if not _is_last_day_of_month():
        today = date.today()
        return f"非月末({today})，跳过月度归档"

    memory_manager = getattr(hermes_engine, "memory_manager", None)
    if memory_manager is None:
        return "memory_manager 未注入，跳过月度归档"

    llm = getattr(memory_manager, "_default_llm", None)
    if llm is None:
        return "LLM 未配置，跳过月度归档"

    vector_store = getattr(memory_manager, "_vector_store", None)

    user_ids = await _get_all_active_user_ids()
    if not user_ids:
        return "无活跃用户，跳过月度归档"

    from app.agents.system.monthly_archiver import MonthlyArchiverAgent
    archiver = MonthlyArchiverAgent()

    ok_users = failed_users = skipped_users = 0
    for uid in user_ids:
        try:
            result = await archiver.run(
                user_id     = uid,
                llm         = llm,
                vector_store = vector_store,
            )
            status = result.get("status", "")
            if status == "ok":
                ok_users += 1
            elif status == "skipped":
                skipped_users += 1
            else:
                failed_users += 1
        except Exception as e:
            failed_users += 1
            logger.error("[MonthlyArchiveTask] 用户 %s 归档失败: %s", uid, e)

    today_str = date.today().isoformat()
    summary = (
        f"月度归档 {today_str}：共 {len(user_ids)} 用户 | "
        f"成功 {ok_users} | 跳过 {skipped_users} | 失败 {failed_users}"
    )
    logger.info("[SystemTasks] %s", summary)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# Handler：年度归档
# ══════════════════════════════════════════════════════════════════════════════

async def yearly_archive_handler(task: ScheduledTask, hermes_engine) -> str:
    """年度归档系统任务 handler。

    在每年 12 月 31 日遍历所有活跃用户，调用 YearlyArchiverAgent 将 3 年前的
    月度摘要按自然年归档。

    Returns:
        str: 执行结果摘要（写入 task_run_logs.output）
    """
    memory_manager = getattr(hermes_engine, "memory_manager", None)
    if memory_manager is None:
        return "memory_manager 未注入，跳过年度归档"

    llm = getattr(memory_manager, "_default_llm", None)
    if llm is None:
        return "LLM 未配置，跳过年度归档"

    vector_store = getattr(memory_manager, "_vector_store", None)

    user_ids = await _get_all_active_user_ids()
    if not user_ids:
        return "无活跃用户，跳过年度归档"

    from app.agents.system.yearly_archiver import YearlyArchiverAgent
    archiver = YearlyArchiverAgent()

    ok_users = failed_users = skipped_users = 0
    for uid in user_ids:
        try:
            result = await archiver.run(
                user_id     = uid,
                llm         = llm,
                vector_store = vector_store,
            )
            status = result.get("status", "")
            if status == "ok":
                ok_users += 1
            elif status == "skipped":
                skipped_users += 1
            else:
                failed_users += 1
        except Exception as e:
            failed_users += 1
            logger.error("[YearlyArchiveTask] 用户 %s 归档失败: %s", uid, e)

    today_str = date.today().isoformat()
    summary = (
        f"年度归档 {today_str}：共 {len(user_ids)} 用户 | "
        f"成功 {ok_users} | 跳过 {skipped_users} | 失败 {failed_users}"
    )
    logger.info("[SystemTasks] %s", summary)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# Handler：文件清理
# ══════════════════════════════════════════════════════════════════════════════

async def file_cleanup_handler(task: ScheduledTask, hermes_engine) -> str:
    """文件清理系统任务 handler。

    读取 config/system_config.yaml file_storage.cleanup_days 配置：
      - cleanup_days == -1：永不清理，直接跳过。
      - cleanup_days >  0 ：删除 UPLOAD_ROOT 下所有用户目录中
        修改时间超过 cleanup_days 天的文件。

    每日 UTC 02:00 触发（cron: 0 2 * * *）。
    """
    from app.core.file_storage import UPLOAD_ROOT, CLEANUP_DAYS

    if CLEANUP_DAYS < 0:
        return "cleanup_days=-1，永不清理，跳过"

    if not UPLOAD_ROOT.exists():
        return f"上传目录不存在（{UPLOAD_ROOT}），跳过"

    cutoff     = datetime.utcnow() - timedelta(days=CLEANUP_DAYS)
    deleted    = 0
    failed     = 0
    freed_bytes = 0

    for user_dir in UPLOAD_ROOT.iterdir():
        if not user_dir.is_dir():
            continue
        for p in list(user_dir.rglob("*")):
            if not p.is_file():
                continue
            try:
                mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                if mtime < cutoff:
                    size = p.stat().st_size
                    p.unlink()
                    freed_bytes += size
                    deleted += 1
            except Exception as e:
                failed += 1
                logger.warning("[FileCleanup] 删除失败 %s: %s", p, e)

    # 清理空目录（从深到浅）
    for user_dir in UPLOAD_ROOT.iterdir():
        if not user_dir.is_dir():
            continue
        for d in sorted(user_dir.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if d.is_dir():
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except Exception:
                    pass

    freed_mb = freed_bytes / (1024 * 1024)
    summary  = (
        f"文件清理（保留 {CLEANUP_DAYS} 天）："
        f"删除 {deleted} 个文件 / 释放 {freed_mb:.1f} MB / 失败 {failed} 个"
    )
    logger.info("[SystemTasks] %s", summary)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# Handler：Skill 演进
# ══════════════════════════════════════════════════════════════════════════════

async def skill_evolution_handler(task: ScheduledTask, hermes_engine) -> str:
    """Skill 自演进系统任务 handler。

    遍历所有 code-type agent，对调用次数满足阈值的 agent 触发 skill 演进。
    每日 UTC 03:00 触发（低峰期），使用 MemoryManager 的默认 LLM。

    Returns:
        str: 执行结果摘要（写入 task_run_logs.output）
    """
    memory_manager = getattr(hermes_engine, "memory_manager", None)
    if memory_manager is None:
        return "memory_manager 未注入，跳过 skill 演进"

    llm = getattr(memory_manager, "_default_llm", None)
    if llm is None:
        return "LLM 未配置，跳过 skill 演进"

    from app.skills.evolver import skill_evolver
    results = await skill_evolver.evolve_all_agents(llm, force=False)

    if not results:
        return "无 code-type agent，跳过 skill 演进"

    generated = sum(1 for r in results if r.get("action") == "generate" and r.get("success"))
    optimized = sum(1 for r in results if r.get("action") == "optimize" and r.get("success"))
    skipped   = sum(1 for r in results if r.get("action") == "skip")
    failed    = sum(1 for r in results if not r.get("success") and r.get("action") != "skip")

    today_str = date.today().isoformat()
    summary = (
        f"Skill 演进 {today_str}：共 {len(results)} 个 agent | "
        f"新生成 {generated} | 优化 {optimized} | 跳过 {skipped} | 失败 {failed}"
    )
    logger.info("[SystemTasks] %s", summary)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# 注册入口
# ══════════════════════════════════════════════════════════════════════════════

async def register_system_tasks(scheduler) -> None:
    """注册系统级定时任务。在 lifespan 中 TaskScheduler.start() 之后调用。

    1. 向调度器注册 handler 函数（handler 通过 agent_name 索引）
    2. 在 MySQL 中 upsert 两条系统 cron 任务（已存在则跳过，确保幂等）

    月度归档 cron：``0 23 28,29,30,31 * *``
      — 每月 28~31 日 23:00 UTC 触发；handler 内判断是否为月末，非月末跳过。

    年度归档 cron：``0 1 31 12 *``
      — 每年 12 月 31 日 01:00 UTC 触发。
    """
    # ── 1. 注册 handler ───────────────────────────────────────────────────────
    scheduler.register_system_handler(_MONTHLY_HANDLER,        monthly_archive_handler)
    scheduler.register_system_handler(_YEARLY_HANDLER,         yearly_archive_handler)
    scheduler.register_system_handler(_FILE_CLEANUP_HANDLER,   file_cleanup_handler)
    scheduler.register_system_handler(_SKILL_EVOLUTION_HANDLER, skill_evolution_handler)
    logger.info("[SystemTasks] 系统 handler 注册完成")

    # ── 2. upsert 系统 cron 任务（幂等，重复启动不重复创建）────────────────────
    from app.scheduler.store import task_store

    monthly_task = ScheduledTask(
        task_id      = _MONTHLY_TASK_ID,
        user_id      = _SYS_USER_ID,
        name         = "月度记忆归档（系统）",
        task_type    = TaskType.CRON,
        action_type  = ActionType.SYSTEM,
        cron_expr    = "0 23 28,29,30,31 * *",   # UTC 23:00，28~31 日触发
        agent_name   = _MONTHLY_HANDLER,
        notify_on_done = False,                   # 系统任务不推送用户通知
        max_retries  = 1,
    )

    yearly_task = ScheduledTask(
        task_id      = _YEARLY_TASK_ID,
        user_id      = _SYS_USER_ID,
        name         = "年度记忆归档（系统）",
        task_type    = TaskType.CRON,
        action_type  = ActionType.SYSTEM,
        cron_expr    = "0 1 31 12 *",             # UTC 01:00，12 月 31 日触发
        agent_name   = _YEARLY_HANDLER,
        notify_on_done = False,
        max_retries  = 1,
    )

    file_cleanup_task = ScheduledTask(
        task_id      = _FILE_CLEANUP_TASK_ID,
        user_id      = _SYS_USER_ID,
        name         = "用户文件定期清理（系统）",
        task_type    = TaskType.CRON,
        action_type  = ActionType.SYSTEM,
        cron_expr    = "0 2 * * *",               # UTC 02:00 每日触发
        agent_name   = _FILE_CLEANUP_HANDLER,
        notify_on_done = False,
        max_retries  = 1,
    )

    skill_evolution_task = ScheduledTask(
        task_id      = _SKILL_EVOLUTION_TASK_ID,
        user_id      = _SYS_USER_ID,
        name         = "Agent Skill 自演进（系统）",
        task_type    = TaskType.CRON,
        action_type  = ActionType.SYSTEM,
        cron_expr    = "0 3 * * *",               # UTC 03:00 每日触发（低峰期）
        agent_name   = _SKILL_EVOLUTION_HANDLER,
        notify_on_done = False,
        max_retries  = 1,
    )

    for sys_task in (monthly_task, yearly_task, file_cleanup_task, skill_evolution_task):
        try:
            existing = await task_store.get_task(sys_task.task_id)
            if existing is None:
                # 首次启动：写入并计算首次触发时间
                from app.scheduler.runner import compute_next_run
                sys_task.next_run_at = compute_next_run(sys_task)
                await task_store.save_task(sys_task)
                logger.info(
                    "[SystemTasks] 创建系统任务: %s next_run=%s",
                    sys_task.name, sys_task.next_run_at,
                )
            else:
                logger.debug("[SystemTasks] 系统任务已存在，跳过: %s", sys_task.name)
        except Exception as e:
            logger.warning("[SystemTasks] upsert 系统任务失败 %s: %s", sys_task.name, e)
