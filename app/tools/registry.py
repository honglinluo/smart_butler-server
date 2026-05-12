"""
【模块说明】工具注册中心（ToolRegistry）— 所有可用工具的"工具箱"

类似 Agent 的注册中心，这里维护一个全局工具列表（进程级单例）。
系统中所有工具（无论是内置的、用户创建的还是 Agent 动态生成的）都在这里登记。

  - 代码工具：服务启动时自动注册（通过 @tool 装饰器或 register()）
  - 用户工具：用户创建后立即注册，服务重启时从数据库恢复加载
  - Agent 工具：Agent 运行时动态创建并立即注册（visibility=exclusive，仅该 Agent 可用）

提供 list_available_for(user_id, agent_name) 方法，
根据用户身份和调用场景返回该用户有权使用的工具列表。
"""


import json
import logging
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    全局工具注册中心（进程级单例）。

    code 来源工具：
        - 通过 @tool 装饰器或 register() 显式注册，模块导入时自动完成。
        - 所有用户 / agent 均可用（visibility=public）或仅指定 agent（exclusive）。

    user 来源工具：
        - 由 API 接收用户代码后通过 ToolLoader 编译，调用 register() 注册。
        - 服务启动时调用 load_from_db() 恢复上次已创建的工具。

    agent 来源工具：
        - Code Agent 运行时通过 ToolLoader.create_agent_tool() 创建并立即注册。
        - visibility 强制为 exclusive，owner_agent 指向创建它的 agent。
    """

    _instance: Optional["ToolRegistry"] = None

    def __new__(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, "BaseTool"] = {}
        return cls._instance

    # ── 注册 / 注销 ───────────────────────────────────────────────────────────

    def register(self, tool: "BaseTool") -> None:
        self._tools[tool.name] = tool
        logger.info(
            "Tool 已注册: name=%s source=%s vis=%s loc=%s",
            tool.name, tool.source, tool.visibility, tool.exec_location,
        )

    def unregister(self, name: str) -> None:
        if name in self._tools:
            del self._tools[name]
            logger.info("Tool 已注销: name=%s", name)

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional["BaseTool"]:
        return self._tools.get(name)

    def list_all(self) -> List["BaseTool"]:
        return list(self._tools.values())

    def list_available_for(
        self,
        user_id:    str,
        agent_name: Optional[str] = None,
    ) -> List["BaseTool"]:
        """
        返回指定用户 / agent 可用的所有工具。

        - public    → 所有人可用
        - private   → 仅 owner_user_id 可用
        - exclusive → 仅 owner_agent 可用（agent_name 必须匹配）
        """
        return [t for t in self._tools.values() if t.is_available_for(user_id, agent_name)]

    def list_by_location(self, exec_location: str) -> List["BaseTool"]:
        """按执行位置过滤（server / client）。"""
        return [t for t in self._tools.values() if t.exec_location == exec_location]

    def names(self) -> List[str]:
        return list(self._tools.keys())

    # ── 从 MySQL 恢复 user / agent 来源工具 ─────────────────────────────────

    async def load_from_db(self) -> int:
        """服务启动时从 tools 表加载 user / agent 来源工具并重新注册。"""
        from app.database.pool import get_connection, release_connection
        from app.tools.loader import ToolLoader

        conn = None
        loaded = 0
        try:
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                """
                SELECT tool_id, name, description, source, visibility, exec_location,
                       owner_user_id, owner_agent, dangerous_ops, parameters_schema, code_source
                FROM tools
                WHERE source IN ('user', 'agent') AND is_active = 1
                ORDER BY created_at ASC
                """,
                {},
            )
            if rows is None or len(rows) == 0:
                return 0

            for _, row in rows.iterrows():
                try:
                    ops    = json.loads(row.get("dangerous_ops")  or "[]")
                    params = json.loads(row.get("parameters_schema") or "{}")
                    code   = row.get("code_source") or ""
                    tool   = ToolLoader.compile_tool(
                        name              = row["name"],
                        code_source       = code,
                        description       = row.get("description", ""),
                        source            = row["source"],
                        visibility        = row["visibility"],
                        exec_location     = row.get("exec_location", "server"),
                        owner_user_id     = row.get("owner_user_id"),
                        owner_agent       = row.get("owner_agent"),
                        dangerous_ops     = ops,
                        parameters_schema = params,
                        tool_id           = row["tool_id"],
                    )
                    self.register(tool)
                    loaded += 1
                except Exception as e:
                    logger.warning("从 DB 加载工具失败 name=%s: %s", row.get("name"), e)

        except Exception as e:
            logger.warning("load_from_db 失败: %s", e)
        finally:
            if conn:
                await release_connection("mysql", conn)

        logger.info("从数据库恢复工具: %d 个", loaded)
        return loaded

    def clear_dynamic_tools(self) -> int:
        """清除所有 user / agent 来源工具（重新加载前调用）。"""
        from app.tools.base import SRC_CODE
        names = [n for n, t in self._tools.items() if t.source != SRC_CODE]
        for n in names:
            del self._tools[n]
        logger.info("已清除动态工具: %d 个", len(names))
        return len(names)

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"<ToolRegistry tools={self.names()}>"


# 全局单例
registry = ToolRegistry()
