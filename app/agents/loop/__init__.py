"""
【模块说明】Agent 事件循环包（Loop）— Agent 执行任务时的"内部引擎"

当一个 Agent 接到任务后，不一定一次就能完成。它可能需要：
  - 请求新建一个工具（"我需要一个能查天气的工具"）
  - 等待用户确认是否允许（危险操作需要授权）
  - 工具建好后重新尝试（"好了，现在我有工具了，重新试一遍"）

这个包提供了实现上述流程的所有组件：

  event_loop.py    — 主循环：协调 Agent 执行 → 工具请求 → 工具构建 → 重试的完整流程
  decision_gate.py — 决策门控：在构建新工具前暂停，等待用户授权（allow/ask/deny）
  tool_builder.py  — 工具构建器：让 AI 生成新工具的代码，写入动态工具目录
  events.py        — 事件/数据结构定义（事件类型、工具请求格式等）
  loop_logger.py   — 调用日志：记录每一步执行过程，推送到 Redis 供实时查看

Agent 内部事件循环包
"""
from app.agents.loop.event_loop import AgentEventLoop
from app.agents.loop.decision_gate import DecisionState, UserDecisionGate
from app.agents.loop.events import (
    LoopEventType, LoopLogEntry, ToolCodeRequest, BuiltTool, TOOL_REQUEST_SCHEMA,
)

__all__ = [
    "AgentEventLoop",
    "DecisionState",
    "UserDecisionGate",
    "LoopEventType",
    "LoopLogEntry",
    "ToolCodeRequest",
    "BuiltTool",
    "TOOL_REQUEST_SCHEMA",
]
