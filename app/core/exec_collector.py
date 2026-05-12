"""
【模块说明】执行记录收集器（ExecCollector）— 追踪本轮对话中 Agent 调用了什么

每次 AI 处理一条用户消息时，中间可能调用多个工具、执行多个步骤。
这个模块负责收集这些执行记录，最终汇总到对话历史中保存。

【收集内容】
  ToolCallRecord — 每次工具调用的记录：工具名、输入参数预览、结果预览、是否成功
  L2StepRecord   — 每个 L2 执行步骤的记录（见 task_planner.py）

【ContextVar 机制】
  使用 Python 的 ContextVar（上下文变量）存储，同一次请求中任何地方的代码
  都可以直接向收集器写入记录，不需要通过函数参数层层传递。

per-agent execution collector — tracks tool calls and L2 steps for history storage.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallRecord:
    tool_name: str
    args_preview: str
    result_preview: str
    auth_level: str  # "无需授权" | "已授权" | "已拒绝"
    success: bool
    elapsed_ms: float = 0.0

    def to_summary(self) -> str:
        status = "成功" if self.success else "失败"
        return f"调用 {self.tool_name}（{self.auth_level}），{status}：{self.result_preview}"


@dataclass
class StepRecord:
    step_id: str
    description: str
    success: bool
    result_summary: str = ""


class AgentExecCollector:
    """Collect tool calls and L2 steps during a single agent execution."""

    def __init__(self) -> None:
        self.steps: List[StepRecord] = []
        self.tool_calls: List[ToolCallRecord] = []

    def add_step(self, step_id: str, desc: str, success: bool, result_summary: str = "") -> None:
        self.steps.append(StepRecord(step_id, desc[:120], success, result_summary[:200]))

    def add_tool_call(
        self,
        tool_name: str,
        args_preview: str,
        result_preview: str,
        auth_level: str,
        success: bool,
        elapsed_ms: float = 0.0,
    ) -> None:
        self.tool_calls.append(
            ToolCallRecord(tool_name, args_preview[:150], result_preview[:200], auth_level, success, elapsed_ms)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": [
                {
                    "step_id": s.step_id,
                    "description": s.description,
                    "success": s.success,
                    "result_summary": s.result_summary,
                }
                for s in self.steps
            ],
            "tool_calls": [
                {
                    "tool_name": t.tool_name,
                    "auth_level": t.auth_level,
                    "success": t.success,
                    "summary": t.to_summary(),
                }
                for t in self.tool_calls
            ],
        }


_CURRENT_COLLECTOR: ContextVar[Optional[AgentExecCollector]] = ContextVar(
    "agent_exec_collector", default=None
)


def set_collector(c: Optional[AgentExecCollector]) -> Any:
    """Set the current collector; returns a token for reset."""
    return _CURRENT_COLLECTOR.set(c)


def reset_collector(token: Any) -> None:
    """Reset to the previous collector using the token from set_collector."""
    _CURRENT_COLLECTOR.reset(token)


def get_collector() -> Optional[AgentExecCollector]:
    """Get the current collector, or None if not set."""
    return _CURRENT_COLLECTOR.get()
