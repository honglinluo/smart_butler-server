"""客服 Agent — 处理客户咨询、投诉和售后问题"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base import BaseAgent
from app.agents.decorators import agent


@agent(
    name="customer_support",
    role="客服专员",
    background=(
        "你是一名专业客服专员，负责：\n"
        "- 解答客户产品和服务问题\n"
        "- 处理投诉并提供解决方案\n"
        "- 查询订单状态和退款政策\n"
        "- 引导客户完成操作步骤\n\n"
        "回复风格：礼貌、耐心、专业。遇到无法解决的问题，给出上报流程。"
    ),
)
class CustomerSupportAgent(BaseAgent):
    async def execute(self, task: dict, context: dict, llm) -> dict:
        await self.load_skills()
        messages = [SystemMessage(content=self._build_system_prompt())]

        customer_info = context.get("customer_info", {})
        if customer_info:
            messages.append(SystemMessage(content=f"客户信息：{customer_info}"))

        history = context.get("history", [])
        for turn in history[-3:]:
            if isinstance(turn, dict):
                messages.append(HumanMessage(content=turn.get("user_input", "")))

        description = task.get("description", "")
        messages.append(HumanMessage(content=description))

        try:
            result = await llm.ainvoke(messages)
            answer = result.content if hasattr(result, "content") else str(result)
            await self.update_skill(description, answer, success=True)
            return {
                "result": answer,
                "success": True,
                "metadata": {"agent": self.name, "task_type": "customer_support"},
            }
        except Exception as e:
            return {"result": f"客服处理失败: {e}", "success": False, "metadata": {}}
