"""工具装饰器 — @tool 声明式注册服务端工具。

示例::

    from app.tools.decorators import tool
    from app.tools.base import BaseTool, EXEC_SERVER, VIS_PUBLIC

    @tool(
        name="web_search",
        description="搜索互联网获取实时信息",
        exec_location=EXEC_SERVER,
        visibility=VIS_PUBLIC,
        parameters={
            "query":       {"type": "string",  "description": "搜索词",   "required": True},
            "max_results": {"type": "integer", "description": "最大结果数", "default": 5},
        },
    )
    class WebSearchTool(BaseTool):
        async def execute(self, params: dict, context: dict) -> dict:
            query = params["query"]
            # ... 实际搜索逻辑
            return {"result": results, "success": True, "metadata": {"tool": self.name}}

    # 危险操作示例：
    @tool(
        name="file_delete",
        description="删除本地文件（需要用户授权）",
        exec_location=EXEC_CLIENT,
        visibility=VIS_PUBLIC,
        dangerous_ops=["delete"],
        parameters={
            "path": {"type": "string", "description": "文件路径", "required": True},
        },
    )
    class FileDeleteTool(BaseTool):
        async def execute(self, params: dict, context: dict) -> dict:
            # 框架已在此之前完成 delete 操作的同意核查
            ...

    # 专用工具（仅指定 agent 可用）：
    @tool(
        name="internal_sql",
        description="内部 SQL 查询",
        exec_location=EXEC_SERVER,
        visibility=VIS_EXCLUSIVE,
        owner_agent="data_analyst",
    )
    class InternalSqlTool(BaseTool):
        ...
"""

import logging
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


def tool(
    name:              str,
    description:       str             = "",
    exec_location:     str             = "server",
    visibility:        str             = "public",
    owner_agent:       Optional[str]   = None,
    dangerous_ops:     Optional[List[str]]      = None,
    parameters:        Optional[Dict[str, Any]] = None,
):
    """
    声明并注册工具的装饰器。

    被装饰的类必须继承 BaseTool，并实现 execute(params, context) 方法。
    模块导入时立即实例化并注册到全局 registry。

    Args:
        name:          工具唯一标识（snake_case）
        description:   工具功能描述，供 LLM / 前端展示
        exec_location: 执行位置，EXEC_SERVER（默认）或 EXEC_CLIENT
        visibility:    VIS_PUBLIC（默认）/ VIS_PRIVATE / VIS_EXCLUSIVE
        owner_agent:   visibility=exclusive 时指定归属 agent 名称
        dangerous_ops: 包含的危险操作类型列表，参考 DANGEROUS_OPS 常量
        parameters:    参数 schema 字典，格式：
                       {"param_name": {"type": "string", "description": "...", "required": True}}
    """
    from app.tools.registry import registry
    from app.tools.base import SRC_CODE

    def decorator(cls: Type) -> Type:
        cls.name              = name
        cls.description       = description
        cls.source            = SRC_CODE
        cls.exec_location     = exec_location
        cls.visibility        = visibility
        cls.owner_agent       = owner_agent
        cls.dangerous_ops     = dangerous_ops or []
        cls.parameters_schema = parameters or {}

        try:
            instance = cls()
            registry.register(instance)
            logger.info("@tool 注册成功: name=%s vis=%s loc=%s", name, visibility, exec_location)
        except Exception as e:
            logger.error("@tool 注册失败: name=%s error=%s", name, e)

        return cls

    return decorator
