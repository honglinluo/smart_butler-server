"""
【模块说明】评分管理 API — 查询和管理 Agent / Tool 的评分数据

GET  /scoring/agents              查询 Top-N Agent 综合评分
GET  /scoring/agents/{name}       查询指定 Agent 的评分详情与原始统计
GET  /scoring/tools               查询 Top-N Tool 综合评分
GET  /scoring/tools/{name}        查询指定 Tool 的评分详情与原始统计
GET  /scoring/weights             查询当前评分权重配置
PUT  /scoring/weights             更新评分权重配置
DELETE /scoring/agents/{name}     重置指定 Agent 的统计数据
DELETE /scoring/tools/{name}      重置指定 Tool 的统计数据
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.scoring.manager import get_scoring_manager
from app.scoring.models import ScoreWeights

logger  = logging.getLogger(__name__)
router  = APIRouter(prefix="/scoring", tags=["scoring"])


# ── 请求 / 响应体 ────────────────────────────────────────────────────────────────

class WeightsUpdateRequest(BaseModel):
    agent_success:       Optional[float] = None
    agent_latency:       Optional[float] = None
    agent_quality:       Optional[float] = None
    agent_popularity:    Optional[float] = None
    tool_success:        Optional[float] = None
    tool_latency:        Optional[float] = None
    tool_popularity:     Optional[float] = None
    tool_danger_penalty: Optional[float] = None


# ── Agent ────────────────────────────────────────────────────────────────────────

@router.get("/agents", summary="Top-N Agent 综合评分")
async def list_agent_scores(
    top: int = Query(default=10, ge=1, le=100, description="返回前 N 名"),
) -> List[Dict[str, Any]]:
    sm = get_scoring_manager()
    scores = await sm.get_top_agents(top)
    return [s.to_dict() for s in scores]


@router.get("/agents/{agent_name}", summary="Agent 评分详情")
async def get_agent_score(agent_name: str) -> Dict[str, Any]:
    sm    = get_scoring_manager()
    score = await sm.get_agent_score(agent_name)
    if score is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' 暂无评分数据")
    stats = await sm.get_agent_stats(agent_name)
    return {
        "score": score.to_dict(),
        "raw_stats": stats.to_dict() if stats else None,
    }


@router.delete("/agents/{agent_name}", summary="重置 Agent 统计数据")
async def reset_agent_stats(agent_name: str) -> Dict[str, Any]:
    sm = get_scoring_manager()
    ok = await sm.reset_agent(agent_name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' 暂无统计数据")
    return {"success": True, "message": f"Agent '{agent_name}' 统计数据已重置"}


# ── Tool ─────────────────────────────────────────────────────────────────────────

@router.get("/tools", summary="Top-N Tool 综合评分")
async def list_tool_scores(
    top: int = Query(default=10, ge=1, le=100, description="返回前 N 名"),
) -> List[Dict[str, Any]]:
    sm = get_scoring_manager()
    scores = await sm.get_top_tools(top)
    return [s.to_dict() for s in scores]


@router.get("/tools/{tool_name}", summary="Tool 评分详情")
async def get_tool_score(tool_name: str) -> Dict[str, Any]:
    sm    = get_scoring_manager()
    score = await sm.get_tool_score(tool_name)
    if score is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' 暂无评分数据")
    stats = await sm.get_tool_stats(tool_name)
    return {
        "score": score.to_dict(),
        "raw_stats": stats.to_dict() if stats else None,
    }


@router.delete("/tools/{tool_name}", summary="重置 Tool 统计数据")
async def reset_tool_stats(tool_name: str) -> Dict[str, Any]:
    sm = get_scoring_manager()
    ok = await sm.reset_tool(tool_name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' 暂无统计数据")
    return {"success": True, "message": f"Tool '{tool_name}' 统计数据已重置"}


# ── 权重管理 ──────────────────────────────────────────────────────────────────────

@router.get("/weights", summary="查询当前评分权重")
async def get_weights() -> Dict[str, Any]:
    sm = get_scoring_manager()
    w  = await sm.get_weights()
    return w.to_dict()


@router.put("/weights", summary="更新评分权重")
async def update_weights(req: WeightsUpdateRequest) -> Dict[str, Any]:
    sm      = get_scoring_manager()
    current = await sm.get_weights()
    updated = ScoreWeights(
        agent_success       = req.agent_success       if req.agent_success       is not None else current.agent_success,
        agent_latency       = req.agent_latency       if req.agent_latency       is not None else current.agent_latency,
        agent_quality       = req.agent_quality       if req.agent_quality       is not None else current.agent_quality,
        agent_popularity    = req.agent_popularity    if req.agent_popularity    is not None else current.agent_popularity,
        tool_success        = req.tool_success        if req.tool_success        is not None else current.tool_success,
        tool_latency        = req.tool_latency        if req.tool_latency        is not None else current.tool_latency,
        tool_popularity     = req.tool_popularity     if req.tool_popularity     is not None else current.tool_popularity,
        tool_danger_penalty = req.tool_danger_penalty if req.tool_danger_penalty is not None else current.tool_danger_penalty,
    )
    await sm.update_weights(updated)
    return {"success": True, "weights": updated.to_dict()}
