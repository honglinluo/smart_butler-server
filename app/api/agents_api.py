"""
【模块说明】AI 助手（Agent）管理 — 创建、查询、评分、重新加载

这个文件负责管理系统中的 AI 助手（Agent）。Agent 是具备特定能力和职责的 AI 角色，
例如"数据分析师 Agent"、"客服 Agent"、"代码助手 Agent"等。

Agent 分为两类：
  - 代码 Agent（source=code）：由开发者在代码中定义，功能稳定，全局共享
  - 数据库 Agent（source=db）：由用户通过页面/API 创建，灵活自定义

用户可以：
  - 查看所有可用 Agent 列表（自己创建的 + 公开的）
  - 创建自己的自定义 Agent（设置名称、职责、背景描述、可用工具）
  - 修改或删除自己创建的 Agent
  - 对其他人公开的 Agent 进行 1-5 星评分
  - 查看自己 Agent 的评分通知（评分过低时提示优化）

【评分告警】
  当公开 Agent 收到 5 条以上评分且平均分低于 3.0 时，创建者会看到告警通知，
  并推荐同类高分 Agent 供参考改进。
"""


import glob
import importlib
import json
import logging
import os
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user
from app.utils.headers import ResponseHeaders
from app.database.pool import get_connection, release_connection
from app.agents.base import BaseAgent
from app.agents.registry import registry
from app.tools.registry import registry as tool_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["Agents"])

_SCORE_WARN_THRESHOLD = 3.0   # 低于此分值发送提醒
_SCORE_MIN_RATINGS    = 5     # 至少有这么多评分才触发提醒


# ── 请求 / 响应模型 ──────────────────────────────────────────────────────────

class AgentCreate(BaseModel):
    """
    创建新 Agent 时提交的信息。
    name：全局唯一的英文标识符，用下划线连接单词，如 data_analyst
    role：职责简述，一句话说明这个 Agent 是做什么的，如"数据分析工程师"
    background：详细的背景描述，会注入到 AI 的系统提示词中，影响 Agent 的行为风格
    tools：该 Agent 可以使用的工具 ID 列表（空列表则不绑定特定工具）
    is_public：是否公开给所有用户使用（默认私有，仅自己可调用）
    """
    name:       str         = Field(..., description="Agent 唯一名称（英文下划线）")
    role:       str         = Field(..., description="职责简述，如「数据分析工程师」")
    background: str         = Field("", description="详细背景描述，注入系统提示词")
    tools:      List[str]   = Field([], description="工具 ID 列表")
    is_public:  bool        = Field(False, description="是否公开（所有用户可用）")


class AgentUpdate(BaseModel):
    """修改 Agent 信息时提交的字段（不填则保持原值不变）。"""
    role:       Optional[str]       = None
    background: Optional[str]       = None
    tools:      Optional[List[str]] = None
    is_public:  Optional[bool]      = None


class AgentRating(BaseModel):
    """对 Agent 进行评分时提交的信息。score 范围 1-5 星，comment 为可选文字评价。"""
    score:   int            = Field(..., ge=1, le=5, description="评分 1-5")
    comment: Optional[str]  = Field(None, description="评论（可选）")


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _validate_tools(tools: List[str], user_id: str) -> None:
    """校验工具列表：每个工具必须存在于注册表中，且当前用户有权限使用。

    可使用的工具包括：系统内置（code/public）、公开的用户工具（public）
    以及当前用户自己创建的私有工具（private/owner_user_id == user_id）。
    """
    unavailable = []
    for tool_name in tools:
        tool = tool_registry.get(tool_name)
        if tool is None or not tool.is_available_for(user_id):
            unavailable.append(tool_name)
    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"以下工具不存在、状态不可用或当前用户无权限使用: {', '.join(unavailable)}",
        )


async def _load_db_agents_to_registry() -> int:
    """
    从数据库读取所有已启用的用户自定义 Agent，并加载到内存注册表中使其可被调用。
    返回成功加载的 Agent 数量。
    通常在服务启动或调用"重新加载"接口时执行。
    """
    conn = await get_connection("mysql", None)
    loaded = 0
    try:
        df = await conn.execute_raw(
            "SELECT id, agent_name, job, `desc`, `public`, user_id "
            "FROM agents WHERE state = 1",
            {},
        )
        if df is None or len(df) == 0:
            return 0
        registry.clear_db_agents()
        for _, row in df.iterrows():
            try:
                desc_data = json.loads(row["desc"]) if row.get("desc") else {}
                ag = BaseAgent(
                    name       =str(row["agent_name"]),
                    role       =str(row.get("job", "")),
                    background =str(desc_data.get("background", "")),
                    tools      =list(desc_data.get("tools", [])),
                    is_public  =bool(row.get("public", 0)),
                    source     ="db",
                    user_id    =str(row.get("user_id", "0")),
                    db_id      =int(row["id"]),
                )
                registry.register(ag)
                loaded += 1
            except Exception as e:
                logger.warning("加载 DB Agent 失败: id=%s err=%s", row.get("id"), e)
    except Exception as e:
        logger.error("批量加载 DB Agent 异常: %s", e)
    finally:
        await release_connection("mysql", conn)
    return loaded


async def _get_agent_ratings(agent_name: str) -> dict:
    """查询指定 Agent 的评分汇总：平均分（avg_score）和总评分条数（rating_count）。"""
    conn = await get_connection("mysql", None)
    try:
        df = await conn.execute_raw(
            "SELECT AVG(score) AS avg_score, COUNT(*) AS rating_count "
            "FROM agent_ratings WHERE agent_name = :name",
            {"name": agent_name},
        )
        if df is not None and len(df) > 0:
            row = df.iloc[0]
            return {
                "avg_score":    round(float(row.get("avg_score") or 0), 2),
                "rating_count": int(row.get("rating_count") or 0),
            }
    except Exception:
        pass
    finally:
        await release_connection("mysql", conn)
    return {"avg_score": 0.0, "rating_count": 0}


# ── API 端点 ─────────────────────────────────────────────────────────────────

@router.get("", response_model=dict)
async def list_agents(response: Response, current_user: dict = Depends(get_current_user)):
    """
    获取当前用户可以使用的全部 Agent 列表，包括：
      - 系统内置的代码 Agent（所有用户共用）
      - 用户自己创建的私有 Agent
      - 其他用户公开的 Agent
    对公开的数据库 Agent 同时附带评分统计信息。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]
    agents  = registry.list_available_for_user(user_id)

    result = []
    for ag in agents:
        info = ag.to_dict()
        if ag.is_public and ag.source == "db":
            ratings = await _get_agent_ratings(ag.name)
            info.update(ratings)
        result.append(info)

    return {"agents": result, "total": len(result)}


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """
    创建一个新的自定义 Agent，保存到数据库并立即在内存中注册（创建后即可使用）。
    Agent 名称全局唯一，不可重复。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]

    # 检查名称唯一性
    if registry.get(data.name):
        raise HTTPException(
            status_code=400,
            detail=f"Agent 名称 '{data.name}' 已存在",
        )

    # 校验工具列表
    if data.tools:
        _validate_tools(data.tools, user_id)

    conn = await get_connection("mysql", None)

    try:
        desc_json = json.dumps(
            {"background": data.background, "tools": data.tools},
            ensure_ascii=False,
        )
        await conn.execute_raw(
            "INSERT INTO agents (agent_name, user_id, job, `desc`, `public`, state, created_at, updated_at) "
            "VALUES (:name, :uid, :job, :desc, :pub, 1, :now, :now)",
            {
                "name": data.name,
                "uid":  user_id,
                "job":  data.role,
                "desc": desc_json,
                "pub":  1 if data.is_public else 0,
                "now":  datetime.now(),
            },
        )
        # 同步注册到 registry
        ag = BaseAgent(
            name       =data.name,
            role       =data.role,
            background =data.background,
            tools      =data.tools,
            is_public  =data.is_public,
            source     ="db",
            user_id    =user_id,
        )
        registry.register(ag)
        return {"message": "Agent 创建成功", "name": data.name}
    finally:
        await release_connection("mysql", conn)


@router.put("/{agent_name}", response_model=dict)
async def update_agent(
    agent_name: str,
    data: AgentUpdate,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """修改自定义 Agent 的职责、背景描述、工具绑定或公开状态。仅 Agent 的创建者有权修改。"""
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]

    conn = await get_connection("mysql", None)
    try:
        df = await conn.execute_raw(
            "SELECT id, user_id, job, `desc`, `public` "
            "FROM agents WHERE agent_name = :name AND state = 1",
            {"name": agent_name},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        row = df.iloc[0]
        if str(row["user_id"]) != user_id:
            raise HTTPException(status_code=403, detail="无权限修改此 Agent")

        desc_data = json.loads(row["desc"]) if row.get("desc") else {}
        new_job    = data.role       if data.role       is not None else str(row["job"])
        new_pub    = data.is_public  if data.is_public  is not None else bool(row["public"])
        if data.background is not None:
            desc_data["background"] = data.background
        if data.tools is not None:
            # 校验工具列表
            if data.tools:
                _validate_tools(data.tools, user_id)
            desc_data["tools"] = data.tools

        await conn.execute_raw(
            "UPDATE agents SET job=:job, `desc`=:desc, `public`=:pub, updated_at=:now "
            "WHERE agent_name=:name",
            {
                "job":  new_job,
                "desc": json.dumps(desc_data, ensure_ascii=False),
                "pub":  1 if new_pub else 0,
                "now":  datetime.now(),
                "name": agent_name,
            },
        )
        # 更新 registry 中的实例
        ag = registry.get(agent_name)
        if ag:
            if data.role is not None:
                ag.role = data.role
            if data.background is not None:
                ag.background = data.background
            if data.tools is not None:
                ag.tools = data.tools
            if data.is_public is not None:
                ag.is_public = data.is_public
            ag.invalidate_skills_cache()

        return {"message": "Agent 更新成功"}
    finally:
        await release_connection("mysql", conn)


@router.delete("/{agent_name}", response_model=dict)
async def delete_agent(
    agent_name: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """
    删除自定义 Agent（软删除：把数据库中的 state 标记为 -1，数据不真正删除）。
    同时从内存注册表中移除，立即生效。仅 Agent 的创建者有权删除。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]

    conn = await get_connection("mysql", None)
    try:
        df = await conn.execute_raw(
            "SELECT user_id FROM agents WHERE agent_name = :name AND state = 1",
            {"name": agent_name},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        if str(df.iloc[0]["user_id"]) != user_id:
            raise HTTPException(status_code=403, detail="无权限删除此 Agent")

        await conn.execute_raw(
            "UPDATE agents SET state = -1, updated_at = :now WHERE agent_name = :name",
            {"now": datetime.now(), "name": agent_name},
        )
        registry.unregister(agent_name)
        return {"message": "Agent 已删除"}
    finally:
        await release_connection("mysql", conn)


@router.post("/{agent_name}/rate", response_model=dict)
async def rate_agent(
    agent_name: str,
    data: AgentRating,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """
    对其他人公开的 Agent 进行 1-5 星评分，可附文字评论。
    每个用户对同一 Agent 只能评一次，重复提交会覆盖之前的评分。
    创建者不能给自己的 Agent 评分，系统内置代码 Agent 不参与评分。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]

    ag = registry.get(agent_name)
    if ag is None or not ag.is_public:
        raise HTTPException(status_code=404, detail="Agent 不存在或非公有")
    if ag.source == "code":
        raise HTTPException(status_code=400, detail="代码 Agent 不参与评分")
    if ag.user_id == user_id:
        raise HTTPException(status_code=400, detail="创建者不能给自己的 Agent 评分")

    conn = await get_connection("mysql", None)
    try:
        await conn.execute_raw(
            "INSERT INTO agent_ratings (agent_name, user_id, score, comment, created_at) "
            "VALUES (:name, :uid, :score, :comment, :now) "
            "ON DUPLICATE KEY UPDATE score=:score, comment=:comment, created_at=:now",
            {
                "name":    agent_name,
                "uid":     user_id,
                "score":   data.score,
                "comment": data.comment or "",
                "now":     datetime.now(),
            },
        )
        ratings = await _get_agent_ratings(agent_name)
        return {"message": "评分成功", **ratings}
    finally:
        await release_connection("mysql", conn)


@router.get("/notifications/my", response_model=dict)
async def get_my_notifications(response: Response, current_user: dict = Depends(get_current_user)):
    """
    获取当前用户创建的公开 Agent 的评分告警通知。
    当某个 Agent 评分数超过 5 且平均分低于 3.0 时，
    系统会提醒创建者并推荐同类评分较高的 Agent 供参考改进。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]

    notifications = []
    conn = await get_connection("mysql", None)
    try:
        # 查询当前用户的公有 DB Agent
        df = await conn.execute_raw(
            "SELECT agent_name, job FROM agents WHERE user_id = :uid AND `public` = 1 AND state = 1",
            {"uid": user_id},
        )
        if df is None or len(df) == 0:
            return {"notifications": []}

        for _, row in df.iterrows():
            name = str(row["agent_name"])
            ratings = await _get_agent_ratings(name)
            if (ratings["rating_count"] >= _SCORE_MIN_RATINGS
                    and ratings["avg_score"] < _SCORE_WARN_THRESHOLD):
                # 查询同类评分更高的 Agent（相同 job 关键词）
                job_kw = str(row["job"])[:10]
                df_rec = await conn.execute_raw(
                    "SELECT a.agent_name, AVG(r.score) AS avg_score, COUNT(r.id) AS cnt "
                    "FROM agents a JOIN agent_ratings r ON a.agent_name = r.agent_name "
                    "WHERE a.`public` = 1 AND a.state = 1 "
                    "AND a.agent_name != :name AND a.job LIKE :kw "
                    "GROUP BY a.agent_name HAVING avg_score >= :threshold ORDER BY avg_score DESC LIMIT 3",
                    {
                        "name": name,
                        "kw": f"%{job_kw}%",
                        "threshold": _SCORE_WARN_THRESHOLD,
                    },
                )
                recommendations = []
                if df_rec is not None and len(df_rec) > 0:
                    recommendations = [
                        {"agent_name": r["agent_name"], "avg_score": round(float(r["avg_score"]), 2)}
                        for _, r in df_rec.iterrows()
                    ]
                notifications.append({
                    "type":            "low_rating",
                    "agent_name":      name,
                    "avg_score":       ratings["avg_score"],
                    "rating_count":    ratings["rating_count"],
                    "message":         f"你的 Agent「{name}」评分较低（{ratings['avg_score']:.1f}/5），建议优化。",
                    "recommendations": recommendations,
                })
    except Exception as e:
        logger.error("获取通知失败: %s", e)
    finally:
        await release_connection("mysql", conn)

    return {"notifications": notifications}


@router.post("/admin/reload", response_model=dict)
async def reload_code_agents(response: Response, current_user: dict = Depends(get_current_user)):
    """
    重新扫描并注册所有代码 Agent（扫描 app/agents/workers/ 目录）。
    同时重新加载所有 DB Agent。
    """
    ResponseHeaders().apply(response)

    # 1. 扫描 workers 目录，重新导入模块（触发 @agent 装饰器）
    workers_dir = os.path.join(os.path.dirname(__file__), "../agents/workers")
    loaded_modules = []
    for path in glob.glob(os.path.join(workers_dir, "*.py")):
        mod_file = os.path.basename(path)
        if mod_file.startswith("_"):
            continue
        mod_name = f"app.agents.workers.{mod_file[:-3]}"
        try:
            if mod_name in __import__("sys").modules:
                importlib.reload(__import__("sys").modules[mod_name])
            else:
                importlib.import_module(mod_name)
            loaded_modules.append(mod_name)
        except Exception as e:
            logger.error("重载模块失败: %s err=%s", mod_name, e)

    # 2. 重新加载 DB Agent
    db_count = await _load_db_agents_to_registry()

    return {
        "message":          "重载完成",
        "loaded_modules":   loaded_modules,
        "db_agents_loaded": db_count,
        "registered":       registry.names(),
    }
