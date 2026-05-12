"""
【模块说明】定时任务包（Scheduler）— 让系统能在特定时间自动执行任务

这个包提供了完整的定时任务能力，让用户可以设置"每天早上 9 点提醒我"或
"每周一让 AI 自动整理周报"这类定时自动化任务。

外部代码通过这里统一导入所需的调度器组件：
  scheduler     — 全局调度器单例（应用启动时运行，每 30 秒检查到期任务）
  task_store    — 任务存储层（对 MySQL 的 CRUD 操作）
  ScheduledTask — 定时任务数据结构
  push_notification / pop_notifications — 任务完成通知的推送和获取

定时任务模块。
"""

from app.scheduler.models import (
    ActionType, ScheduledTask, TaskRunLog, TaskStatus, TaskType,
)
from app.scheduler.runner import TaskScheduler, compute_next_run, scheduler
from app.scheduler.store import TaskStore, task_store
from app.scheduler.notifier import push_notification, pop_notifications

__all__ = [
    "ActionType", "ScheduledTask", "TaskRunLog", "TaskStatus", "TaskType",
    "TaskScheduler", "compute_next_run", "scheduler",
    "TaskStore", "task_store",
    "push_notification", "pop_notifications",
]
