"""
【模块说明】代码助手 Agent（CodeAssistantAgent）— 专业软件工程师

负责处理一切跟编程相关的任务：
  - 生成代码（Python、TypeScript、SQL 等）
  - 代码审查（发现 Bug、安全漏洞、性能问题）
  - Debug 调试（分析报错，提供修复方案）
  - 解释代码逻辑，帮助理解复杂代码

代码助手 Agent — 代码生成、审查和调试
"""

from langchain_core.messages import SystemMessage

from app.agents.base import BaseAgent
from app.agents.decorators import agent


@agent(
    name="code_assistant",
    role="代码助手",
    background=(
        "你是一名全栈软件工程师，擅长：\n"
        "- 代码生成（Python、TypeScript、SQL 等主流语言）\n"
        "- 代码审查（发现 bug、安全漏洞、性能问题）\n"
        "- 代码重构和优化\n"
        "- 技术架构建议\n\n"
        "回复规范：代码块用 ```language 包裹，给出简短说明，指出潜在风险。"
    ),
)
class CodeAssistantAgent(BaseAgent):
    def _build_context_messages(self, context: dict) -> list:
        msgs = []
        if code_context := context.get("code_context", ""):
            msgs.append(SystemMessage(content=f"当前代码上下文：\n```\n{code_context}\n```"))
        if error_trace := context.get("error_trace", ""):
            msgs.append(SystemMessage(content=f"错误信息：\n```\n{error_trace}\n```"))
        return msgs
