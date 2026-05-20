"""
【模块说明】工具基础类（BaseTool）— 所有工具的公共能力和接口

"工具"（Tool）是 AI 执行具体操作的能力单元，例如：搜索网页、读取文件、执行命令。
所有工具都从 BaseTool 继承，框架会自动处理权限检查、执行路由和调用统计。

【工具三种来源（source）】
  code  — 开发者写在代码里的工具，导入模块时自动注册，最稳定
  user  — 用户通过前端上传代码或文字描述后动态创建，存在 MySQL 中
  agent — 某个 Agent 运行时临时创建的专用工具，仅该 Agent 可用

【工具可见性（visibility）】
  public    — 所有人都可以使用
  private   — 仅创建者自己可用
  exclusive — 仅指定的某个 Agent 可用（Agent 内部专用工具）

【执行位置（exec_location）】
  server — 在服务器上执行（网络请求、数据库查询等）
  client — 需要在用户本地设备上执行（操作本地文件、调用本地命令等）

【危险操作与权限控制】
  工具需要在 dangerous_ops 中声明它会执行哪些危险操作。
  每次执行前，框架自动检查用户是否已授权，
  未授权则抛出 ConsentRequiredException，引擎会弹出授权确认框。

Tool 基础类 — 工具权限控制、执行路由与调用统计。

三种创建来源（source）：
  code  — 开发人员通过继承基类或 @tool 装饰器实现，模块导入时自动注册。
  user  — 用户通过前端传入代码字符串或文档动态创建，持久化到 MySQL。
  agent — Code Agent 运行时动态生成，强制 visibility=exclusive，立即加载。

可见性（visibility）：
  public    — 所有用户和所有 agent 均可调用。
  private   — 仅创建者（owner_user_id）可调用；user 来源默认值。
  exclusive — 仅归属 agent（owner_agent）可调用；agent 来源强制此值。
  注：code 来源工具由开发者在定义时声明 visibility，默认 public。

执行位置（exec_location）：
  server — 在服务端直接运行（Web 操作、在线 API、数据查询等）。
  client — 需在用户客户端本地运行（文件系统、本地 CLI、本地部署等）。
           execute() 返回 ClientExecRequest，由接入层转发给客户端执行。

危险操作与最小权限：
  通过 dangerous_ops 声明该工具包含的危险操作类型（来自 DANGEROUS_OPS 常量集）。
  每次调用前，框架自动调用 ConsentManager 核查用户是否已授权：
    - once    仅本次命令允许
    - session 本会话内允许
    - project 本项目内允许
    - always  永久允许
  未授权时抛出 ConsentRequiredException，由上层（HermesEngine）呈现授权弹窗。
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Set

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

EXEC_SERVER = "server"
EXEC_CLIENT = "client"

VIS_PUBLIC    = "public"
VIS_PRIVATE   = "private"
VIS_EXCLUSIVE = "exclusive"

SRC_CODE  = "code"
SRC_USER  = "user"
SRC_AGENT = "agent"

CONSENT_ONCE         = "once"
CONSENT_SESSION      = "session"
CONSENT_PROJECT      = "project"
CONSENT_ALWAYS       = "always"
CONSENT_CONVERSATION = "conversation"  # 当前用户消息轮次内全部允许

# 需要用户同意才能执行的操作类型
DANGEROUS_OPS: FrozenSet[str] = frozenset({
    "modify",        # 修改文件/数据
    "delete",        # 删除文件/数据
    "cli",           # 本地 CLI 命令
    "write",         # 写文件/写数据库
    "admin",         # 管理员操作
    "sudo",          # 提权操作
    "execute_code",  # 执行任意代码
    # "network",       # 外发网络请求（隐私相关）
})

# 极危险操作：即使用户选择"当前会话同意"也需要逐次手动确认
CRITICAL_OPS: FrozenSet[str] = frozenset({
    "delete",        # 删除文件/数据（不可逆）
    "admin",         # 管理员权限操作
    "sudo",          # 提权操作
    # "execute_code",  # 执行任意代码
})


# ── 数据类 ───────────────────────────────────────────────────────────────────

@dataclass
class ClientExecRequest:
    """服务端向客户端发出的本地执行请求。

    当工具的 exec_location="client" 时，execute() 返回此对象而非直接运行。
    接入层（HermesEngine / API）负责将其转发给客户端，等待结果后继续。
    """
    tool_name:   str
    params:      Dict[str, Any]
    request_id:  str = field(default_factory=lambda: uuid.uuid4().hex)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type":        "client_exec_request",
            "tool_name":   self.tool_name,
            "params":      self.params,
            "request_id":  self.request_id,
            "description": self.description,
        }


@dataclass
class ConsentRequiredException(Exception):
    """工具包含危险操作，用户尚未授权时抛出此异常。

    上层（HermesEngine）捕获后向用户呈现授权选项，用户授权后重试工具调用。
    """
    tool_name:    str
    operation:    str              # 需要授权的危险操作类型
    user_id:      str
    session_id:   str = ""
    project_id:   str = ""
    request_id:   str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type":       "consent_required",
            "tool_name":  self.tool_name,
            "operation":  self.operation,
            "user_id":    self.user_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "consent_options": ["allow", "deny", "session"],
        }

    def __str__(self) -> str:
        return (
            f"工具 '{self.tool_name}' 包含危险操作 '{self.operation}'，"
            f"用户 {self.user_id} 尚未授权"
        )


# ── 基础类 ───────────────────────────────────────────────────────────────────

class BaseTool:
    """
    工具基础类。所有工具（无论哪种创建方式）均继承此类。

    代码方式（继承）::

        class WebSearchTool(BaseTool):
            name         = "web_search"
            description  = "搜索互联网获取实时信息"
            exec_location = EXEC_SERVER
            visibility   = VIS_PUBLIC
            parameters_schema = {
                "query":       {"type": "string",  "description": "搜索词",   "required": True},
                "max_results": {"type": "integer", "description": "最大结果数", "default": 5},
            }

            async def execute(self, params: dict, context: dict) -> dict:
                ...

    装饰器方式（见 app/tools/decorators.py @tool 装饰器）::

        @tool(name="web_search", description="...", exec_location=EXEC_SERVER)
        class WebSearchTool(BaseTool):
            async def execute(self, params, context):
                ...

    用户 / Agent 动态创建方式（见 app/tools/loader.py）：
        无需直接继承，由 ToolLoader 从代码字符串动态编译并注册。

    执行前自动完成：
      1. 危险操作同意核查（ConsentManager）
      2. 参数验证（parameters_schema）

    执行后自动完成：
      3. 调用统计异步写入 MySQL（tool_call_stats）
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """子类定义 execute() 时，自动包装以实现同意核查 + 统计埋点。"""
        super().__init_subclass__(**kwargs)
        if "execute" in cls.__dict__:
            _orig = cls.__dict__["execute"]

            async def _wrapped_execute(
                self: "BaseTool",
                params:  Dict[str, Any],
                context: Dict[str, Any],
                _f: Any = _orig,
            ) -> Dict[str, Any]:
                # ── 1. 危险操作同意核查 ─────────────────────────
                if self.dangerous_ops:
                    from app.tools.permission import consent_manager, get_consent_hook
                    user_id    = context.get("user_id", "")
                    session_id = context.get("session_id", "")
                    project_id = context.get("project_id", "")
                    for op in self.dangerous_ops:
                        if not await consent_manager.check_consented(
                            self.name, op, user_id, session_id, project_id
                        ):
                            hook = get_consent_hook()
                            if hook is not None:
                                exc = ConsentRequiredException(
                                    tool_name  = self.name,
                                    operation  = op,
                                    user_id    = user_id,
                                    session_id = session_id,
                                    project_id = project_id,
                                )
                                decision = await hook(exc)
                                if decision == "deny":
                                    return {
                                        "result":   f"用户拒绝了危险操作 '{op}'",
                                        "success":  False,
                                        "metadata": {"denied": True, "op": op},
                                    }
                                # allow / conversation：consent 已由 hook 授予，继续
                            else:
                                raise ConsentRequiredException(
                                    tool_name  = self.name,
                                    operation  = op,
                                    user_id    = user_id,
                                    session_id = session_id,
                                    project_id = project_id,
                                )

                # ── 2. 参数验证 ──────────────────────────────────
                if self.parameters_schema:
                    error = self._validate_params(params)
                    if error:
                        return {"result": error, "success": False, "metadata": {}}

                # ── 3. 执行工具 ──────────────────────────────────
                start = time.monotonic()
                success          = False
                error_info:      Optional[str] = None
                consent_required = False
                try:
                    result  = await _f(self, params, context)
                    success = result.get("success", True) if isinstance(result, dict) else True
                    return result
                except ConsentRequiredException:
                    consent_required = True
                    raise
                except Exception as e:
                    error_info = str(e)[:500]
                    logger.error("Tool 执行异常: name=%s error=%s", self.name, e)
                    raise
                finally:
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    asyncio.create_task(
                        self._record_call(context, success, elapsed_ms, error_info, consent_required)
                    )

            cls.execute = _wrapped_execute

    # ── 类变量（子类通过类变量或装饰器声明元信息）────────────────────────────
    name:              ClassVar[str]            = ""
    description:       ClassVar[str]            = ""
    source:            ClassVar[str]            = SRC_CODE
    visibility:        ClassVar[str]            = VIS_PUBLIC
    exec_location:     ClassVar[str]            = EXEC_SERVER
    owner_user_id:     ClassVar[Optional[str]]  = None
    owner_agent:       ClassVar[Optional[str]]  = None
    dangerous_ops:     ClassVar[List[str]]      = []
    parameters_schema: ClassVar[Dict[str, Any]] = {}

    def __init__(
        self,
        name:              Optional[str]            = None,
        description:       Optional[str]            = None,
        source:            Optional[str]            = None,
        visibility:        Optional[str]            = None,
        exec_location:     Optional[str]            = None,
        owner_user_id:     Optional[str]            = None,
        owner_agent:       Optional[str]            = None,
        dangerous_ops:     Optional[List[str]]      = None,
        parameters_schema: Optional[Dict[str, Any]] = None,
        tool_id:           Optional[str]            = None,
    ):
        self.name              = name          or self.__class__.name or self.__class__.__name__
        self.description       = description   if description   is not None else self.__class__.description
        self.source            = source        if source        is not None else self.__class__.source
        self.exec_location     = exec_location if exec_location is not None else self.__class__.exec_location
        self.owner_user_id     = owner_user_id if owner_user_id is not None else self.__class__.owner_user_id
        self.owner_agent       = owner_agent   if owner_agent   is not None else self.__class__.owner_agent
        self.dangerous_ops     = list(dangerous_ops)     if dangerous_ops     is not None else list(self.__class__.dangerous_ops)
        self.parameters_schema = dict(parameters_schema) if parameters_schema is not None else dict(self.__class__.parameters_schema)
        self.tool_id           = tool_id or uuid.uuid4().hex

        # agent 来源强制 exclusive
        _vis = visibility if visibility is not None else self.__class__.visibility
        if self.source == SRC_AGENT:
            self.visibility = VIS_EXCLUSIVE
        else:
            self.visibility = _vis

    # ── 执行接口 ─────────────────────────────────────────────────────────────

    async def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """默认实现：客户端工具返回执行请求；服务端工具需子类重写。"""
        if self.exec_location == EXEC_CLIENT:
            req = ClientExecRequest(
                tool_name   = self.name,
                params      = params,
                description = self.description,
            )
            return {
                "result":        req.to_dict(),
                "success":       True,
                "exec_location": EXEC_CLIENT,
                "metadata":      {"tool": self.name},
            }
        return {
            "result":  "该工具未实现 execute() 方法",
            "success": False,
            "metadata": {},
        }

    # ── 参数验证 ─────────────────────────────────────────────────────────────

    def _validate_params(self, params: Dict[str, Any]) -> Optional[str]:
        """根据 parameters_schema 校验必填参数，返回错误描述或 None（通过）。"""
        for param_name, schema in self.parameters_schema.items():
            if schema.get("required") and param_name not in params:
                return f"缺少必填参数: {param_name}"
        return None

    # ── 权限检查（辅助，直接用于工具注册层过滤）─────────────────────────────

    def is_available_for(self, user_id: str, agent_name: Optional[str] = None) -> bool:
        """判断当前工具对指定用户/agent 是否可用。"""
        if self.visibility == VIS_PUBLIC:
            return True
        if self.visibility == VIS_PRIVATE:
            return self.owner_user_id == user_id
        if self.visibility == VIS_EXCLUSIVE:
            return agent_name is not None and agent_name == self.owner_agent
        return False

    # ── 调用统计 ─────────────────────────────────────────────────────────────

    async def _record_call(
        self,
        context:          Dict[str, Any],
        success:          bool,
        exec_ms:          int,
        error_info:       Optional[str],
        consent_required: bool = False,
    ) -> None:
        """将一次调用记录异步写入 tool_call_stats，并更新评分统计。"""
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                INSERT INTO tool_call_stats
                    (tool_name, caller_user_id, caller_agent, success, exec_ms, error_info, called_at)
                VALUES
                    (:name, :user_id, :agent, :success, :ms, :err, :ts)
                """,
                {
                    "name":    self.name,
                    "user_id": context.get("user_id", ""),
                    "agent":   context.get("agent_name", ""),
                    "success": 1 if success else 0,
                    "ms":      exec_ms,
                    "err":     error_info,
                    "ts":      datetime.now(),
                },
            )
        except Exception as e:
            logger.debug("记录工具调用统计失败 tool=%s: %s", self.name, e)
        finally:
            if conn:
                await release_connection("mysql", conn)

        # ── 评分埋点 ─────────────────────────────────────────────────────────
        try:
            from app.scoring.manager import get_scoring_manager
            await get_scoring_manager().record_tool_call(
                tool_name=self.name,
                success=success,
                latency_ms=float(exec_ms),
                consent_required=consent_required,
            )
        except Exception as _se:
            logger.debug("工具评分记录失败 tool=%s: %s", self.name, _se)

    # ── 持久化（user/agent 来源工具写入数据库）──────────────────────────────

    async def save_to_db(self, code_source: str = "") -> bool:
        """将工具元信息持久化到 tools 表（仅 user/agent 来源需要）。"""
        if self.source == SRC_CODE:
            return False
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                INSERT INTO tools
                    (tool_id, name, description, source, visibility, exec_location,
                     owner_user_id, owner_agent, dangerous_ops, parameters_schema,
                     code_source, is_active)
                VALUES
                    (:tool_id, :name, :desc, :src, :vis, :loc,
                     :owner_user, :owner_agent, :ops, :params,
                     :code, 1)
                ON DUPLICATE KEY UPDATE
                    description       = VALUES(description),
                    visibility        = VALUES(visibility),
                    dangerous_ops     = VALUES(dangerous_ops),
                    parameters_schema = VALUES(parameters_schema),
                    code_source       = VALUES(code_source),
                    updated_at        = NOW()
                """,
                {
                    "tool_id":     self.tool_id,
                    "name":        self.name,
                    "desc":        self.description,
                    "src":         self.source,
                    "vis":         self.visibility,
                    "loc":         self.exec_location,
                    "owner_user":  self.owner_user_id,
                    "owner_agent": self.owner_agent,
                    "ops":         json.dumps(self.dangerous_ops),
                    "params":      json.dumps(self.parameters_schema),
                    "code":        code_source,
                },
            )
            return True
        except Exception as e:
            logger.warning("工具持久化失败 tool=%s: %s", self.name, e)
            return False
        finally:
            if conn:
                await release_connection("mysql", conn)

    # ── 元信息 ───────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id":          self.tool_id,
            "name":             self.name,
            "description":      self.description,
            "source":           self.source,
            "visibility":       self.visibility,
            "exec_location":    self.exec_location,
            "owner_user_id":    self.owner_user_id,
            "owner_agent":      self.owner_agent,
            "dangerous_ops":    self.dangerous_ops,
            "parameters_schema": self.parameters_schema,
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} name={self.name!r} "
            f"source={self.source!r} vis={self.visibility!r} "
            f"loc={self.exec_location!r}>"
        )
