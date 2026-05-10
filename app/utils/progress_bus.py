"""请求级别进度事件总线

通过 ContextVar 在任意嵌套位置向 SSE 流推送结构化进度事件，
无需参数穿透。stream 端点负责设置 Queue；深层代码调用 push() 即可。

事件类型（event 字段）
----------------------
  planning     — 路由完成，返回 pipeline 计划
  agent_start  — 某个 Agent 开始执行
  step_start   — Agent 内部 L2 子步骤开始
  step_done    — Agent 内部 L2 子步骤完成
  tool_call    — 工具调用开始
  tool_result  — 工具调用成功返回
  tool_error   — 工具调用失败
  agent_done   — Agent 执行完成
  token        — 最终 LLM 输出 token 块
  done         — 全部完成
  error        — 发生错误
  cancelled    — 对话已被终止
"""

import asyncio
from contextvars import ContextVar
from typing import Any, Dict, Optional

# 每个请求独立的进度队列（None 表示当前无活跃流）
_queue_var: ContextVar[Optional[asyncio.Queue]] = ContextVar("_progress_q", default=None)


def set_queue(q: asyncio.Queue) -> None:
    """在 stream 请求开始时设置本 coroutine 链的进度队列。"""
    _queue_var.set(q)


def push(event_type: str, data: Dict[str, Any]) -> None:
    """发射一条进度事件（非阻塞；队列满或无活跃流时静默跳过）。"""
    q = _queue_var.get()
    if q is None:
        return
    try:
        q.put_nowait({"event": event_type, "data": data})
    except (asyncio.QueueFull, RuntimeError):
        pass


def close() -> None:
    """发射结束哨兵 None，通知 SSE 生成器退出读取循环。"""
    q = _queue_var.get()
    if q is None:
        return
    try:
        q.put_nowait(None)
    except (asyncio.QueueFull, RuntimeError):
        pass
