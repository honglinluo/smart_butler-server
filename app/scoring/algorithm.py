"""
评分算法 — 纯函数，无副作用，便于单独测试和调参。

Agent 综合评分公式：
  base = success_rate * w_success
       + latency_score * w_latency       # latency_score = max(0, 1 - ms/AGENT_LATENCY_BASE)
       + quality_score * w_quality
       + popularity_score * w_popularity # log-normalized: log(1+n)/LOG_CEILING
  penalty = min(consecutive_failures * 0.05, 0.25)
  final = clamp(base - penalty, 0.0, 1.0)

Tool 综合评分公式：
  score = success_rate * w_success
        + latency_score * w_latency      # latency_score = max(0, 1 - ms/TOOL_LATENCY_BASE)
        + popularity_score * w_popularity
        - consent_rate * w_danger_penalty
  final = clamp(score, 0.0, 1.0)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.scoring.models import AgentStats, ToolStats, ScoreWeights, AgentScore, ToolScore

# ── 调参常数 ───────────────────────────────────────────────────────────────────

AGENT_LATENCY_BASE_MS = 8_000.0    # ≥8s 延迟得分为 0
TOOL_LATENCY_BASE_MS  = 3_000.0    # ≥3s 延迟得分为 0
LOG_CEILING           = math.log(1 + 10_000)  # 约等于 9.21；用于归一化 popularity


# ── 子维度计算 ─────────────────────────────────────────────────────────────────

def _success_rate(call_count: int, success_count: int) -> float:
    return success_count / call_count if call_count > 0 else 0.0


def _latency_score(avg_ms: float, baseline_ms: float) -> float:
    """越快越高，≥baseline_ms 则为 0。"""
    return max(0.0, 1.0 - avg_ms / baseline_ms)


def _popularity_score(call_count: int) -> float:
    """对数归一化：call_count=10000 时约为 1.0。"""
    return math.log(1 + call_count) / LOG_CEILING


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# ── Agent 评分 ─────────────────────────────────────────────────────────────────

def compute_agent_score(stats: "AgentStats", weights: "ScoreWeights") -> "AgentScore":
    from app.scoring.models import AgentScore

    if stats.call_count == 0:
        return AgentScore(
            agent_name=stats.agent_name,
            composite_score=0.0,
            success_rate=0.0,
            avg_latency_ms=0.0,
            quality_score=0.5,
            call_count=0,
            consecutive_fail=0,
        )

    sr        = _success_rate(stats.call_count, stats.success_count)
    avg_ms    = stats.total_latency_ms / stats.call_count
    lat_score = _latency_score(avg_ms, AGENT_LATENCY_BASE_MS)
    quality   = (
        stats.quality_score_sum / stats.quality_score_count
        if stats.quality_score_count > 0
        else 0.5
    )
    pop_score = _popularity_score(stats.call_count)

    base = (
        sr        * weights.agent_success
        + lat_score * weights.agent_latency
        + quality   * weights.agent_quality
        + pop_score * weights.agent_popularity
    )
    penalty    = min(stats.consecutive_failures * 0.05, 0.25)
    composite  = round(_clamp(base - penalty), 4)

    return AgentScore(
        agent_name=stats.agent_name,
        composite_score=composite,
        success_rate=round(sr, 4),
        avg_latency_ms=round(avg_ms, 2),
        quality_score=round(quality, 4),
        call_count=stats.call_count,
        consecutive_fail=stats.consecutive_failures,
    )


# ── Tool 评分 ──────────────────────────────────────────────────────────────────

def compute_tool_score(stats: "ToolStats", weights: "ScoreWeights") -> "ToolScore":
    from app.scoring.models import ToolScore

    if stats.call_count == 0:
        return ToolScore(
            tool_name=stats.tool_name,
            composite_score=0.0,
            success_rate=0.0,
            avg_latency_ms=0.0,
            consent_rate=0.0,
            call_count=0,
        )

    sr           = _success_rate(stats.call_count, stats.success_count)
    avg_ms       = stats.total_latency_ms / stats.call_count
    lat_score    = _latency_score(avg_ms, TOOL_LATENCY_BASE_MS)
    consent_rate = stats.consent_required_count / stats.call_count
    pop_score    = _popularity_score(stats.call_count)

    score = (
        sr           * weights.tool_success
        + lat_score   * weights.tool_latency
        + pop_score   * weights.tool_popularity
        - consent_rate * weights.tool_danger_penalty
    )
    composite = round(_clamp(score), 4)

    return ToolScore(
        tool_name=stats.tool_name,
        composite_score=composite,
        success_rate=round(sr, 4),
        avg_latency_ms=round(avg_ms, 2),
        consent_rate=round(consent_rate, 4),
        call_count=stats.call_count,
    )
