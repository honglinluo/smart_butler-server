"""
【模块说明】进度事件总线（ProgressBus）— 流式对话中的实时进度推送通道

在流式对话中，AI 处理的每一步（路由决策、Agent 开始执行、工具调用等）
都需要实时推送给前端显示进度。

【问题：如何从深层代码推送事件？】
  AI 处理过程可能嵌套很多层（Engine → Agent → Tool → ...），
  如果每个函数都要传一个"队列"参数，代码会非常繁琐。
  本模块用 ContextVar（上下文变量）解决这个问题：
  每个请求有自己独立的消息队列，深层代码只需调用 push() 就能发送，
  不需要知道队列存在哪里。

【事件类型】
  push() 推送的结构化事件最终被转换为 SSE 格式发送给浏览器，
  前端根据事件类型渲染进度面板（路由信息、Agent 执行状态、工具调用结果等）。

请求级别进度事件总线

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
