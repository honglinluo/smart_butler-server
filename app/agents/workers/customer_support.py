"""
【模块说明】客服 Agent（CustomerSupportAgent）— 专业客户服务代表

负责处理一切与客户服务相关的对话：
  - 回答产品/服务相关的咨询问题
  - 受理投诉，安抚情绪，提供解决方案
  - 处理售后问题（退换货、使用教程等）
  - 收集用户反馈，汇总常见问题

客服 Agent — 处理客户咨询、投诉和售后问题
"""

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
    def _build_context_messages(self, context: dict) -> list:
        msgs = []
        if customer_info := context.get("customer_info", {}):
            msgs.append(SystemMessage(content=f"客户信息：{customer_info}"))
        for turn in context.get("history", [])[-3:]:
            if isinstance(turn, dict) and (ui := turn.get("user_input", "")):
                msgs.append(HumanMessage(content=ui))
        return msgs
