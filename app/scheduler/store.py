"""
【模块说明】定时任务存储层（Store）— 把任务信息"存进"和"取出"数据库

调度器运行时需要知道：哪些任务到时间了要执行？执行完后怎么更新状态？
这个模块就负责对接 MySQL 数据库，完成这些"读写账本"的操作。

【两张数据库表】
  scheduled_tasks  — 存所有定时任务的配置和状态
  task_run_logs    — 存每次执行的结果记录（什么时候跑的、成功了没有、输出了什么）

【主要操作】
  save_task()        — 创建或更新一条任务（新建 + 编辑都用这个）
  get_task()         — 按 ID 查询一条任务
  list_user_tasks()  — 查询某用户的所有任务
  list_due_tasks()   — 查询"当前时间到期"的待执行任务（调度器每 30 秒调一次）
  update_task_status() — 执行完任务后更新状态（next_run_at、run_count等）
  delete_task()      — 取消任务（软删除，改状态为 cancelled）
  save_run_log()     — 记录一次执行结果
  list_run_logs()    — 查询某任务的历史执行记录

定时任务持久化 — MySQL CRUD。

建表 DDL（首次启动自动执行）：

  CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id       VARCHAR(36)  PRIMARY KEY,
    user_id       VARCHAR(64)  NOT NULL,
    name          VARCHAR(128) NOT NULL,
    task_type     VARCHAR(16)  NOT NULL,
    action_type   VARCHAR(16)  NOT NULL,
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    cron_expr     VARCHAR(64),
    hour          TINYINT      NOT NULL DEFAULT 9,
    minute        TINYINT      NOT NULL DEFAULT 0,
    weekday       TINYINT,
    day_of_month  TINYINT,
    run_at        DATETIME,
    reminder_text TEXT,
    agent_name    VARCHAR(64),
    agent_prompt  TEXT,
    notify_on_done TINYINT(1)  NOT NULL DEFAULT 1,
    run_count     INT          NOT NULL DEFAULT 0,
    max_retries   INT          NOT NULL DEFAULT 3,
    last_run_at   DATETIME,
    next_run_at   DATETIME,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_status (user_id, status),
    INDEX idx_next_run    (next_run_at, status)
  );

  CREATE TABLE IF NOT EXISTS task_run_logs (
    log_id      VARCHAR(36)  PRIMARY KEY,
    task_id     VARCHAR(36)  NOT NULL,
    user_id     VARCHAR(64)  NOT NULL,
    started_at  DATETIME     NOT NULL,
    finished_at DATETIME,
    success     TINYINT(1)   NOT NULL DEFAULT 0,
    output      TEXT,
    error       TEXT,
    INDEX idx_task (task_id),
    INDEX idx_user (user_id)
  );
"""

import logging
from datetime import datetime
from typing import List, Optional

from app.database.pool import get_connection, release_connection
from app.scheduler.models import (
    ActionType, ScheduledTask, TaskRunLog, TaskStatus, TaskType,
)

logger = logging.getLogger(__name__)

_CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id       VARCHAR(36)   PRIMARY KEY,
    user_id       VARCHAR(64)   NOT NULL,
    name          VARCHAR(128)  NOT NULL,
    task_type     VARCHAR(16)   NOT NULL,
    action_type   VARCHAR(16)   NOT NULL,
    status        VARCHAR(16)   NOT NULL DEFAULT 'active',
    cron_expr     VARCHAR(64)   DEFAULT NULL,
    hour          TINYINT       NOT NULL DEFAULT 9,
    minute        TINYINT       NOT NULL DEFAULT 0,
    weekday       TINYINT       DEFAULT NULL,
    day_of_month  TINYINT       DEFAULT NULL,
    run_at        DATETIME      DEFAULT NULL,
    reminder_text TEXT          DEFAULT NULL,
    agent_name    VARCHAR(64)   DEFAULT NULL,
    agent_prompt  TEXT          DEFAULT NULL,
    notify_on_done TINYINT(1)   NOT NULL DEFAULT 1,
    run_count     INT           NOT NULL DEFAULT 0,
    max_retries   INT           NOT NULL DEFAULT 3,
    last_run_at   DATETIME      DEFAULT NULL,
    next_run_at   DATETIME      DEFAULT NULL,
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_status (user_id, status),
    INDEX idx_next_run    (next_run_at, status)
)
"""

_CREATE_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS task_run_logs (
    log_id      VARCHAR(36)  PRIMARY KEY,
    task_id     VARCHAR(36)  NOT NULL,
    user_id     VARCHAR(64)  NOT NULL,
    started_at  DATETIME     NOT NULL,
    finished_at DATETIME     DEFAULT NULL,
    success     TINYINT(1)   NOT NULL DEFAULT 0,
    output      TEXT         DEFAULT NULL,
    error       TEXT         DEFAULT NULL,
    INDEX idx_task (task_id),
    INDEX idx_user (user_id)
)
"""


def _row_to_task(row) -> ScheduledTask:
    """将 DataFrame 行 / dict 转换为 ScheduledTask。"""
    if hasattr(row, "to_dict"):
        d = row.to_dict()
    else:
        d = dict(row)

    def _dt(v):
        if v is None or (isinstance(v, float) and v != v):  # NaN
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v))
        except Exception:
            return None

    def _int_or_none(v):
        try:
            return int(v) if v is not None and str(v) != "nan" else None
        except Exception:
            return None

    return ScheduledTask(
        task_id       = str(d["task_id"]),
        user_id       = str(d["user_id"]),
        name          = str(d["name"]),
        task_type     = TaskType(d["task_type"]),
        action_type   = ActionType(d["action_type"]),
        status        = TaskStatus(d.get("status", "active")),
        cron_expr     = d.get("cron_expr") or None,
        hour          = int(d.get("hour") or 9),
        minute        = int(d.get("minute") or 0),
        weekday       = _int_or_none(d.get("weekday")),
        day_of_month  = _int_or_none(d.get("day_of_month")),
        run_at        = _dt(d.get("run_at")),
        reminder_text = d.get("reminder_text") or None,
        agent_name    = d.get("agent_name") or None,
        agent_prompt  = d.get("agent_prompt") or None,
        notify_on_done= bool(d.get("notify_on_done", True)),
        run_count     = int(d.get("run_count") or 0),
        max_retries   = int(d.get("max_retries") or 3),
        last_run_at   = _dt(d.get("last_run_at")),
        next_run_at   = _dt(d.get("next_run_at")),
        created_at    = _dt(d.get("created_at")),
        updated_at    = _dt(d.get("updated_at")),
    )


class TaskStore:
    """定时任务 MySQL 存储层（无状态，可多实例）。"""

    async def ensure_tables(self) -> None:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            if conn:
                await conn.execute_raw(_CREATE_TASKS_TABLE, {})
                await conn.execute_raw(_CREATE_LOGS_TABLE, {})
        except Exception as e:
            logger.warning("创建定时任务表失败: %s", e)
        finally:
            if conn:
                await release_connection("mysql", conn)

    # ── 任务 CRUD ──────────────────────────────────────────────────────────────

    async def save_task(self, task: ScheduledTask) -> bool:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                INSERT INTO scheduled_tasks
                  (task_id, user_id, name, task_type, action_type, status,
                   cron_expr, hour, minute, weekday, day_of_month, run_at,
                   reminder_text, agent_name, agent_prompt, notify_on_done,
                   run_count, max_retries, last_run_at, next_run_at, created_at, updated_at)
                VALUES
                  (:task_id, :user_id, :name, :task_type, :action_type, :status,
                   :cron_expr, :hour, :minute, :weekday, :day_of_month, :run_at,
                   :reminder_text, :agent_name, :agent_prompt, :notify_on_done,
                   :run_count, :max_retries, :last_run_at, :next_run_at, :created_at, :updated_at)
                ON DUPLICATE KEY UPDATE
                  name=VALUES(name), status=VALUES(status), cron_expr=VALUES(cron_expr),
                  hour=VALUES(hour), minute=VALUES(minute), weekday=VALUES(weekday),
                  day_of_month=VALUES(day_of_month), run_at=VALUES(run_at),
                  reminder_text=VALUES(reminder_text), agent_name=VALUES(agent_name),
                  agent_prompt=VALUES(agent_prompt), notify_on_done=VALUES(notify_on_done),
                  run_count=VALUES(run_count), last_run_at=VALUES(last_run_at),
                  next_run_at=VALUES(next_run_at), updated_at=VALUES(updated_at)
                """,
                {
                    "task_id":       task.task_id,
                    "user_id":       task.user_id,
                    "name":          task.name,
                    "task_type":     task.task_type.value,
                    "action_type":   task.action_type.value,
                    "status":        task.status.value,
                    "cron_expr":     task.cron_expr,
                    "hour":          task.hour,
                    "minute":        task.minute,
                    "weekday":       task.weekday,
                    "day_of_month":  task.day_of_month,
                    "run_at":        task.run_at,
                    "reminder_text": task.reminder_text,
                    "agent_name":    task.agent_name,
                    "agent_prompt":  task.agent_prompt,
                    "notify_on_done": int(task.notify_on_done),
                    "run_count":     task.run_count,
                    "max_retries":   task.max_retries,
                    "last_run_at":   task.last_run_at,
                    "next_run_at":   task.next_run_at,
                    "created_at":    task.created_at or datetime.utcnow(),
                    "updated_at":    datetime.utcnow(),
                },
            )
            return True
        except Exception as e:
            logger.error("保存定时任务失败 task=%s: %s", task.task_id[:8], e)
            return False
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                "SELECT * FROM scheduled_tasks WHERE task_id = :tid",
                {"tid": task_id},
            )
            if rows is None or len(rows) == 0:
                return None
            return _row_to_task(rows.iloc[0])
        except Exception as e:
            logger.error("查询定时任务失败 task=%s: %s", task_id[:8], e)
            return None
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def list_user_tasks(
        self,
        user_id: str,
        status: Optional[str] = None,
    ) -> List[ScheduledTask]:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            if status:
                rows = await conn.execute_raw(
                    "SELECT * FROM scheduled_tasks WHERE user_id=:uid AND status=:st ORDER BY created_at DESC",
                    {"uid": user_id, "st": status},
                )
            else:
                rows = await conn.execute_raw(
                    "SELECT * FROM scheduled_tasks WHERE user_id=:uid ORDER BY created_at DESC",
                    {"uid": user_id},
                )
            if rows is None or len(rows) == 0:
                return []
            return [_row_to_task(rows.iloc[i]) for i in range(len(rows))]
        except Exception as e:
            logger.error("列出用户定时任务失败 user=%s: %s", user_id, e)
            return []
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def list_due_tasks(self, now: datetime) -> List[ScheduledTask]:
        """查询所有到期待执行的活跃任务。"""
        conn = None
        try:
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= :now
                ORDER BY next_run_at ASC
                """,
                {"now": now},
            )
            if rows is None or len(rows) == 0:
                return []
            return [_row_to_task(rows.iloc[i]) for i in range(len(rows))]
        except Exception as e:
            logger.error("查询到期任务失败: %s", e)
            return []
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def update_task_status(
        self,
        task_id:    str,
        status:     str,
        next_run_at: Optional[datetime] = None,
        last_run_at: Optional[datetime] = None,
        run_count_delta: int = 0,
    ) -> None:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                UPDATE scheduled_tasks
                SET status      = :status,
                    next_run_at = :next_run_at,
                    last_run_at = COALESCE(:last_run_at, last_run_at),
                    run_count   = run_count + :delta,
                    updated_at  = :now
                WHERE task_id = :tid
                """,
                {
                    "status":       status,
                    "next_run_at":  next_run_at,
                    "last_run_at":  last_run_at,
                    "delta":        run_count_delta,
                    "now":          datetime.utcnow(),
                    "tid":          task_id,
                },
            )
        except Exception as e:
            logger.error("更新定时任务状态失败 task=%s: %s", task_id[:8], e)
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def delete_task(self, task_id: str, user_id: str) -> bool:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                "UPDATE scheduled_tasks SET status='cancelled', updated_at=:now WHERE task_id=:tid AND user_id=:uid",
                {"now": datetime.utcnow(), "tid": task_id, "uid": user_id},
            )
            return True
        except Exception as e:
            logger.error("取消定时任务失败 task=%s: %s", task_id[:8], e)
            return False
        finally:
            if conn:
                await release_connection("mysql", conn)

    # ── 执行日志 ───────────────────────────────────────────────────────────────

    async def save_run_log(self, log: TaskRunLog) -> None:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                INSERT INTO task_run_logs
                  (log_id, task_id, user_id, started_at, finished_at, success, output, error)
                VALUES
                  (:log_id, :task_id, :user_id, :started_at, :finished_at, :success, :output, :error)
                """,
                {
                    "log_id":      log.log_id,
                    "task_id":     log.task_id,
                    "user_id":     log.user_id,
                    "started_at":  log.started_at,
                    "finished_at": log.finished_at,
                    "success":     int(log.success),
                    "output":      log.output,
                    "error":       log.error,
                },
            )
        except Exception as e:
            logger.error("保存任务执行日志失败 task=%s: %s", log.task_id[:8], e)
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def list_run_logs(
        self,
        task_id: str,
        limit:   int = 20,
    ) -> list:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                "SELECT * FROM task_run_logs WHERE task_id=:tid ORDER BY started_at DESC LIMIT :lim",
                {"tid": task_id, "lim": limit},
            )
            if rows is None or len(rows) == 0:
                return []
            cols = list(rows.columns)
            return [dict(zip(cols, rows.iloc[i].tolist())) for i in range(len(rows))]
        except Exception as e:
            logger.error("查询任务执行日志失败 task=%s: %s", task_id[:8], e)
            return []
        finally:
            if conn:
                await release_connection("mysql", conn)


task_store = TaskStore()
