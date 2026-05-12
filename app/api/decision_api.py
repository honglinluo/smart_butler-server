"""
【模块说明】用户决策 API — 处理 AI 构建新工具时需要用户确认的授权机制

当 AI 需要临时创建一个新工具（如写代码去调用某个 API），系统会暂停等待用户确认。
这些接口让前端能够展示"待审批请求"弹窗，并接收用户的确认或拒绝操作。

【可用接口】
  GET  /decisions/pending              — 查询当前所有等待确认的决策请求（展示弹窗用）
  POST /decisions/{id}/resolve         — 确认或拒绝某个决策（用户点击"允许"或"拒绝"）
  GET  /decisions/logs/{session_id}    — 查看某次 Agent 执行的完整调用日志

  GET  /users/{user_id}/decision-policy  — 查看用户的工具构建授权策略
  PUT  /users/{user_id}/decision-policy  — 修改策略（allow/ask/deny 三选一）

用户决策 API — 管理工具构建的用户授权与策略配置

端点列表：
  GET  /decisions/pending              — 查询所有挂起等待的决策
  POST /decisions/{decision_id}/resolve — 确认或拒绝一个挂起决策
  GET  /decisions/logs/{session_id}    — 查询某次事件循环的调用日志
  GET  /users/{user_id}/decision-policy — 获取用户工具构建策略
  PUT  /users/{user_id}/decision-policy — 设置用户工具构建策略
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.agents.loop.decision_gate import DecisionState, UserDecisionGate
from app.utils.headers import ResponseHeaders

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/decisions", tags=["decisions"])


# ── Pydantic 请求体 ────────────────────────────────────────────────────────────

class ResolveBody(BaseModel):
    state: str  # "allow" | "deny"


class PolicyBody(BaseModel):
    policy: str  # "allow" | "ask" | "deny"


# ── 辅助：从 app.state 获取 Redis ──────────────────────────────────────────────

def _redis(request: Request):
    engine = getattr(request.app.state, "hermes_engine", None)
    if engine is None:
        return None
    mm = getattr(engine, "memory_manager", None)
    if mm is None:
        return None
    return getattr(mm, "_redis", None)


# ── 决策管理端点 ───────────────────────────────────────────────────────────────

@router.get("/pending", summary="查询所有挂起的工具构建决策")
async def list_pending(response: Response) -> Dict[str, str]:
    """返回当前所有等待用户确认的工具构建请求，格式 {decision_id: "pending"}。"""
    ResponseHeaders().apply(response)
    return UserDecisionGate.list_pending()


@router.post("/{decision_id}/resolve", summary="确认或拒绝一个挂起决策")
async def resolve_decision(decision_id: str, body: ResolveBody, response: Response) -> Dict[str, Any]:
    """唤醒挂起的事件循环协程并设置决策结果。

    - state="allow"：批准工具构建，循环继续执行
    - state="deny" ：拒绝构建，循环终止并返回提示给用户
    """
    ResponseHeaders().apply(response)
    if body.state not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="state 必须为 allow 或 deny")

    state = DecisionState.ALLOW if body.state == "allow" else DecisionState.DENIED
    ok    = UserDecisionGate.resolve(decision_id, state)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"decision_id={decision_id!r} 不存在或已超时",
        )
    return {"decision_id": decision_id, "resolved": body.state, "ok": True}


@router.get("/logs/{session_id}", summary="查询某次事件循环的调用日志")
async def get_loop_logs(
    session_id: str,
    user_id: str,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    """从 Redis 拉取事件循环的完整调用日志（JSON 数组）。

    日志格式每条：{event_type, agent_name, message, timestamp, iteration, data}
    """
    ResponseHeaders().apply(response)
    redis_db = _redis(request)
    if redis_db is None:
        raise HTTPException(status_code=503, detail="Redis 不可用")
    try:
        key   = f"user:{user_id}:loop_logs:{session_id}"
        items = await redis_db.lrange(key, 0, -1)
        logs: List[Dict] = []
        for raw in (items or []):
            try:
                logs.append(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
            except Exception:
                logs.append({"raw": str(raw)})
        return {"session_id": session_id, "count": len(logs), "logs": logs}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── 用户策略端点 ───────────────────────────────────────────────────────────────

@router.get("/users/{user_id}/decision-policy", summary="获取用户工具构建授权策略")
async def get_policy(user_id: str, request: Request, response: Response) -> Dict[str, str]:
    """读取当前用户的工具构建授权策略（allow / ask / deny）。"""
    ResponseHeaders().apply(response)
    redis_db = _redis(request)
    gate     = UserDecisionGate(redis_db=redis_db)
    policy   = await gate._get_policy(user_id)
    return {"user_id": user_id, "policy": policy}


@router.put("/users/{user_id}/decision-policy", summary="设置用户工具构建授权策略")
async def set_policy(
    user_id: str, body: PolicyBody, request: Request, response: Response,
) -> Dict[str, str]:
    """配置工具构建的默认授权策略。

    - allow：所有工具构建请求自动放行（无需确认）
    - ask  ：每次构建前通知并等待用户确认（默认值）
    - deny ：拒绝所有工具构建请求
    """
    ResponseHeaders().apply(response)
    if body.policy not in ("allow", "ask", "deny"):
        raise HTTPException(status_code=400, detail="policy 必须为 allow / ask / deny")

    redis_db = _redis(request)
    if redis_db is None:
        raise HTTPException(status_code=503, detail="Redis 不可用")
    try:
        await redis_db.set(f"user:{user_id}:decision_policy", body.policy)
        logger.info("[DecisionAPI] user=%s policy 设置为 %s", user_id, body.policy)
        return {"user_id": user_id, "policy": body.policy}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
