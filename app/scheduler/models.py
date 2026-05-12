"""
【模块说明】定时任务数据模型 — 描述"一个定时任务长什么样"

这里定义了系统中定时任务的数据结构：一个任务包含哪些信息、有哪些状态、
支持哪些触发方式。可以把它理解为定时任务的"户口本"——记录每条任务的全部属性。

【三个关键枚举】
  TaskType   — 任务"什么时候触发"：一次性/每天/每周/每月/工作日/周末/cron表达式
  ActionType — 任务"执行什么动作"：发提醒消息 / 调用 AI Agent / 系统内部维护
  TaskStatus — 任务当前状态：活跃 / 暂停 / 已完成 / 已取消 / 失败

【两个主要数据结构】
  ScheduledTask  — 一条定时任务的完整信息（名字、触发时间、执行内容等）
  TaskRunLog     — 每次实际执行的记录（开始时间、结果、报错信息）

定时任务数据模型 — 枚举、数据类定义。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskType(str, Enum):
    """任务触发类型"""
    ONCE      = "once"      # 一次性任务
    DAILY     = "daily"     # 每天
    WEEKLY    = "weekly"    # 每周
    MONTHLY   = "monthly"   # 每月
    WORKDAY   = "workday"   # 工作日（排除法定节假日）
    WEEKEND   = "weekend"   # 周末 + 法定节假日
    CRON      = "cron"      # 自定义 cron 表达式


class ActionType(str, Enum):
    """任务执行动作"""
    REMINDER  = "reminder"  # 仅推送提醒消息
    AGENT     = "agent"     # 调用指定 Agent 完成任务
    SYSTEM    = "system"    # 系统内置任务（用户不可见，handler 从注册表查找）


class TaskStatus(str, Enum):
    """任务状态"""
    ACTIVE    = "active"    # 活跃（调度中）
    PAUSED    = "paused"    # 暂停
    DONE      = "done"      # 已完成（一次性任务执行后）
    CANCELLED = "cancelled" # 用户取消
    FAILED    = "failed"    # 执行失败超过上限


@dataclass
class ScheduledTask:
    """一条定时任务记录。"""
    task_id:       str
    user_id:       str
    name:          str
    task_type:     TaskType
    action_type:   ActionType
    status:        TaskStatus   = TaskStatus.ACTIVE

    # 触发配置
    cron_expr:     Optional[str] = None   # task_type=cron 时必填
    hour:          int           = 9      # 每日触发小时（0-23）
    minute:        int           = 0      # 触发分钟（0-59）
    weekday:       Optional[int] = None   # weekly: 0=Mon…6=Sun
    day_of_month:  Optional[int] = None   # monthly: 1-31
    run_at:        Optional[datetime] = None  # once: 精确执行时间

    # 动作配置
    reminder_text: Optional[str] = None  # REMINDER 动作的提醒文本
    agent_name:    Optional[str] = None  # AGENT 动作的目标 Agent
    agent_prompt:  Optional[str] = None  # AGENT 动作的任务描述

    # 通知配置
    notify_on_done: bool = True           # 任务完成后是否推送通知

    # 统计与元数据
    run_count:     int           = 0
    max_retries:   int           = 3
    last_run_at:   Optional[datetime] = None
    next_run_at:   Optional[datetime] = None
    created_at:    Optional[datetime] = None
    updated_at:    Optional[datetime] = None

    @classmethod
    def new(
        cls,
        user_id:       str,
        name:          str,
        task_type:     TaskType,
        action_type:   ActionType,
        **kwargs: Any,
    ) -> "ScheduledTask":
        now = datetime.utcnow()
        return cls(
            task_id    = str(uuid.uuid4()),
            user_id    = user_id,
            name       = name,
            task_type  = task_type,
            action_type= action_type,
            created_at = now,
            updated_at = now,
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id":       self.task_id,
            "user_id":       self.user_id,
            "name":          self.name,
            "task_type":     self.task_type.value,
            "action_type":   self.action_type.value,
            "status":        self.status.value,
            "cron_expr":     self.cron_expr,
            "hour":          self.hour,
            "minute":        self.minute,
            "weekday":       self.weekday,
            "day_of_month":  self.day_of_month,
            "run_at":        self.run_at.isoformat() if self.run_at else None,
            "reminder_text": self.reminder_text,
            "agent_name":    self.agent_name,
            "agent_prompt":  self.agent_prompt,
            "notify_on_done": self.notify_on_done,
            "run_count":     self.run_count,
            "last_run_at":   self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at":   self.next_run_at.isoformat() if self.next_run_at else None,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class TaskRunLog:
    """单次任务执行记录。"""
    log_id:       str
    task_id:      str
    user_id:      str
    started_at:   datetime
    finished_at:  Optional[datetime] = None
    success:      bool               = False
    output:       Optional[str]      = None
    error:        Optional[str]      = None

    @classmethod
    def new(cls, task_id: str, user_id: str) -> "TaskRunLog":
        return cls(
            log_id    = str(uuid.uuid4()),
            task_id   = task_id,
            user_id   = user_id,
            started_at= datetime.utcnow(),
        )

    def finish(self, success: bool, output: str = "", error: str = "") -> None:
        self.finished_at = datetime.utcnow()
        self.success     = success
        self.output      = output[:4096] if output else ""
        self.error       = error[:1024]  if error  else ""
