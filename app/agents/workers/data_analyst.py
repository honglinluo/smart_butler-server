"""
【模块说明】数据分析 Agent（DataAnalystAgent）— 专业数据分析师

负责处理一切跟数据打交道的任务：
  - 查询数据库（SQL）、分析电子表格（CSV/Excel）
  - 统计计算、趋势分析、异常检测
  - 生成数据洞察报告，提炼关键发现
  - 辅助做数据决策建议

数据分析 Agent — 处理数据查询、统计分析和洞察生成
"""

import json

from langchain_core.messages import SystemMessage

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
    def _build_context_messages(self, context: dict) -> list:
        data_context = context.get("data", {})
        if not data_context:
            return []
        return [SystemMessage(
            content=f"当前数据上下文：\n{json.dumps(data_context, ensure_ascii=False, indent=2)}"
        )]
