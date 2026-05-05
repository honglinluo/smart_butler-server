"""Agent 事件循环 — 用户决策门控

决策策略（Redis key: user:{user_id}:decision_policy）：
  allow — 所有工具构建自动放行（无需确认）
  ask   — 每次构建前挂起等待用户通过 API 确认（默认值）
  deny  — 拒绝所有工具构建请求

挂起等待机制：
  - 使用 asyncio.Event + 模块级字典实现进程内协程挂起
  - 超时（默认 5 min）自动转为 DENIED
  - 用户通过 POST /decisions/{id}/resolve 唤醒等待中的协程
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger("agent_loop.decision")

_WAIT_TIMEOUT = 300.0  # 5 min

# 进程级挂起决策表（单 worker 进程内有效；多进程需用 Redis pub/sub）
_pending_events:  Dict[str, asyncio.Event]  = {}
_pending_results: Dict[str, "DecisionState"] = {}


class DecisionState(str, Enum):
    ALLOW   = "allow"    # 放行
    PENDING = "pending"  # 等待用户决策
    DENIED  = "denied"   # 拒绝


class UserDecisionGate:
    """用户决策门控。实例化时注入 redis_db 以读取策略配置。"""

    def __init__(self, redis_db=None) -> None:
        self._redis = redis_db

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def check_and_wait(
        self,
        user_id:     str,
        decision_id: str,
        action_desc: str,
    ) -> DecisionState:
        """按策略检查并在必要时挂起协程等待用户确认。

        Args:
            user_id:     当前用户 ID
            decision_id: 唯一决策标识（用于 resolve API）
            action_desc: 待授权操作的文字描述（展示给用户）

        Returns:
            ALLOW  — 可以继续执行
            DENIED — 操作被拒绝，应终止
        """
        policy = await self._get_policy(user_id)

        if policy == "allow":
            logger.debug(
                "[DecisionGate] user=%s policy=allow 自动放行 decision_id=%s",
                user_id, decision_id,
            )
            return DecisionState.ALLOW

        if policy == "deny":
            logger.info(
                "[DecisionGate] user=%s policy=deny 自动拒绝 decision_id=%s",
                user_id, decision_id,
            )
            return DecisionState.DENIED

        # policy == "ask"：挂起等待用户决策
        event = asyncio.Event()
        _pending_events[decision_id]  = event
        _pending_results[decision_id] = DecisionState.PENDING

        logger.info(
            "[DecisionGate] user=%s 挂起等待决策 decision_id=%s 操作: %s",
            user_id, decision_id, action_desc,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[DecisionGate] decision_id=%s 等待超时，自动拒绝", decision_id)
            _pending_results[decision_id] = DecisionState.DENIED
        finally:
            _pending_events.pop(decision_id, None)

        result = _pending_results.pop(decision_id, DecisionState.DENIED)
        logger.info("[DecisionGate] decision_id=%s 最终结果: %s", decision_id, result.value)
        return result

    @staticmethod
    def resolve(decision_id: str, state: DecisionState) -> bool:
        """由决策 API 调用，唤醒挂起的协程并注入结果。

        Returns:
            True  — 找到并唤醒了对应的等待协程
            False — decision_id 不存在（已超时或不合法）
        """
        event = _pending_events.get(decision_id)
        if event is None:
            return False
        _pending_results[decision_id] = state
        event.set()
        logger.info(
            "[DecisionGate] resolve decision_id=%s → %s",
            decision_id, state.value,
        )
        return True

    @staticmethod
    def list_pending() -> Dict[str, str]:
        """返回所有当前挂起等待的决策 ID → "pending" 映射。"""
        return {k: "pending" for k in _pending_events}

    # ── 内部 ──────────────────────────────────────────────────────────────────

    async def _get_policy(self, user_id: str) -> str:
        """从 Redis 读取用户决策策略，默认 ask。"""
        if self._redis is None:
            return "allow"
        try:
            val = await self._redis.get(f"user:{user_id}:decision_policy")
            return val if val in ("allow", "ask", "deny") else "ask"
        except Exception:
            return "allow"
