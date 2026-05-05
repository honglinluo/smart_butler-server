"""数据分析 Agent — 处理数据查询、统计分析和洞察生成"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base import BaseAgent
from app.agents.decorators import agent


@agent(
    name="data_analyst",
    role="数据分析师",
    background=(
        "你是一名资深数据分析师，擅长：\n"
        "- 解读数据趋势和异常\n"
        "- 提供统计分析和业务洞察\n"
        "- 给出可视化建议（图表类型、维度选择）\n"
        "- 提出数据驱动的决策建议\n\n"
        "回复时请：先给出核心洞察（3 条以内），再展开分析，最后提出行动建议。"
    ),
)
class DataAnalystAgent(BaseAgent):
    async def execute(self, task: dict, context: dict, llm) -> dict:
        await self.load_skills()
        messages = [SystemMessage(content=self._build_system_prompt())]

        data_context = context.get("data", {})
        if data_context:
            messages.append(SystemMessage(
                content=f"当前数据上下文：\n{json.dumps(data_context, ensure_ascii=False, indent=2)}"
            ))

        description = task.get("description", "")
        messages.append(HumanMessage(content=f"请分析以下数据需求：{description}"))

        try:
            result = await llm.ainvoke(messages)
            answer = result.content if hasattr(result, "content") else str(result)
            await self.update_skill(description, answer, success=True)
            return {
                "result": answer,
                "success": True,
                "metadata": {"agent": self.name, "task_type": "data_analysis"},
            }
        except Exception as e:
            return {"result": f"数据分析失败: {e}", "success": False, "metadata": {}}
