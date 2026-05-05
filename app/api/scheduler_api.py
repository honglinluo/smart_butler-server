"""定时任务 API — 创建、查询、取消定时任务，以及通知推送接口。

端点概览：
  GET    /scheduler/tasks                  列出当前用户的所有定时任务
  POST   /scheduler/tasks                  创建定时任务
  GET    /scheduler/tasks/{task_id}        查询单条任务详情
  DELETE /scheduler/tasks/{task_id}        取消定时任务
  GET    /scheduler/tasks/{task_id}/logs   查看执行日志
  GET    /scheduler/notifications          轮询/消费待读通知（LPOP）
  GET    /scheduler/notifications/stream   SSE 实时通知流
  GET    /scheduler/notifications/peek     预览通知（不消费）
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.dependencies import get_current_user
from app.scheduler.models import (
    ActionType, ScheduledTask, TaskStatus, TaskType,
)
from app.scheduler.notifier import (
    sse_notification_stream, pop_notifications, peek_notifications,
)
from app.scheduler.runner import compute_next_run, scheduler
from app.scheduler.store import task_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["Scheduler"])


# ══════════════════════════════════════════════════════════════════
# 请求模型
# ══════════════════════════════════════════════════════════════════

class TaskCreateRequest(BaseModel):
    name:        str            = Field(..., min_length=1, max_length=128, description="任务名称")
    task_type:   TaskType       = Field(..., description="触发类型")
    action_type: ActionType     = Field(..., description="执行动作")

    # 触发配置
    cron_expr:    Optional[str] = Field(None, description="cron 表达式（task_type=cron 时必填），格式：分 时 日 月 周")
    hour:         int           = Field(9,  ge=0, le=23, description="触发小时（本地时区，UTC+8）")
    minute:       int           = Field(0,  ge=0, le=59, description="触发分钟")
    weekday:      Optional[int] = Field(None, ge=0, le=6, description="星期几（weekly 时必填，0=周一）")
    day_of_month: Optional[int] = Field(None, ge=1, le=31, description="每月第几天（monthly 时必填）")
    run_at:       Optional[datetime] = Field(None, description="精确执行时间（once 时必填，ISO 格式）")

    # 动作配置
    reminder_text: Optional[str] = Field(None, max_length=2048, description="提醒文本（action_type=reminder 时必填）")
    agent_name:    Optional[str] = Field(None, max_length=64,   description="Agent 名称（action_type=agent 时必填）")
    agent_prompt:  Optional[str] = Field(None, max_length=4096, description="Agent 任务描述")

    # 通知
    notify_on_done: bool = Field(True, description="任务完成后是否发送通知")

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def validate_logic(self) -> "TaskCreateRequest":
        t = self.task_type
        a = self.action_type

        if t == TaskType.CRON and not self.cron_expr:
            raise ValueError("task_type=cron 时 cron_expr 不能为空")
        if t == TaskType.WEEKLY and self.weekday is None:
            raise ValueError("task_type=weekly 时 weekday 不能为空（0=周一…6=周日）")
        if t == TaskType.MONTHLY and self.day_of_month is None:
            raise ValueError("task_type=monthly 时 day_of_month 不能为空（1-31）")
        if t == TaskType.ONCE and self.run_at is None:
            raise ValueError("task_type=once 时 run_at 不能为空")

        if a == ActionType.REMINDER and not self.reminder_text:
            raise ValueError("action_type=reminder 时 reminder_text 不能为空")
        if a == ActionType.AGENT and not self.agent_name:
            raise ValueError("action_type=agent 时 agent_name 不能为空")

        return self


# ══════════════════════════════════════════════════════════════════
# 辅助：将请求时间（CST UTC+8）转换为 UTC
# ══════════════════════════════════════════════════════════════════

def _cst_hour_to_utc(hour: int, minute: int):
    """前端传入 CST（UTC+8）小时，转为 UTC 用于内部存储。"""
    utc_hour = (hour - 8) % 24
    return utc_hour, minute


# ══════════════════════════════════════════════════════════════════
# 端点实现
# ══════════════════════════════════════════════════════════════════

@router.get(
    "/tasks",
    summary="列出当前用户的定时任务",
    response_model=dict,
)
async def list_tasks(
    current_user: dict = Depends(get_current_user),
    status_filter: Optional[str] = Query(None, alias="status", description="按状态过滤：active/paused/done/cancelled/failed"),
):
    user_id = current_user["user_id"]
    tasks   = await task_store.list_user_tasks(user_id, status=status_filter)
    return {
        "user_id": user_id,
        "total":   len(tasks),
        "tasks":   [t.to_dict() for t in tasks],
    }


@router.post(
    "/tasks",
    summary="创建定时任务",
    status_code=status.HTTP_201_CREATED,
    response_model=dict,
)
async def create_task(
    body:         TaskCreateRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id  = current_user["user_id"]
    utc_hour, utc_minute = _cst_hour_to_utc(body.hour, body.minute)

    task = ScheduledTask.new(
        user_id      = user_id,
        name         = body.name,
        task_type    = body.task_type,
        action_type  = body.action_type,
        cron_expr    = body.cron_expr,
        hour         = utc_hour,
        minute       = utc_minute,
        weekday      = body.weekday,
        day_of_month = body.day_of_month,
        run_at       = body.run_at,
        reminder_text= body.reminder_text,
        agent_name   = body.agent_name,
        agent_prompt = body.agent_prompt,
        notify_on_done= body.notify_on_done,
    )

    # 计算首次触发时间
    nxt = compute_next_run(task)
    if nxt is None and body.task_type != TaskType.ONCE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无法计算首次触发时间，请检查任务配置",
        )
    if body.task_type == TaskType.ONCE and body.run_at and body.run_at <= datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="一次性任务的执行时间必须在当前时间之后",
        )
    task.next_run_at = nxt

    saved = await task_store.save_task(task)
    if not saved:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="保存定时任务失败，请稍后重试",
        )

    logger.info("创建定时任务 task=%s user=%s type=%s next=%s",
                task.task_id[:8], user_id, task.task_type.value, nxt)
    return {
        "task_id":   task.task_id,
        "next_run_at": nxt.isoformat() if nxt else None,
        "task":      task.to_dict(),
    }


@router.get(
    "/tasks/{task_id}",
    summary="查询单条定时任务",
    response_model=dict,
)
async def get_task(
    task_id:      str,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    task    = await task_store.get_task(task_id)
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.to_dict()


@router.delete(
    "/tasks/{task_id}",
    summary="取消定时任务",
    response_model=dict,
)
async def cancel_task(
    task_id:      str,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    task    = await task_store.get_task(task_id)
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in (TaskStatus.CANCELLED, TaskStatus.DONE):
        raise HTTPException(status_code=400, detail=f"任务已处于 {task.status.value} 状态，无需取消")

    await task_store.delete_task(task_id, user_id)
    return {"task_id": task_id, "status": "cancelled"}


@router.get(
    "/tasks/{task_id}/logs",
    summary="查询任务执行日志",
    response_model=dict,
)
async def get_task_logs(
    task_id:      str,
    current_user: dict = Depends(get_current_user),
    limit:        int  = Query(20, ge=1, le=100),
):
    user_id = current_user["user_id"]
    task    = await task_store.get_task(task_id)
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    logs = await task_store.list_run_logs(task_id, limit=limit)
    return {"task_id": task_id, "logs": logs}


# ══════════════════════════════════════════════════════════════════
# 通知接口
# ══════════════════════════════════════════════════════════════════

@router.get(
    "/notifications",
    summary="消费待读通知（LPOP，读后删除）",
    response_model=dict,
)
async def consume_notifications(
    current_user: dict = Depends(get_current_user),
    max_count:    int  = Query(20, ge=1, le=100),
):
    user_id = current_user["user_id"]
    msgs    = await pop_notifications(user_id, max_count=max_count)
    return {"user_id": user_id, "count": len(msgs), "notifications": msgs}


@router.get(
    "/notifications/peek",
    summary="预览通知（不消费）",
    response_model=dict,
)
async def preview_notifications(
    current_user: dict = Depends(get_current_user),
    count:        int  = Query(20, ge=1, le=100),
):
    user_id = current_user["user_id"]
    msgs    = await peek_notifications(user_id, count=count)
    return {"user_id": user_id, "count": len(msgs), "notifications": msgs}


@router.get(
    "/notifications/stream",
    summary="SSE 实时通知流",
    description=(
        "Server-Sent Events 长连接，轮询 Redis 通知队列并实时推送。\n\n"
        "事件类型：\n"
        "- `event: notification` — 有新通知（data 为 JSON）\n"
        "- `: ping` — 保活心跳（无数据行）\n"
        "- `event: close` — 空闲超时，服务端主动关闭"
    ),
)
async def stream_notifications(
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    return StreamingResponse(
        sse_notification_stream(user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
