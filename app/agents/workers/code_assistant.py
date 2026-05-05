"""代码助手 Agent — 代码生成、审查和调试"""

from langchain_core.messages import HumanMessage, SystemMessage

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
    async def execute(self, task: dict, context: dict, llm) -> dict:
        await self.load_skills()
        messages = [SystemMessage(content=self._build_system_prompt())]

        code_context = context.get("code_context", "")
        if code_context:
            messages.append(SystemMessage(content=f"当前代码上下文：\n```\n{code_context}\n```"))

        error_trace = context.get("error_trace", "")
        if error_trace:
            messages.append(SystemMessage(content=f"错误信息：\n```\n{error_trace}\n```"))

        description = task.get("description", "")
        messages.append(HumanMessage(content=description))

        try:
            result = await llm.ainvoke(messages)
            answer = result.content if hasattr(result, "content") else str(result)
            await self.update_skill(description, answer, success=True)
            return {
                "result": answer,
                "success": True,
                "metadata": {"agent": self.name, "task_type": "code_assistance"},
            }
        except Exception as e:
            return {"result": f"代码助手处理失败: {e}", "success": False, "metadata": {}}
