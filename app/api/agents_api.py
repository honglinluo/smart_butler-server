"""Agent 管理 API - 创建、查询、评分、重载"""

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
from app.core.headers import ResponseHeaders
from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["Agents"])

_SCORE_WARN_THRESHOLD = 3.0   # 低于此分值发送提醒
_SCORE_MIN_RATINGS    = 5     # 至少有这么多评分才触发提醒


# ── 请求 / 响应模型 ──────────────────────────────────────────────────────────

class AgentCreate(BaseModel):
    name:       str         = Field(..., description="Agent 唯一名称（英文下划线）")
    role:       str         = Field(..., description="职责简述，如「数据分析工程师」")
    background: str         = Field("", description="详细背景描述，注入系统提示词")
    tools:      List[str]   = Field([], description="工具 ID 列表")
    is_public:  bool        = Field(False, description="是否公开（所有用户可用）")


class AgentUpdate(BaseModel):
    role:       Optional[str]       = None
    background: Optional[str]       = None
    tools:      Optional[List[str]] = None
    is_public:  Optional[bool]      = None


class AgentRating(BaseModel):
    score:   int            = Field(..., ge=1, le=5, description="评分 1-5")
    comment: Optional[str]  = Field(None, description="评论（可选）")


# ── 工具函数 ─────────────────────────────────────────────────────────────────

async def _load_db_agents_to_registry() -> int:
    """从 MySQL 加载所有启用的 DB Agent 并注册到 registry，返回加载数量。"""
    from app.agents.base import BaseAgent
    from app.agents.registry import registry

    conn = await get_connection("mysql", None)
    if not conn:
        return 0
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
    """查询 Agent 的评分统计（平均分 + 评分数）。"""
    conn = await get_connection("mysql", None)
    if not conn:
        return {"avg_score": 0.0, "rating_count": 0}
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
    """返回当前用户所有可用的 Agent 列表（代码 Agent + 公有/自有 DB Agent）。"""
    ResponseHeaders().apply(response)
    from app.agents.registry import registry
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
    """通过 API 创建 DB Agent（支持文字描述方式）。"""
    ResponseHeaders().apply(response)
    from app.agents.base import BaseAgent
    from app.agents.registry import registry

    user_id = current_user["user_id"]

    # 检查名称唯一性
    if registry.get(data.name):
        raise HTTPException(
            status_code=400,
            detail=f"Agent 名称 '{data.name}' 已存在",
        )

    conn = await get_connection("mysql", None)
    if not conn:
        raise HTTPException(status_code=500, detail="数据库连接失败")

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
    """更新 DB Agent（仅创建者可操作）。"""
    ResponseHeaders().apply(response)
    from app.agents.registry import registry
    user_id = current_user["user_id"]

    conn = await get_connection("mysql", None)
    if not conn:
        raise HTTPException(status_code=500, detail="数据库连接失败")
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
    """软删除 DB Agent（仅创建者可操作）。"""
    ResponseHeaders().apply(response)
    from app.agents.registry import registry
    user_id = current_user["user_id"]

    conn = await get_connection("mysql", None)
    if not conn:
        raise HTTPException(status_code=500, detail="数据库连接失败")
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
    """对公有 Agent 进行评分（每个用户只能评一次，可覆盖）。"""
    ResponseHeaders().apply(response)
    from app.agents.registry import registry
    user_id = current_user["user_id"]

    ag = registry.get(agent_name)
    if ag is None or not ag.is_public:
        raise HTTPException(status_code=404, detail="Agent 不存在或非公有")
    if ag.source == "code":
        raise HTTPException(status_code=400, detail="代码 Agent 不参与评分")
    if ag.user_id == user_id:
        raise HTTPException(status_code=400, detail="创建者不能给自己的 Agent 评分")

    conn = await get_connection("mysql", None)
    if not conn:
        raise HTTPException(status_code=500, detail="数据库连接失败")
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
    """获取当前用户创建的公有 Agent 的低分提醒及同类高分推荐。"""
    ResponseHeaders().apply(response)
    from app.agents.registry import registry
    user_id = current_user["user_id"]

    notifications = []
    conn = await get_connection("mysql", None)
    if not conn:
        return {"notifications": []}
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
                    "  AND a.agent_name != :name AND a.job LIKE :kw "
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
    from app.agents.registry import registry

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

