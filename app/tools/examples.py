"""工具创建方式示例 — 演示三种创建方式及权限分类。

此文件中的工具在模块导入时自动注册到全局 registry。
"""

from app.tools.base import (
    BaseTool,
    EXEC_SERVER, EXEC_CLIENT,
    VIS_PUBLIC, VIS_EXCLUSIVE,
    DANGEROUS_OPS,
)
from app.tools.decorators import tool


# ══════════════════════════════════════════════════════════════════════════════
# 方式 A — 装饰器方式（公开工具，服务端执行）
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="web_search",
    description="搜索互联网，获取实时信息",
    exec_location=EXEC_SERVER,
    visibility=VIS_PUBLIC,
    parameters={
        "query":       {"type": "string",  "description": "搜索词",     "required": True},
        "max_results": {"type": "integer", "description": "最大结果数", "default": 5},
    },
)
class WebSearchTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        query = params["query"]
        # 实际搜索逻辑（此处为占位）
        return {
            "result":   f"关于'{query}'的搜索结果（占位）",
            "success":  True,
            "metadata": {"tool": self.name, "query": query},
        }


# ══════════════════════════════════════════════════════════════════════════════
# 方式 B — 继承方式（公开工具，服务端执行，含危险操作）
# ══════════════════════════════════════════════════════════════════════════════

class DatabaseQueryTool(BaseTool):
    """直接查询数据库（需声明 write 危险操作以启用修改）。"""
    name              = "db_query"
    description       = "执行 SQL 查询，获取数据库记录"
    exec_location     = EXEC_SERVER
    visibility        = VIS_PUBLIC
    dangerous_ops     = []           # 仅 SELECT，无危险操作
    parameters_schema = {
        "sql":     {"type": "string", "description": "SQL 查询语句",  "required": True},
        "db_name": {"type": "string", "description": "目标数据库名称", "required": False},
    }

    async def execute(self, params: dict, context: dict) -> dict:
        sql = params.get("sql", "")
        # 实际执行逻辑（此处为占位）
        return {
            "result":   f"查询结果（占位）: {sql[:50]}",
            "success":  True,
            "metadata": {"tool": self.name},
        }


# ══════════════════════════════════════════════════════════════════════════════
# 方式 C — 专用工具（仅 data_analyst agent 可用）
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="data_pivot",
    description="对数据集执行透视表分析（data_analyst 专用）",
    exec_location=EXEC_SERVER,
    visibility=VIS_EXCLUSIVE,
    owner_agent="data_analyst",
    parameters={
        "data":        {"type": "array",  "description": "原始数据（JSON 数组）", "required": True},
        "group_by":    {"type": "string", "description": "分组字段",             "required": True},
        "agg_field":   {"type": "string", "description": "聚合字段",             "required": True},
        "agg_func":    {"type": "string", "description": "聚合函数 sum/avg/count", "default": "sum"},
    },
)
class DataPivotTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        return {
            "result":   "透视表结果（占位）",
            "success":  True,
            "metadata": {"tool": self.name},
        }


# ══════════════════════════════════════════════════════════════════════════════
# 方式 D — 客户端工具（需危险操作授权，在用户本地执行）
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="local_file_read",
    description="读取用户本地文件内容",
    exec_location=EXEC_CLIENT,
    visibility=VIS_PUBLIC,
    parameters={
        "path":     {"type": "string", "description": "文件路径",     "required": True},
        "encoding": {"type": "string", "description": "文件编码",     "default": "utf-8"},
    },
)
class LocalFileReadTool(BaseTool):
    """客户端执行工具：execute() 返回 ClientExecRequest，由接入层转发给客户端。"""
    # 父类默认 execute() 已处理 client 工具逻辑，无需重写


@tool(
    name="local_file_write",
    description="向用户本地文件写入内容（需要授权）",
    exec_location=EXEC_CLIENT,
    visibility=VIS_PUBLIC,
    dangerous_ops=["write"],       # 触发同意核查
    parameters={
        "path":    {"type": "string", "description": "目标文件路径", "required": True},
        "content": {"type": "string", "description": "写入内容",     "required": True},
        "mode":    {"type": "string", "description": "写入模式 w/a", "default": "w"},
    },
)
class LocalFileWriteTool(BaseTool):
    """写文件工具：write 操作需要用户同意后，框架才会实际生成 ClientExecRequest。"""


@tool(
    name="run_cli",
    description="在用户本地执行 CLI 命令（需要授权）",
    exec_location=EXEC_CLIENT,
    visibility=VIS_PUBLIC,
    dangerous_ops=["cli"],         # 触发最严格的同意核查
    parameters={
        "command": {"type": "string", "description": "Shell 命令", "required": True},
        "cwd":     {"type": "string", "description": "工作目录",   "required": False},
    },
)
class RunCliTool(BaseTool):
    """CLI 执行工具：cli 操作是最高危险操作，每次默认要求用户重新授权。"""


# ══════════════════════════════════════════════════════════════════════════════
# 动态创建示例（通常由 API 或 Agent 在运行时调用，此处仅作文档示意）
# ══════════════════════════════════════════════════════════════════════════════

_USER_TOOL_EXAMPLE = '''
# 用户在前端提交的代码（函数式）
async def execute(params, context):
    import httpx
    url = params.get("url", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        return {"result": resp.text[:2000], "success": True, "metadata": {}}
'''

_USER_TOOL_CLASS_EXAMPLE = '''
# 用户在前端提交的代码（类式）
class MyFetchTool(BaseTool):
    async def execute(self, params, context):
        import httpx
        url = params.get("url", "")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            return {"result": resp.text[:2000], "success": True, "metadata": {}}
'''

# 运行时动态创建示例：
# tool = await ToolLoader.from_user_code(
#     code_source       = _USER_TOOL_EXAMPLE,
#     name              = "custom_fetch",
#     description       = "获取指定 URL 的页面内容",
#     owner_user_id     = "user_123",
#     visibility        = "private",
#     exec_location     = "server",
#     parameters_schema = {"url": {"type": "string", "required": True}},
# )

# Code Agent 创建工具示例：
# tool = await ToolLoader.create_agent_tool(
#     code_source   = "async def execute(params, context): ...",
#     name          = "data_analyst_custom_agg",
#     description   = "自定义聚合逻辑",
#     owner_agent   = "data_analyst",
#     owner_user_id = "user_123",
# )
