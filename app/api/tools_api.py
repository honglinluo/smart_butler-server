"""
【模块说明】工具管理 — 创建、查询、修改、删除工具，以及危险操作授权开关

这个文件负责管理 AI 可以调用的"工具"（Tools）。
工具是 AI 执行具体任务时使用的能力，例如：搜索网页、读取文件、执行命令等。

功能一览：
  - 查看工具列表（可用工具 + 支持按来源/执行位置过滤）
  - 创建工具（两种方式：直接写代码 OR 用文字描述由 AI 自动生成代码）
  - 修改工具的描述、可见性等基本信息（不可修改代码）
  - 删除工具（软删除，数据保留）
  - 危险操作开关管理：可以针对每种危险操作类型单独开关是否需要用户授权

【工具可见性】
  - public：所有用户都可以使用
  - private：仅创建者自己可使用
  - exclusive：仅绑定的 Agent 内部使用（不在列表中展示）

【危险操作声明】
  如果工具会执行危险动作（写文件、删除、执行命令等），
  必须在创建时声明 dangerous_ops，调用时系统会暂停等待用户授权。
"""


import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.api.dependencies import get_current_user, get_user_model
from app.utils.headers import ResponseHeaders
from app.tools.base import (
    EXEC_CLIENT, EXEC_SERVER,
    VIS_EXCLUSIVE, VIS_PRIVATE, VIS_PUBLIC,
    SRC_CODE, SRC_USER, SRC_AGENT,
    DANGEROUS_OPS,
)
from app.database.pool import get_connection, release_connection
from app.tools.permission import _invalidate_op_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["Tools"])

_VALID_EXEC_LOCATIONS = {EXEC_SERVER, EXEC_CLIENT}
_VALID_VISIBILITIES   = {VIS_PUBLIC, VIS_PRIVATE}   # 用户创建只允许 public/private
_VALID_SOURCES        = {SRC_CODE, SRC_USER, SRC_AGENT}


# ── 请求 / 响应模型 ────────────────────────────────────────────────────────────

class ToolSummary(BaseModel):
    """工具摘要（列表接口返回，不含代码）。"""
    tool_id:           str
    name:              str
    description:       str
    source:            str
    visibility:        str
    exec_location:     str
    owner_user_id:     Optional[str]
    owner_agent:       Optional[str]
    dangerous_ops:     List[str]
    parameters_schema: Dict[str, Any]


class ToolCreate(BaseModel):
    """创建工具的请求体。"""
    name: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="工具唯一名称（snake_case，如 web_search）",
    )
    description: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="工具功能描述，供 LLM 和前端展示",
    )
    input_type: str = Field(
        "code",
        description="创建方式：code（直接提交 Python 代码）或 doc（自然语言描述，由 LLM 生成代码）",
    )
    code_source: Optional[str] = Field(
        None,
        description="input_type=code 时必填；Python 函数或 BaseTool 子类代码字符串",
    )
    doc: Optional[str] = Field(
        None,
        description="input_type=doc 时必填；自然语言描述工具功能",
    )
    visibility: str = Field(
        VIS_PRIVATE,
        description="可见性：public（所有用户可用）或 private（仅自己可用）",
    )
    exec_location: str = Field(
        EXEC_SERVER,
        description="执行位置：server（服务端）或 client（客户端本地执行）",
    )
    dangerous_ops: List[str] = Field(
        [],
        description=f"危险操作类型列表，合法值：{sorted(DANGEROUS_OPS)}",
    )
    parameters_schema: Dict[str, Any] = Field(
        {},
        description=(
            '参数 schema，格式：{"param_name": {"type": "string", '
            '"description": "...", "required": true}}'
        ),
    )

    @field_validator("name")
    @classmethod
    def name_snake_case(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError("name 必须为 snake_case（小写字母、数字、下划线，字母开头）")
        return v

    @field_validator("input_type")
    @classmethod
    def valid_input_type(cls, v: str) -> str:
        if v not in ("code", "doc"):
            raise ValueError("input_type 只允许 'code' 或 'doc'")
        return v

    @field_validator("visibility")
    @classmethod
    def valid_visibility(cls, v: str) -> str:
        if v not in _VALID_VISIBILITIES:
            raise ValueError(f"visibility 只允许 {sorted(_VALID_VISIBILITIES)}")
        return v

    @field_validator("exec_location")
    @classmethod
    def valid_exec_location(cls, v: str) -> str:
        if v not in _VALID_EXEC_LOCATIONS:
            raise ValueError(f"exec_location 只允许 {sorted(_VALID_EXEC_LOCATIONS)}")
        return v

    @field_validator("dangerous_ops")
    @classmethod
    def valid_dangerous_ops(cls, v: List[str]) -> List[str]:
        invalid = set(v) - DANGEROUS_OPS
        if invalid:
            raise ValueError(f"不合法的 dangerous_ops 值：{invalid}，合法值：{sorted(DANGEROUS_OPS)}")
        return v


class ToolCreateResponse(BaseModel):
    """创建工具的响应。"""
    tool_id:       str
    name:          str
    description:   str
    source:        str
    visibility:    str
    exec_location: str
    dangerous_ops: List[str]
    message:       str


class ToolUpdate(BaseModel):
    """修改工具的请求体（仅允许修改元信息，不修改代码）。"""
    description: Optional[str] = Field(
        None, min_length=5, max_length=500, description="新的功能描述"
    )
    visibility: Optional[str] = Field(
        None, description="可见性：public 或 private"
    )
    exec_location: Optional[str] = Field(
        None, description="执行位置：server 或 client"
    )

    @field_validator("visibility")
    @classmethod
    def valid_visibility(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_VISIBILITIES:
            raise ValueError(f"visibility 只允许 {sorted(_VALID_VISIBILITIES)}")
        return v

    @field_validator("exec_location")
    @classmethod
    def valid_exec_location(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_EXEC_LOCATIONS:
            raise ValueError(f"exec_location 只允许 {sorted(_VALID_EXEC_LOCATIONS)}")
        return v


# ── 依赖：从 app.state 获取 hermes_engine ─────────────────────────────────────

async def _get_engine(request: Request):
    engine = getattr(request.app.state, "hermes_engine", None)
    if not engine:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务引擎未就绪，请稍后重试",
        )
    return engine


# ══════════════════════════════════════════════════════════════════════════════
# GET /tools  — 查询当前用户可用的工具列表
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "",
    response_model=List[ToolSummary],
    summary="查询当前用户可用的工具",
    description=(
        "返回当前登录用户有权调用的所有工具。\n\n"
        "**过滤规则（visibility）**\n"
        "- `public`：返回所有 public 工具\n"
        "- `private`：仅返回该用户自己创建的 private 工具\n"
        "- `exclusive`：不在此接口返回（专用工具由 agent 内部调用）\n\n"
        "支持按 `source`、`exec_location` 进一步过滤。"
    ),
)
async def list_tools(
    response: Response,
    source:        Optional[str] = Query(None, description=f"按来源过滤：{sorted(_VALID_SOURCES)}"),
    exec_location: Optional[str] = Query(None, description="按执行位置过滤：server / client"),
    current_user: dict = Depends(get_current_user),
) -> List[ToolSummary]:
    """查询当前用户可用的工具列表（排除 exclusive 专用工具）。"""
    ResponseHeaders().apply(response)
    # 参数校验
    if source and source not in _VALID_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"source 不合法，允许值：{sorted(_VALID_SOURCES)}",
        )
    if exec_location and exec_location not in _VALID_EXEC_LOCATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"exec_location 不合法，允许值：{sorted(_VALID_EXEC_LOCATIONS)}",
        )

    from app.tools.registry import registry

    user_id = current_user["user_id"]

    # 按用户权限过滤（排除 exclusive 专用工具）
    available = [
        t for t in registry.list_available_for(user_id=user_id, agent_name=None)
        if t.visibility != VIS_EXCLUSIVE
    ]

    # 附加过滤条件
    if source:
        available = [t for t in available if t.source == source]
    if exec_location:
        available = [t for t in available if t.exec_location == exec_location]

    return [
        ToolSummary(
            tool_id           = t.tool_id,
            name              = t.name,
            description       = t.description,
            source            = t.source,
            visibility        = t.visibility,
            exec_location     = t.exec_location,
            owner_user_id     = t.owner_user_id,
            owner_agent       = t.owner_agent,
            dangerous_ops     = t.dangerous_ops,
            parameters_schema = t.parameters_schema,
        )
        for t in available
    ]


# ══════════════════════════════════════════════════════════════════════════════
# POST /tools  — 用户创建工具
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "",
    response_model=ToolCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="用户创建工具",
    description=(
        "支持两种创建方式：\n\n"
        "**方式一 `input_type=code`**（直接提交代码）\n"
        "```python\n"
        "# 函数式（简单推荐）\n"
        "async def execute(params, context):\n"
        "    url = params['url']\n"
        "    ...\n"
        "    return {'result': ..., 'success': True, 'metadata': {}}\n"
        "```\n\n"
        "**方式二 `input_type=doc`**（自然语言描述，LLM 自动生成代码）\n"
        "```json\n"
        '{"input_type": "doc", "doc": "获取指定 URL 的 HTTP 状态码", ...}\n'
        "```\n\n"
        "**危险操作声明**\n"
        "如工具包含文件写入、删除、CLI 执行等操作，必须在 `dangerous_ops` 中声明，"
        "调用时将要求用户授权。\n\n"
        "**安全限制**\n"
        "代码中禁止导入：os、subprocess、sys、shutil、socket 等高危模块。"
    ),
)
async def create_tool(
    body:         ToolCreate,
    request:      Request,
    response:     Response,
    current_user: dict = Depends(get_current_user),
    user_model:   dict = Depends(get_user_model),
    engine             = Depends(_get_engine),
) -> ToolCreateResponse:
    """用户通过代码或自然语言描述创建工具，立即注册并持久化。"""
    ResponseHeaders().apply(response)
    from app.tools.registry import registry
    from app.tools.loader import ToolLoader

    user_id = current_user["user_id"]

    # ── 1. 工具名称唯一性检查 ─────────────────────────────────────────────────
    if registry.get(body.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"工具名称 '{body.name}' 已存在，请换一个名称",
        )

    # ── 2. 按 input_type 分支处理 ────────────────────────────────────────────
    try:
        if body.input_type == "code":
            # 方式一：直接提交代码
            if not body.code_source or not body.code_source.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="input_type=code 时 code_source 不能为空",
                )
            new_tool = await ToolLoader.from_user_code(
                code_source       = body.code_source,
                name              = body.name,
                description       = body.description,
                owner_user_id     = user_id,
                visibility        = body.visibility,
                exec_location     = body.exec_location,
                dangerous_ops     = body.dangerous_ops,
                parameters_schema = body.parameters_schema,
            )

        else:
            # 方式二：自然语言描述，LLM 生成代码
            if not body.doc or not body.doc.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="input_type=doc 时 doc 不能为空",
                )
            # 构建 LLM 实例（复用 chat.py 的构建模式）
            llm_instance = None
            try:
                llm_info     = engine.LLMInfo(
                    model_name  = user_model["model_name"],
                    api_key     = user_model["api_key"],
                    url         = user_model["url"],
                    temperature = float(user_model.get("temperature", 0.7)),
                    model_type  = user_model.get("model_type", "chat"),
                )
                llm_instance = await engine._build_llm_from_config(llm_info)
            except Exception as e:
                logger.warning("构建 LLM 实例失败 user=%s: %s", user_id, e)

            if llm_instance is None:
                raise HTTPException(
                    status_code=status.HTTP_424_FAILED_DEPENDENCY,
                    detail="input_type=doc 需要可用的 LLM，请先在「模型管理」中配置模型",
                )

            new_tool = await ToolLoader.from_doc(
                doc               = body.doc,
                name              = body.name,
                owner_user_id     = user_id,
                llm               = llm_instance,
                description       = body.description,
                visibility        = body.visibility,
                exec_location     = body.exec_location,
                dangerous_ops     = body.dangerous_ops,
                parameters_schema = body.parameters_schema,
            )

    except HTTPException:
        raise
    except ValueError as e:
        # ToolLoader 的代码校验失败
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except Exception as e:
        logger.error("创建工具失败 user=%s name=%s: %s", user_id, body.name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"工具创建失败：{e}",
        )

    # ── 3. 构造响应 ───────────────────────────────────────────────────────────
    ops_hint = ""
    if new_tool.dangerous_ops:
        ops_hint = f"；包含危险操作 {new_tool.dangerous_ops}，调用时将要求用户授权"

    return ToolCreateResponse(
        tool_id       = new_tool.tool_id,
        name          = new_tool.name,
        description   = new_tool.description,
        source        = new_tool.source,
        visibility    = new_tool.visibility,
        exec_location = new_tool.exec_location,
        dangerous_ops = new_tool.dangerous_ops,
        message       = f"工具 '{new_tool.name}' 创建成功{ops_hint}",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 危险操作类型配置接口（固定路径必须在 /{tool_id} 参数路由之前注册）
# GET  /tools/dangerous-ops        — 查询用户的危险操作类型开关状态
# PATCH /tools/dangerous-ops/{op}  — 修改单个操作类型的开关状态
# ══════════════════════════════════════════════════════════════════════════════

_OP_LABELS: dict = {
    "modify":       "修改文件/数据",
    "delete":       "删除文件/数据",
    "cli":          "本地 CLI 命令",
    "write":        "写文件/写数据库",
    "admin":        "管理员操作",
    "sudo":         "提权操作",
    "execute_code": "执行任意代码",
}


class DangerousOpStatus(BaseModel):
    op_type:    str
    label:      str
    is_enabled: bool


class DangerousOpToggle(BaseModel):
    is_enabled: bool


@router.get(
    "/dangerous-ops",
    response_model=List[DangerousOpStatus],
    summary="查询用户的危险操作类型开关",
)
async def list_dangerous_ops(
    response: Response,
    current_user: dict = Depends(get_current_user),
) -> List[DangerousOpStatus]:
    """返回所有危险操作类型及当前用户的开关状态（无记录 = 默认开启）。"""
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]

    conn = None
    try:
        conn = await get_connection("mysql", None)
        user_configs: dict = {}
        if conn:
            rows = await conn.execute_raw(
                "SELECT op_type, is_enabled FROM dangerous_op_configs WHERE user_id = :uid",
                {"uid": user_id},
            )
            if rows is not None and len(rows) > 0:
                for _, row in rows.iterrows():
                    user_configs[str(row["op_type"])] = bool(row["is_enabled"])

        return [
            DangerousOpStatus(
                op_type    = op,
                label      = _OP_LABELS.get(op, op),
                is_enabled = user_configs.get(op, True),
            )
            for op in sorted(DANGEROUS_OPS)
        ]
    except Exception as e:
        logger.error("查询危险操作配置失败 user=%s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="查询失败")
    finally:
        if conn:
            await release_connection("mysql", conn)


@router.patch(
    "/dangerous-ops/{op_type}",
    response_model=dict,
    summary="修改危险操作类型的开关状态",
)
async def toggle_dangerous_op(
    op_type:      str,
    body:         DangerousOpToggle,
    response:     Response,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """开启或关闭指定危险操作类型（用户级别，关闭后该类操作无需授权即可执行）。"""
    ResponseHeaders().apply(response)

    if op_type not in DANGEROUS_OPS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"op_type 不合法，合法值：{sorted(DANGEROUS_OPS)}",
        )

    user_id = current_user["user_id"]
    conn = None
    try:
        conn = await get_connection("mysql", None)
        await conn.execute_raw(
            """
            INSERT INTO dangerous_op_configs (user_id, op_type, is_enabled)
            VALUES (:uid, :op, :enabled)
            ON DUPLICATE KEY UPDATE is_enabled = VALUES(is_enabled)
            """,
            {"uid": user_id, "op": op_type, "enabled": int(body.is_enabled)},
        )
        _invalidate_op_cache(user_id, op_type)
        state = "开启" if body.is_enabled else "关闭"
        return {"message": f"危险操作 '{op_type}' 已{state}", "is_enabled": body.is_enabled}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("修改危险操作配置失败 user=%s op=%s: %s", user_id, op_type, e)
        raise HTTPException(status_code=500, detail="修改失败")
    finally:
        if conn:
            await release_connection("mysql", conn)


# ══════════════════════════════════════════════════════════════════════════════
# PUT /tools/{tool_id}  — 修改工具（仅创建者）
# ══════════════════════════════════════════════════════════════════════════════

@router.put(
    "/{tool_id}",
    response_model=dict,
    summary="修改工具",
    description="修改工具的描述、可见性或执行位置，仅工具创建者可操作。",
)
async def update_tool(
    tool_id:      str,
    body:         ToolUpdate,
    response:     Response,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """修改工具元信息（description / visibility / exec_location）。"""
    ResponseHeaders().apply(response)
    from app.tools.registry import registry
    from app.database.pool import get_connection, release_connection

    user_id = current_user["user_id"]

    # ── 1. 从 DB 校验归属 ────────────────────────────────────────────────────
    conn = None
    try:
        conn = await get_connection("mysql", None)

        df = await conn.execute_raw(
            "SELECT tool_id, name, owner_user_id, description, visibility, exec_location "
            "FROM tools WHERE tool_id = :tid AND is_active = 1",
            {"tid": tool_id},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="工具不存在")

        row = df.iloc[0]
        if str(row["owner_user_id"]) != user_id:
            raise HTTPException(status_code=403, detail="无权限修改此工具，仅创建者可操作")

        # ── 2. 合并更新字段 ──────────────────────────────────────────────────
        new_desc   = body.description  if body.description  is not None else str(row["description"])
        new_vis    = body.visibility   if body.visibility    is not None else str(row["visibility"])
        new_loc    = body.exec_location if body.exec_location is not None else str(row["exec_location"])

        await conn.execute_raw(
            "UPDATE tools SET description=:desc, visibility=:vis, exec_location=:loc "
            "WHERE tool_id=:tid",
            {"desc": new_desc, "vis": new_vis, "loc": new_loc, "tid": tool_id},
        )

        # ── 3. 同步更新 registry ─────────────────────────────────────────────
        tool_name = str(row["name"])
        t = registry.get(tool_name)
        if t:
            if body.description  is not None: t.description  = new_desc
            if body.visibility   is not None: t.visibility   = new_vis
            if body.exec_location is not None: t.exec_location = new_loc

        return {"message": f"工具 '{tool_name}' 更新成功"}

    finally:
        if conn:
            await release_connection("mysql", conn)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE /tools/{tool_id}  — 删除工具（仅创建者）
# ══════════════════════════════════════════════════════════════════════════════

@router.delete(
    "/{tool_id}",
    response_model=dict,
    summary="删除工具",
    description="软删除工具（is_active=0）并从注册表中注销，仅工具创建者可操作。",
)
async def delete_tool(
    tool_id:      str,
    response:     Response,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """删除用户自己创建的工具（软删除）。"""
    ResponseHeaders().apply(response)
    from app.tools.registry import registry
    from app.database.pool import get_connection, release_connection

    user_id = current_user["user_id"]

    conn = None
    try:
        conn = await get_connection("mysql", None)

        df = await conn.execute_raw(
            "SELECT tool_id, name, owner_user_id FROM tools WHERE tool_id = :tid AND is_active = 1",
            {"tid": tool_id},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="工具不存在")

        row = df.iloc[0]
        if str(row["owner_user_id"]) != user_id:
            raise HTTPException(status_code=403, detail="无权限删除此工具，仅创建者可操作")

        await conn.execute_raw(
            "UPDATE tools SET is_active = 0 WHERE tool_id = :tid",
            {"tid": tool_id},
        )

        # 从 registry 注销
        tool_name = str(row["name"])
        registry.unregister(tool_name)

        return {"message": f"工具 '{tool_name}' 已删除"}

    finally:
        if conn:
            await release_connection("mysql", conn)
