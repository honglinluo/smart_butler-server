"""
评分数据模型。

AgentStats / ToolStats — 累计原始统计数据（持久化到文件）
AgentScore / ToolScore  — 由统计数据计算出的综合评分（仅内存）
ScoreWeights            — 各维度权重（可通过 JSON 文件覆盖）
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


# ── 原始统计（持久化层）────────────────────────────────────────────────────────

@dataclass
class AgentStats:
    """某个 Agent 的累计执行统计。"""
    agent_name:           str
    call_count:           int   = 0       # 总调用次数
    success_count:        int   = 0       # 成功次数
    total_latency_ms:     float = 0.0     # 总耗时（毫秒）
    quality_score_sum:    float = 0.0     # LLM 判断的质量分数累加（0-1）
    quality_score_count:  int   = 0       # 有质量评分的次数
    consecutive_failures: int   = 0       # 当前连续失败次数
    last_updated:         str   = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentStats":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ToolStats:
    """某个 Tool 的累计执行统计。"""
    tool_name:              str
    call_count:             int   = 0
    success_count:          int   = 0
    total_latency_ms:       float = 0.0
    consent_required_count: int   = 0     # 触发了权限请求的次数
    last_updated:           str   = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ToolStats":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── 权重配置 ────────────────────────────────────────────────────────────────────

@dataclass
class ScoreWeights:
    """
    各维度的评分权重（所有权重之和应为 1.0，建议值如下）。

    Agent 权重：
      - success:    成功率（最重要）
      - latency:    响应速度（越快越好）
      - quality:    LLM 判断的输出质量
      - popularity: 使用频率（对数归一化）

    Tool 权重：
      - success:        成功率
      - latency:        响应速度
      - popularity:     使用频率
      - danger_penalty: 危险操作触发率（惩罚项，权重越高惩罚越重）
    """
    # Agent
    agent_success:    float = 0.40
    agent_latency:    float = 0.20
    agent_quality:    float = 0.25
    agent_popularity: float = 0.15

    # Tool
    tool_success:        float = 0.50
    tool_latency:        float = 0.25
    tool_popularity:     float = 0.15
    tool_danger_penalty: float = 0.10

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreWeights":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── 评分快照（仅用于 API 输出）──────────────────────────────────────────────────

@dataclass
class AgentScore:
    """Agent 综合评分快照（由 ScoringManager.get_agent_score() 返回）。"""
    agent_name:       str
    composite_score:  float   # 0.0 – 1.0，综合评分
    success_rate:     float   # 成功率
    avg_latency_ms:   float   # 平均耗时（毫秒）
    quality_score:    float   # 平均质量分（0-1；无数据时为 0.5）
    call_count:       int
    consecutive_fail: int     # 当前连续失败次数

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolScore:
    """Tool 综合评分快照（由 ScoringManager.get_tool_score() 返回）。"""
    tool_name:       str
    composite_score: float
    success_rate:    float
    avg_latency_ms:  float
    consent_rate:    float    # 危险操作触发率
    call_count:      int

    def to_dict(self) -> dict:
        return asdict(self)
