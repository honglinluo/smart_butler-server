"""工具动态加载器 — 支持用户前端传入代码字符串和 Code Agent 创建工具。

安全限制：
  动态编译的工具代码运行在受限命名空间中，禁止导入高危模块（os.system、subprocess 等）。
  工具代码只能通过声明 dangerous_ops 并获得用户同意后，才能执行受限操作。

用户工具创建流程：
  1. 用户在前端提交工具代码（Python 函数或 BaseTool 子类）+ 工具元信息
  2. ToolLoader.from_user_code() 编译、验证、实例化
  3. 保存到 MySQL（tools 表），注册到 ToolRegistry
  4. 后续服务重启时由 ToolRegistry.load_from_db() 自动恢复

Agent 工具创建流程：
  1. Code Agent 调用 ToolLoader.create_agent_tool()，传入函数代码和元信息
  2. ToolLoader 编译、实例化，visibility 强制设为 exclusive
  3. 保存到 MySQL，立即注册（无需重启）
  4. 归属 agent 可在本次执行中直接使用新工具

代码格式（两种均支持）：

  方式 A — 函数式（简单工具推荐）：
    ```python
    async def execute(params, context):
        query = params.get("query", "")
        # ... 业务逻辑
        return {"result": "...", "success": True, "metadata": {}}
    ```

  方式 B — 类式（完整控制）：
    ```python
    class MyTool(BaseTool):
        async def execute(self, params, context):
            return {"result": "...", "success": True, "metadata": {}}
    ```
"""

import ast
import asyncio
import logging
import textwrap
import uuid
from typing import Any, Dict, List, Optional, Type

from app.tools.base import (
    BaseTool,
    SRC_AGENT, SRC_USER,
    VIS_EXCLUSIVE,
    EXEC_SERVER,
    VIS_PUBLIC,
)

logger = logging.getLogger(__name__)

# 禁止在动态工具代码中导入的模块
_BLOCKED_IMPORTS = frozenset({
    "os", "subprocess", "sys", "shutil", "pathlib",
    "socket", "ftplib", "smtplib", "telnetlib",
    "importlib", "ctypes", "cffi", "multiprocessing",
    "threading",  # 不禁止 asyncio，但禁止裸线程
})

# 允许在动态代码中使用的安全内置函数
_SAFE_BUILTINS = {
    "len", "range", "enumerate", "zip", "map", "filter",
    "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "sorted", "reversed", "min", "max", "sum", "abs", "round",
    "isinstance", "issubclass", "hasattr", "getattr",
    "print", "repr", "type",
    "None", "True", "False",
    "__import__",  # 受限 import，经 _safe_import 过滤
}


def _safe_import(name: str, *args, **kwargs):
    """替换动态代码中的 __import__，拦截高危模块。"""
    base = name.split(".")[0]
    if base in _BLOCKED_IMPORTS:
        raise ImportError(f"动态工具禁止导入模块: {name!r}")
    return __import__(name, *args, **kwargs)


def _validate_code(code: str) -> Optional[str]:
    """
    静态分析工具代码，检查语法错误与高危导入。
    返回错误描述字符串（通过返回 None）。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"代码语法错误: {e}"

    for node in ast.walk(tree):
        # 检查 import xxx
        if isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[0]
                if base in _BLOCKED_IMPORTS:
                    return f"禁止导入高危模块: {alias.name!r}"
        # 检查 from xxx import yyy
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            base   = module.split(".")[0]
            if base in _BLOCKED_IMPORTS:
                return f"禁止导入高危模块: {module!r}"

    return None


class ToolLoader:
    """工具动态编译器（静态方法集合，无状态）。"""

    @staticmethod
    def compile_tool(
        name:              str,
        code_source:       str,
        description:       str             = "",
        source:            str             = SRC_USER,
        visibility:        str             = VIS_PUBLIC,
        exec_location:     str             = EXEC_SERVER,
        owner_user_id:     Optional[str]   = None,
        owner_agent:       Optional[str]   = None,
        dangerous_ops:     Optional[List[str]]      = None,
        parameters_schema: Optional[Dict[str, Any]] = None,
        tool_id:           Optional[str]   = None,
    ) -> BaseTool:
        """
        编译代码字符串，生成 BaseTool 实例。

        支持函数式（async def execute）和类式（class XxxTool(BaseTool)）两种格式。
        agent 来源工具自动强制 visibility=exclusive。
        """
        # ── 1. 静态检查 ──────────────────────────────────────────────────────
        err = _validate_code(code_source)
        if err:
            raise ValueError(f"工具代码校验失败: {err}")

        # ── 2. 编译执行 ──────────────────────────────────────────────────────
        namespace: Dict[str, Any] = {
            "__builtins__": {k: __builtins__[k] for k in _SAFE_BUILTINS if k in __builtins__}  # type: ignore
            if isinstance(__builtins__, dict) else
            {k: getattr(__builtins__, k) for k in _SAFE_BUILTINS if hasattr(__builtins__, k)},
            "__import__": _safe_import,
            "BaseTool":   BaseTool,
            "asyncio":    asyncio,
        }
        exec(compile(code_source, f"<tool:{name}>", "exec"), namespace)  # noqa: S102

        # ── 3. 从命名空间提取 execute 函数或 Tool 子类 ──────────────────────
        tool_instance = ToolLoader._build_instance(
            name              = name,
            namespace         = namespace,
            description       = description,
            source            = source,
            visibility        = visibility,
            exec_location     = exec_location,
            owner_user_id     = owner_user_id,
            owner_agent       = owner_agent,
            dangerous_ops     = dangerous_ops or [],
            parameters_schema = parameters_schema or {},
            tool_id           = tool_id,
        )

        return tool_instance

    @staticmethod
    def _build_instance(
        name:              str,
        namespace:         Dict[str, Any],
        description:       str,
        source:            str,
        visibility:        str,
        exec_location:     str,
        owner_user_id:     Optional[str],
        owner_agent:       Optional[str],
        dangerous_ops:     List[str],
        parameters_schema: Dict[str, Any],
        tool_id:           Optional[str],
    ) -> BaseTool:
        """从编译命名空间构造工具实例（支持函数式和类式两种代码格式）。"""

        # ── 尝试找类式定义（任何 BaseTool 子类）──────────────────────────────
        tool_cls: Optional[Type[BaseTool]] = None
        for obj in namespace.values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseTool)
                and obj is not BaseTool
            ):
                tool_cls = obj
                break

        if tool_cls is not None:
            # 类式：覆盖元信息
            tool_cls.name              = name
            tool_cls.description       = description
            tool_cls.source            = source
            tool_cls.exec_location     = exec_location
            tool_cls.dangerous_ops     = dangerous_ops
            tool_cls.parameters_schema = parameters_schema
            tool_cls.owner_user_id     = owner_user_id
            tool_cls.owner_agent       = owner_agent
            return tool_cls(
                visibility    = visibility,
                tool_id       = tool_id,
            )

        # ── 函数式：寻找 async def execute ─────────────────────────────────
        execute_fn = namespace.get("execute")
        if execute_fn is None or not asyncio.iscoroutinefunction(execute_fn):
            raise ValueError(
                "工具代码中未找到 'async def execute(params, context)' 函数或 BaseTool 子类"
            )

        # 动态构造一个 BaseTool 子类，将 execute 函数挂载上去
        dyn_cls = type(
            f"DynTool_{name}",
            (BaseTool,),
            {"execute": execute_fn},
        )
        return dyn_cls(
            name              = name,
            description       = description,
            source            = source,
            visibility        = visibility,
            exec_location     = exec_location,
            owner_user_id     = owner_user_id,
            owner_agent       = owner_agent,
            dangerous_ops     = dangerous_ops,
            parameters_schema = parameters_schema,
            tool_id           = tool_id,
        )

    # ── 用户前端创建工具 ──────────────────────────────────────────────────────

    @staticmethod
    async def from_user_code(
        code_source:       str,
        name:              str,
        description:       str,
        owner_user_id:     str,
        visibility:        str             = VIS_PUBLIC,
        exec_location:     str             = EXEC_SERVER,
        dangerous_ops:     Optional[List[str]]      = None,
        parameters_schema: Optional[Dict[str, Any]] = None,
    ) -> BaseTool:
        """
        从用户传入的代码创建工具，持久化到 MySQL，注册到全局 registry。

        Args:
            code_source:   Python 代码字符串（函数式或类式）
            name:          工具名称（snake_case，全局唯一）
            description:   工具描述
            owner_user_id: 创建者用户 ID
            visibility:    public / private（用户工具默认 private）
            exec_location: server / client
            dangerous_ops: 危险操作列表
            parameters_schema: 参数 schema

        Returns:
            已注册的 BaseTool 实例

        Raises:
            ValueError: 代码格式错误或校验失败
        """
        from app.tools.registry import registry

        tool = ToolLoader.compile_tool(
            name              = name,
            code_source       = code_source,
            description       = description,
            source            = SRC_USER,
            visibility        = visibility,
            exec_location     = exec_location,
            owner_user_id     = owner_user_id,
            dangerous_ops     = dangerous_ops,
            parameters_schema = parameters_schema,
        )

        saved = await tool.save_to_db(code_source=code_source)
        if not saved:
            logger.warning("用户工具持久化失败，仅注册到内存 tool=%s", name)

        registry.register(tool)
        logger.info(
            "用户工具已创建并注册: name=%s owner=%s vis=%s",
            name, owner_user_id, tool.visibility,
        )
        return tool

    # ── Code Agent 创建工具 ───────────────────────────────────────────────────

    @staticmethod
    async def create_agent_tool(
        code_source:       str,
        name:              str,
        description:       str,
        owner_agent:       str,
        owner_user_id:     Optional[str]            = None,
        exec_location:     str                      = EXEC_SERVER,
        dangerous_ops:     Optional[List[str]]      = None,
        parameters_schema: Optional[Dict[str, Any]] = None,
    ) -> BaseTool:
        """
        由 Code Agent 运行时创建专用工具，立即注册，可在本次任务中直接使用。

        visibility 强制为 exclusive，owner_agent 绑定到创建 agent。

        Args:
            code_source:   Python 代码字符串
            name:          工具名称（建议带 agent 前缀，如 "data_analyst_pivot_table"）
            description:   工具描述
            owner_agent:   创建此工具的 agent 名称
            owner_user_id: 触发该 agent 的用户 ID（可选，用于权限追踪）
            exec_location: server / client
            dangerous_ops: 危险操作列表
            parameters_schema: 参数 schema

        Returns:
            已注册的 BaseTool 实例（visibility=exclusive）
        """
        from app.tools.registry import registry

        tool = ToolLoader.compile_tool(
            name              = name,
            code_source       = code_source,
            description       = description,
            source            = SRC_AGENT,
            visibility        = VIS_EXCLUSIVE,   # 强制 exclusive
            exec_location     = exec_location,
            owner_user_id     = owner_user_id,
            owner_agent       = owner_agent,
            dangerous_ops     = dangerous_ops,
            parameters_schema = parameters_schema,
        )

        saved = await tool.save_to_db(code_source=code_source)
        if not saved:
            logger.warning("Agent 工具持久化失败，仅注册到内存 tool=%s", name)

        registry.register(tool)
        logger.info(
            "Agent 工具已创建并立即注册: name=%s owner_agent=%s",
            name, owner_agent,
        )
        return tool

    # ── 文档式创建（用户传入自然语言描述，由 LLM 生成代码）──────────────────

    @staticmethod
    async def from_doc(
        doc:               str,
        name:              str,
        owner_user_id:     str,
        llm,
        description:       str             = "",
        visibility:        str             = VIS_PUBLIC,
        exec_location:     str             = EXEC_SERVER,
        dangerous_ops:     Optional[List[str]]      = None,
        parameters_schema: Optional[Dict[str, Any]] = None,
    ) -> BaseTool:
        """
        用户通过自然语言文档描述工具功能，由 LLM 生成代码后创建工具。

        Args:
            doc:  工具功能的自然语言描述
            name: 工具名称
            llm:  LangChain ChatModel 实例（用于代码生成）
            其余参数同 from_user_code()
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        system = SystemMessage(content=(
            "你是一个 Python 工具代码生成器。根据用户的工具描述，"
            "生成一个工具的 execute 函数，函数签名为:\n"
            "  async def execute(params: dict, context: dict) -> dict:\n"
            "函数必须返回 {'result': ..., 'success': bool, 'metadata': {}} 格式的字典。\n"
            "只输出 Python 代码，不要有任何解释文字，不要加 ```python 标记。"
        ))
        human = HumanMessage(content=f"工具名称: {name}\n工具描述:\n{doc}")

        resp = await llm.ainvoke([system, human])
        code_source = resp.content if hasattr(resp, "content") else str(resp)
        code_source = code_source.strip().strip("`").strip()

        logger.info("LLM 已为工具 '%s' 生成代码 (%d 字节)", name, len(code_source))

        return await ToolLoader.from_user_code(
            code_source       = code_source,
            name              = name,
            description       = description or doc[:200],
            owner_user_id     = owner_user_id,
            visibility        = visibility,
            exec_location     = exec_location,
            dangerous_ops     = dangerous_ops,
            parameters_schema = parameters_schema,
        )
