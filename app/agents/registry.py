"""
【模块说明】Agent 注册中心（AgentRegistry）— 所有 AI 助手的"花名册"

这个模块维护一个全局的 Agent 列表，系统启动后所有可用的 Agent 都登记在这里。
它是一个单例（全程序只有一个实例），类似一个"花名册"：

  - 注册（register）：添加一个新 Agent 到名册
  - 查找（get）：根据名称找到对应 Agent
  - 注销（unregister）：从名册删除 Agent
  - 列表（list）：查看某用户有权使用哪些 Agent

【两类 Agent】
  代码 Agent（source=code）：
    开发者在 app/agents/workers/ 目录的 Python 文件中定义，
    加载 @agent 装饰器时自动注册，所有用户都可使用。

  数据库 Agent（source=db）：
    用户通过网页或 API 创建，存储在 MySQL 中。
    服务启动时从数据库批量加载，也可通过 /agents/admin/reload 接口热更新。
    公开（is_public=True）的所有用户可用，私有的仅创建者可用。
"""


import logging
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class AgentRegistry:
    """
    全局 Agent 注册中心（进程级单例）。

    代码 Agent（source="code"）：
        - 通过 @agent 装饰器或显式 register() 注册
        - 所有用户均可用
        - 不参与评分，但记录调用次数

    DB Agent（source="db"）：
        - 由 API 创建，存储在 MySQL agents 表
        - 在引擎启动或 /agents/admin/reload 时动态加载
        - 公有的所有用户可用；私有的仅创建者可用
    """

    _instance: Optional["AgentRegistry"] = None

    def __new__(cls) -> "AgentRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._agents: Dict[str, "BaseAgent"] = {}
        return cls._instance

    def register(self, agent: "BaseAgent") -> None:
        self._agents[agent.name] = agent
        logger.info(
            "Agent 已注册: name=%s source=%s public=%s",
            agent.name, agent.source, agent.is_public,
        )

    def unregister(self, name: str) -> None:
        if name in self._agents:
            del self._agents[name]
            logger.info("Agent 已注销: name=%s", name)

    def get(self, name: str) -> Optional["BaseAgent"]:
        return self._agents.get(name)

    def list_all(self) -> List["BaseAgent"]:
        return list(self._agents.values())

    def list_available_for_user(self, user_id: str) -> List["BaseAgent"]:
        """
        返回该用户可用的所有 Agent：
        - source="code"  → 所有用户均可用
        - source="db"    → 公有的 + 该用户创建的私有 Agent
        """
        result = []
        for ag in self._agents.values():
            if ag.source == "code":
                result.append(ag)
            elif ag.is_public or ag.user_id == user_id:
                result.append(ag)
        return result

    def clear_db_agents(self) -> int:
        """清除所有 DB Agent（重新从数据库加载前调用）。"""
        names = [n for n, a in self._agents.items() if a.source == "db"]
        for n in names:
            del self._agents[n]
        logger.info("已清除 %d 个 DB Agent", len(names))
        return len(names)

    def names(self) -> List[str]:
        return list(self._agents.keys())

    def __len__(self) -> int:
        return len(self._agents)

    def __repr__(self) -> str:
        return f"<AgentRegistry agents={self.names()}>"


# 全局单例
registry = AgentRegistry()
