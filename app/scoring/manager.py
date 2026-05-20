"""
ScoringManager — Agent 和 Tool 评分的统一管理入口（进程级单例）。

对外接口：
  record_agent_call(agent_name, success, latency_ms, quality_score)
  record_tool_call(tool_name, success, latency_ms, consent_required)
  get_agent_score(agent_name)   → AgentScore | None
  get_tool_score(tool_name)     → ToolScore  | None
  get_top_agents(n)             → List[AgentScore]
  get_top_tools(n)              → List[ToolScore]
  get_weights()                 → ScoreWeights
  update_weights(weights)       → None
  reset_agent(agent_name)       → bool
  reset_tool(tool_name)         → bool

设计原则：
  - 所有写操作先更新内存缓存再异步持久化（fire-and-forget），不阻塞主流程
  - 使用 per-key asyncio.Lock 防止同名 agent/tool 的并发写冲突
  - 启动时懒加载权重，后续从内存缓存读取
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.scoring.models import AgentStats, ToolStats, ScoreWeights, AgentScore, ToolScore
from app.scoring.algorithm import compute_agent_score, compute_tool_score
from app.scoring.store import ScoringStore

logger = logging.getLogger(__name__)


class ScoringManager:
    """进程级评分管理器单例。通过 get_scoring_manager() 获取实例。"""

    def __init__(self, store_dir: Path) -> None:
        self._store   = ScoringStore(store_dir)
        self._weights: Optional[ScoreWeights] = None

        # 内存写缓冲：最近更新的统计数据（减少重复读磁盘）
        self._agent_cache: Dict[str, AgentStats] = {}
        self._tool_cache:  Dict[str, ToolStats]  = {}

        # 写锁（与 ScoringStore 内部锁配合，确保 cache ↔ 磁盘一致）
        self._agent_locks: Dict[str, asyncio.Lock] = {}
        self._tool_locks:  Dict[str, asyncio.Lock] = {}

    # ── 辅助 ────────────────────────────────────────────────────────────────────

    def _agent_lock(self, name: str) -> asyncio.Lock:
        if name not in self._agent_locks:
            self._agent_locks[name] = asyncio.Lock()
        return self._agent_locks[name]

    def _tool_lock(self, name: str) -> asyncio.Lock:
        if name not in self._tool_locks:
            self._tool_locks[name] = asyncio.Lock()
        return self._tool_locks[name]

    async def _ensure_weights(self) -> ScoreWeights:
        if self._weights is None:
            self._weights = await self._store.load_weights()
        return self._weights

    async def _load_agent(self, agent_name: str) -> AgentStats:
        if agent_name in self._agent_cache:
            return self._agent_cache[agent_name]
        stats = await self._store.load_agent(agent_name)
        if stats is None:
            stats = AgentStats(agent_name=agent_name)
        self._agent_cache[agent_name] = stats
        return stats

    async def _load_tool(self, tool_name: str) -> ToolStats:
        if tool_name in self._tool_cache:
            return self._tool_cache[tool_name]
        stats = await self._store.load_tool(tool_name)
        if stats is None:
            stats = ToolStats(tool_name=tool_name)
        self._tool_cache[tool_name] = stats
        return stats

    # ── 事件记录 ────────────────────────────────────────────────────────────────

    async def record_agent_call(
        self,
        agent_name:    str,
        success:       bool,
        latency_ms:    float = 0.0,
        quality_score: Optional[float] = None,
    ) -> None:
        """记录一次 Agent 执行事件，更新统计数据并异步持久化。"""
        async with self._agent_lock(agent_name):
            stats = await self._load_agent(agent_name)
            stats.call_count       += 1
            stats.total_latency_ms += latency_ms
            if success:
                stats.success_count       += 1
                stats.consecutive_failures = 0
            else:
                stats.consecutive_failures += 1
            if quality_score is not None:
                stats.quality_score_sum   += quality_score
                stats.quality_score_count += 1
            stats.last_updated = datetime.now().isoformat()
            self._agent_cache[agent_name] = stats

        asyncio.create_task(self._store.save_agent(stats))
        logger.debug(
            "[Scoring] agent=%s success=%s latency=%.0fms calls=%d",
            agent_name, success, latency_ms, stats.call_count,
        )

    async def record_tool_call(
        self,
        tool_name:       str,
        success:         bool,
        latency_ms:      float = 0.0,
        consent_required: bool = False,
    ) -> None:
        """记录一次 Tool 执行事件，更新统计数据并异步持久化。"""
        async with self._tool_lock(tool_name):
            stats = await self._load_tool(tool_name)
            stats.call_count       += 1
            stats.total_latency_ms += latency_ms
            if success:
                stats.success_count += 1
            if consent_required:
                stats.consent_required_count += 1
            stats.last_updated = datetime.now().isoformat()
            self._tool_cache[tool_name] = stats

        asyncio.create_task(self._store.save_tool(stats))
        logger.debug(
            "[Scoring] tool=%s success=%s latency=%.0fms calls=%d",
            tool_name, success, latency_ms, stats.call_count,
        )

    # ── 评分查询 ────────────────────────────────────────────────────────────────

    async def get_agent_score(self, agent_name: str) -> Optional[AgentScore]:
        stats = await self._store.load_agent(agent_name)
        if stats is None:
            return None
        weights = await self._ensure_weights()
        return compute_agent_score(stats, weights)

    async def get_tool_score(self, tool_name: str) -> Optional[ToolScore]:
        stats = await self._store.load_tool(tool_name)
        if stats is None:
            return None
        weights = await self._ensure_weights()
        return compute_tool_score(stats, weights)

    async def get_top_agents(self, n: int = 10) -> List[AgentScore]:
        names   = await self._store.list_agents()
        weights = await self._ensure_weights()
        scores: List[AgentScore] = []
        for name in names:
            stats = await self._store.load_agent(name)
            if stats and stats.call_count > 0:
                scores.append(compute_agent_score(stats, weights))
        scores.sort(key=lambda s: s.composite_score, reverse=True)
        return scores[:n]

    async def get_top_tools(self, n: int = 10) -> List[ToolScore]:
        names   = await self._store.list_tools()
        weights = await self._ensure_weights()
        scores: List[ToolScore] = []
        for name in names:
            stats = await self._store.load_tool(name)
            if stats and stats.call_count > 0:
                scores.append(compute_tool_score(stats, weights))
        scores.sort(key=lambda s: s.composite_score, reverse=True)
        return scores[:n]

    # ── 权重管理 ────────────────────────────────────────────────────────────────

    async def get_weights(self) -> ScoreWeights:
        return await self._ensure_weights()

    async def update_weights(self, weights: ScoreWeights) -> None:
        self._weights = weights
        await self._store.save_weights(weights)
        logger.info("[Scoring] 权重已更新: %s", weights.to_dict())

    # ── 统计重置 ────────────────────────────────────────────────────────────────

    async def reset_agent(self, agent_name: str) -> bool:
        async with self._agent_lock(agent_name):
            self._agent_cache.pop(agent_name, None)
        ok = await self._store.delete_agent(agent_name)
        if ok:
            logger.info("[Scoring] agent 统计已重置: %s", agent_name)
        return ok

    async def reset_tool(self, tool_name: str) -> bool:
        async with self._tool_lock(tool_name):
            self._tool_cache.pop(tool_name, None)
        ok = await self._store.delete_tool(tool_name)
        if ok:
            logger.info("[Scoring] tool 统计已重置: %s", tool_name)
        return ok

    async def get_agent_stats(self, agent_name: str) -> Optional[AgentStats]:
        """返回原始统计数据（调试用）。"""
        return await self._store.load_agent(agent_name)

    async def get_tool_stats(self, tool_name: str) -> Optional[ToolStats]:
        """返回原始统计数据（调试用）。"""
        return await self._store.load_tool(tool_name)


# ── 全局单例 ────────────────────────────────────────────────────────────────────

_manager: Optional[ScoringManager] = None


def get_scoring_manager(store_dir: Optional[Path] = None) -> ScoringManager:
    """获取全局 ScoringManager 单例（首次调用时初始化）。"""
    global _manager
    if _manager is None:
        if store_dir is None:
            from app.utils.paths import PROJECT_ROOT
            store_dir = PROJECT_ROOT / "data" / "scoring"
        _manager = ScoringManager(store_dir)
    return _manager
