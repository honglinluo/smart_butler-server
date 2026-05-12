"""
【模块说明】定时任务调度器（Runner）— 系统的"闹钟大脑"

用户可以给 AI 设置定时任务，比如"每天早上 9 点提醒我开会"或"每周一让 AI 自动整理周报"。
这个模块就是负责在正确的时间触发这些任务的引擎。

【它是怎么工作的？】
  服务器启动后，这里会开启一个后台循环：
  1. 每 30 秒醒来一次
  2. 查询数据库：有没有"应该现在执行"的任务？
  3. 有的话，立刻异步执行（不会卡住其他任务）
  4. 执行完后记录结果，计算下一次触发时间，发推送通知给用户

【支持的任务类型】
  - 一次性任务（only once）：指定某个时间执行一次
  - 每日（daily）：每天固定时间执行
  - 每周（weekly）：每周固定某天执行
  - 每月（monthly）：每月固定某日执行
  - 工作日（workday）：只在工作日执行，自动识别法定节假日
  - 周末/节假日（weekend）：只在休息日执行
  - Cron 表达式（cron）：高级用户可以用"0 9 * * 1-5"这类表达式精确配置

【三种动作类型】
  - REMINDER：纯提醒，不调用 AI，只推送一条消息给用户
  - AGENT：让指定的 AI Agent 自动执行一段任务
  - SYSTEM：系统内部维护任务（用户不可见），如月度记忆归档

异步任务调度器 — asyncio 驱动的定时任务执行引擎。

设计要点：
  - 单一后台协程循环，每 30 秒检查一次到期任务
  - 每个到期任务通过 asyncio.create_task() 异步执行，不阻塞调度循环
  - 执行结果写入 task_run_logs，推送 Redis 通知
  - 执行后计算下一次触发时间并更新 MySQL
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, Optional

from app.scheduler.holiday import is_workday, is_weekend_or_holiday
from app.scheduler.models import ActionType, ScheduledTask, TaskRunLog, TaskStatus, TaskType
from app.scheduler.notifier import (
    make_agent_done_payload, make_reminder_payload, push_notification,
)
from app.scheduler.store import TaskStore, task_store

logger = logging.getLogger(__name__)

_TICK_SECONDS = 30  # 调度循环间隔

# ── 系统任务注册表 ─────────────────────────────────────────────────────────────
# key: task.agent_name（如 "__monthly_archive__"）
# value: async callable(task, hermes_engine) -> str  （返回执行摘要文本）
_SYSTEM_HANDLERS: Dict[str, Callable[..., Coroutine[Any, Any, str]]] = {}


def register_system_handler(
    name: str,
    handler: Callable[..., Coroutine[Any, Any, str]],
) -> None:
    """注册系统任务处理函数。

    在服务启动阶段（lifespan）调用，调度器执行 ActionType.SYSTEM 任务时通过
    task.agent_name 查找对应 handler。

    Args:
        name:    与 ScheduledTask.agent_name 匹配的唯一标识
        handler: async (task, hermes_engine) -> str，返回执行结果摘要
    """
    _SYSTEM_HANDLERS[name] = handler
    logger.debug("已注册系统任务 handler: %s", name)


# ── next_run_at 计算 ──────────────────────────────────────────────────────────

def compute_next_run(task: ScheduledTask, after: Optional[datetime] = None) -> Optional[datetime]:
    """计算任务的下一次执行时间（UTC）。

    返回 None 表示任务不再触发（一次性任务已执行，或无效配置）。
    """
    now = after or datetime.utcnow()
    t   = task.task_type

    if t == TaskType.ONCE:
        # 一次性：仅在首次计算时返回 run_at，执行后不再触发
        if task.run_count == 0 and task.run_at and task.run_at > now:
            return task.run_at
        return None

    if t == TaskType.CRON:
        return _next_cron(task.cron_expr, now)

    # 以下类型都按"今天或明天的 H:M"来推算
    h, m = task.hour, task.minute

    if t == TaskType.DAILY:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if t == TaskType.WEEKLY:
        target_wd = task.weekday if task.weekday is not None else 0
        days_ahead = (target_wd - now.weekday()) % 7
        if days_ahead == 0:
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= now:
                days_ahead = 7
        candidate = (now + timedelta(days=days_ahead)).replace(
            hour=h, minute=m, second=0, microsecond=0
        )
        return candidate

    if t == TaskType.MONTHLY:
        dom = task.day_of_month if task.day_of_month else 1
        candidate = now.replace(day=min(dom, _days_in_month(now.year, now.month)),
                                 hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            # 移到下个月
            y, mo = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
            candidate = datetime(y, mo, min(dom, _days_in_month(y, mo)), h, m, 0)
        return candidate

    if t == TaskType.WORKDAY:
        return _next_weekday_match(now, h, m, workday=True)

    if t == TaskType.WEEKEND:
        return _next_weekday_match(now, h, m, workday=False)

    return None


def _next_weekday_match(
    now: datetime,
    hour: int,
    minute: int,
    workday: bool,
    max_days: int = 14,
) -> Optional[datetime]:
    """找到接下来满足 workday/weekend 条件的最近时间点。"""
    check = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if check <= now:
        check += timedelta(days=1)
    for _ in range(max_days):
        if workday and is_workday(check):
            return check
        if not workday and is_weekend_or_holiday(check):
            return check
        check += timedelta(days=1)
    return None


def _days_in_month(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def _next_cron(expr: Optional[str], after: datetime) -> Optional[datetime]:
    """简易 cron 解析（分 时 日 月 周），返回下一次触发时间。"""
    if not expr:
        return None
    try:
        parts = expr.strip().split()
        if len(parts) != 5:
            return None
        min_p, hour_p, dom_p, mon_p, dow_p = parts

        def _matches(val: int, field: str) -> bool:
            if field == "*":
                return True
            for part in field.split(","):
                if "/" in part:
                    rng, step = part.split("/", 1)
                    start = 0 if rng == "*" else int(rng.split("-")[0])
                    if (val - start) % int(step) == 0:
                        return True
                elif "-" in part:
                    lo, hi = map(int, part.split("-"))
                    if lo <= val <= hi:
                        return True
                elif int(part) == val:
                    return True
            return False

        candidate = after + timedelta(minutes=1)
        candidate = candidate.replace(second=0, microsecond=0)
        for _ in range(366 * 24 * 60):  # 最多搜索一年
            if (
                _matches(candidate.month,   mon_p)
                and _matches(candidate.day,    dom_p)
                and _matches(candidate.weekday(), dow_p)
                and _matches(candidate.hour,   hour_p)
                and _matches(candidate.minute, min_p)
            ):
                return candidate
            candidate += timedelta(minutes=1)
    except Exception as e:
        logger.warning("解析 cron 表达式失败 %r: %s", expr, e)
    return None


# ── 任务执行 ──────────────────────────────────────────────────────────────────

async def _execute_task(task: ScheduledTask, hermes_engine=None) -> None:
    """执行单条定时任务，更新状态并推送通知。"""
    run_log = TaskRunLog.new(task.task_id, task.user_id)
    success = False
    output  = ""
    error   = ""

    try:
        if task.action_type == ActionType.REMINDER:
            output  = task.reminder_text or task.name
            success = True

        elif task.action_type == ActionType.AGENT:
            if hermes_engine is None:
                raise RuntimeError("Hermes 引擎未初始化，无法调用 Agent")
            response = await hermes_engine.process_user_input(
                user_id    = task.user_id,
                user_input = task.agent_prompt or task.name,
                context    = {"scheduled_task_id": task.task_id, "task_name": task.name},
                agent_name = task.agent_name,
            )
            output  = response or ""
            success = True

        elif task.action_type == ActionType.SYSTEM:
            handler_name = task.agent_name or ""
            handler = _SYSTEM_HANDLERS.get(handler_name)
            if handler is None:
                raise RuntimeError(f"系统任务 handler 未注册: {handler_name!r}")
            output  = await handler(task, hermes_engine) or ""
            success = True

    except Exception as e:
        error = str(e)
        logger.error("定时任务执行失败 task=%s user=%s: %s", task.task_id[:8], task.user_id, e)

    run_log.finish(success=success, output=output, error=error)
    await task_store.save_run_log(run_log)

    # 计算下次触发时间
    next_run = compute_next_run(task) if success else None
    # 一次性任务或周期任务执行失败超次数则标记完成/失败
    new_status = task.status.value
    if task.task_type == TaskType.ONCE:
        new_status = TaskStatus.DONE.value if success else TaskStatus.FAILED.value
        next_run = None
    elif not success and task.run_count + 1 >= task.max_retries:
        new_status = TaskStatus.FAILED.value
        next_run = None

    await task_store.update_task_status(
        task_id        = task.task_id,
        status         = new_status,
        next_run_at    = next_run,
        last_run_at    = run_log.finished_at,
        run_count_delta= 1,
    )

    # 推送通知（系统任务不通知任何用户）
    if task.notify_on_done and task.action_type != ActionType.SYSTEM:
        if task.action_type == ActionType.REMINDER:
            payload = make_reminder_payload(
                task_id   = task.task_id,
                task_name = task.name,
                user_id   = task.user_id,
                text      = output,
            )
        else:
            payload = make_agent_done_payload(
                task_id    = task.task_id,
                task_name  = task.name,
                user_id    = task.user_id,
                agent_name = task.agent_name or "",
                output     = output,
                success    = success,
            )
        await push_notification(task.user_id, payload)


# ── 调度主循环 ────────────────────────────────────────────────────────────────

class TaskScheduler:
    """异步任务调度器（进程单例）。"""

    def __init__(self, store: TaskStore = task_store):
        self._store       = store
        self._task        : Optional[asyncio.Task] = None
        self._hermes      = None
        self._running     = False

    def set_hermes_engine(self, engine) -> None:
        self._hermes = engine

    def register_system_handler(
        self,
        name: str,
        handler: Callable[..., Coroutine[Any, Any, str]],
    ) -> None:
        """便捷方法：注册系统任务 handler（同时写入模块级注册表）。"""
        register_system_handler(name, handler)

    async def start(self) -> None:
        """启动调度循环（幂等）。"""
        if self._running:
            return
        await self._store.ensure_tables()
        # 启动时为所有活跃任务计算首次 next_run_at（若为 None）
        await self._init_next_runs()
        self._running = True
        self._task    = asyncio.create_task(self._loop(), name="task_scheduler")
        logger.info("✅ 定时任务调度器已启动（间隔 %ds）", _TICK_SECONDS)

    async def stop(self) -> None:
        """停止调度循环。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 定时任务调度器已停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("调度循环异常: %s", e)
            await asyncio.sleep(_TICK_SECONDS)

    async def _tick(self) -> None:
        now  = datetime.utcnow()
        due  = await self._store.list_due_tasks(now)
        for task in due:
            asyncio.create_task(
                _execute_task(task, self._hermes),
                name=f"task_{task.task_id[:8]}",
            )
            logger.debug("触发定时任务 task=%s user=%s", task.task_id[:8], task.user_id)

    async def _init_next_runs(self) -> None:
        """为 next_run_at 为 NULL 的活跃任务初始化首次触发时间。"""
        conn = None
        try:
            from app.database.pool import get_connection, release_connection
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                "SELECT * FROM scheduled_tasks WHERE status='active' AND next_run_at IS NULL",
                {},
            )
            if rows is None or len(rows) == 0:
                return
            from app.scheduler.store import _row_to_task
            for i in range(len(rows)):
                task = _row_to_task(rows.iloc[i])
                nxt  = compute_next_run(task)
                if nxt:
                    await self._store.update_task_status(
                        task_id    = task.task_id,
                        status     = "active",
                        next_run_at= nxt,
                    )
        except Exception as e:
            logger.warning("初始化 next_run_at 失败: %s", e)
        finally:
            if conn:
                from app.database.pool import release_connection
                await release_connection("mysql", conn)


# 全局单例
scheduler = TaskScheduler()
