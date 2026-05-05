"""工作智能体模块 - 定义各类具体的执行智能体"""

from app.agents.workers.general_assistant import GeneralAssistantAgent
from app.agents.workers.data_analyst import DataAnalystAgent
from app.agents.workers.customer_support import CustomerSupportAgent
from app.agents.workers.code_assistant import CodeAssistantAgent
from app.agents.workers.summarizer import SummarizerAgent
from app.agents.workers.skill_builder import SkillBuilderAgent

__all__ = [
    "GeneralAssistantAgent",
    "DataAnalystAgent",
    "CustomerSupportAgent",
    "CodeAssistantAgent",
    "SummarizerAgent",
    "SkillBuilderAgent",
]
