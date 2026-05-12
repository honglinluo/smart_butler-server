"""
【模块说明】Agent 装饰器（@agent）— 用一行声明创建一个 AI 专家

通过 @agent 装饰器，开发者可以用极简的方式定义一个新的 AI Agent：
只需声明它的名字、角色、背景介绍和可用工具，系统会自动完成注册和初始化。

【使用方式】
  @agent(
      name="data_analyst",        # Agent 的唯一标识
      role="数据分析工程师",        # 角色名称（会注入提示词）
      background="你是一个...",    # 角色背景描述（详细的角色设定）
      tools=["sql_query"],        # 该 Agent 可使用的工具列表
      is_public=True,             # 是否对用户可见
  )
  class DataAnalystAgent(BaseAgent):
      pass  # 可选自定义 execute() 方法，不写则用父类默认 LLM 实现

Agent 装饰器 - @agent 声明式注册服务端 Agent
"""

import logging
from typing import List, Optional, Type

logger = logging.getLogger(__name__)


def agent(
    name:       str,
    role:       str             = "",
    background: str             = "",
    tools:      List[str]       = None,
    is_public:  bool            = True,
    max_skills: int             = 10,
):
    """
    声明并注册服务端 Agent 的装饰器。

    被装饰的类必须继承 BaseAgent，并实现 execute() 方法（可选，有默认 LLM 实现）。

    示例::

        from app.agents.decorators import agent
        from app.agents.base import BaseAgent

        @agent(
            name="data_analyst",
            role="数据分析工程师",
            background="你是一个全能的数据分析师，能够挖掘数据底层的关联关系...",
            tools=["sql_query", "chart_generation"],
        )
        class DataAnalystAgent(BaseAgent):
            async def execute(self, task, context, llm):
                # 自定义逻辑；不重写时使用 BaseAgent 默认 LLM 实现
                ...

    注意：
    - 被装饰类在模块导入时立即实例化并注册到全局 registry
    - source 固定为 "code"，所有用户可用，不参与评分
    - 使用 CLI `python -m app.cli reload-agents` 可重新扫描并注册所有代码 Agent
    """
    # 延迟导入，避免循环依赖
    from app.agents.registry import registry

    def decorator(cls: Type) -> Type:
        cls.name       = name
        cls.role       = role
        cls.background = background
        cls.tools      = tools or []
        cls.is_public  = is_public
        cls.source     = "code"

        # 立即实例化并注册
        try:
            instance = cls(max_skills=max_skills)
            registry.register(instance)
            logger.info("@agent 注册成功: name=%s", name)
        except Exception as e:
            logger.error("@agent 注册失败: name=%s error=%s", name, e)

        return cls

    return decorator
