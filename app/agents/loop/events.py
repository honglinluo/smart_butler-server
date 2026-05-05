"""Agent 事件循环 — 数据模型（事件类型、工具请求、构建结果）"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class LoopEventType(str, Enum):
    TASK_START        = "task_start"
    AGENT_EXECUTING   = "agent_executing"
    TOOL_REQUESTED    = "tool_requested"
    TOOL_BUILDING     = "tool_building"
    TOOL_BUILT        = "tool_built"
    TOOL_INJECTED     = "tool_injected"
    AGENT_RETRYING    = "agent_retrying"
    DECISION_REQUIRED = "decision_required"
    DECISION_GRANTED  = "decision_granted"
    DECISION_DENIED   = "decision_denied"
    TASK_COMPLETE     = "task_complete"
    TASK_FAILED       = "task_failed"
    LOOP_MAX_ITER     = "loop_max_iter"


@dataclass
class LoopLogEntry:
    event_type: LoopEventType
    agent_name: str
    message:    str
    timestamp:  datetime = field(default_factory=datetime.utcnow)
    iteration:  int      = 0
    data:       Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "agent_name": self.agent_name,
            "message":    self.message,
            "timestamp":  self.timestamp.isoformat(),
            "iteration":  self.iteration,
            "data":       self.data,
        }

    def format_line(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S")
        return (
            f"[{ts}][iter={self.iteration}][{self.agent_name}]"
            f" {self.event_type.value}: {self.message}"
        )


# ── 工具请求固定 JSON Schema（注入到 agent 系统提示中）─────────────────────────

TOOL_REQUEST_SCHEMA = """\
如果你需要一个当前不存在的专用工具（Python 函数），请**不要尝试自行实现**，
而是在回复中返回以下格式的 JSON 工具请求，系统将自动为你构建该工具并重新调用你：

```json
{
  "__tool_request__": true,
  "tool_name": "<snake_case 工具名，如 fetch_weather>",
  "description": "<一句话说明工具用途>",
  "requirements": "<详细功能需求描述>",
  "input_params": [
    {"name": "<参数名>", "type": "<Python 类型>", "desc": "<说明>"}
  ],
  "output_format": "<期望返回值格式，如：dict {result: str, code: int}>",
  "example_code": "<伪代码或示例调用>"
}
```

注意：只有在任务确实需要外部工具且当前工具列表中不存在时才发起请求。
如果可以直接用现有工具或纯文本回答，请直接完成任务。"""


@dataclass
class ToolCodeRequest:
    """Agent 向路由请求构建新工具时返回的固定格式数据。"""
    tool_name:     str
    description:   str
    requirements:  str
    input_params:  List[Dict[str, str]]
    output_format: str
    example_code:  str
    requested_by:  str = ""

    SIGNAL_KEY = "__tool_request__"

    @classmethod
    def parse(cls, response: str, agent_name: str = "") -> Optional["ToolCodeRequest"]:
        """从 agent 响应文本中解析 ToolCodeRequest。

        支持 ```json ... ``` 包裹格式或裸 JSON 对象。
        返回 None 表示响应中不包含工具请求。
        """
        json_str: Optional[str] = None

        # 优先尝试 ```json 代码块
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", response)
        if m:
            json_str = m.group(1)

        # 退回：找第一个含 __tool_request__ 的 JSON 对象
        if not json_str:
            m2 = re.search(r'\{[^{}]*"__tool_request__"[^{}]*\}', response, re.DOTALL)
            if m2:
                json_str = m2.group(0)

        if not json_str:
            return None

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        if not data.get(cls.SIGNAL_KEY):
            return None

        return cls(
            tool_name    =str(data.get("tool_name", "unnamed_tool")),
            description  =str(data.get("description", "")),
            requirements =str(data.get("requirements", "")),
            input_params =data.get("input_params", []),
            output_format=str(data.get("output_format", "dict")),
            example_code =str(data.get("example_code", "")),
            requested_by =agent_name,
        )


@dataclass
class BuiltTool:
    """code_assistant 构建完成的工具元数据。"""
    tool_name:     str
    module_path:   str   # e.g. app.tools.dynamic.fetch_weather
    function_name: str   # e.g. tool_fetch_weather
    description:   str
    file_path:     str
    success:       bool
    error:         Optional[str] = None
