"""
评分数据持久化 — OpenViking 风格文件存储。

目录布局：
  data/scoring/
  ├── agents/{agent_name}.json   # AgentStats
  ├── tools/{tool_name}.json     # ToolStats
  └── weights.json               # ScoreWeights（可选；不存在时使用默认权重）
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from app.scoring.models import AgentStats, ToolStats, ScoreWeights

logger = logging.getLogger(__name__)


class ScoringStore:
    """
    基于文件系统的评分数据存储。

    每个 agent/tool 独占一个 JSON 文件，写操作使用 per-key asyncio.Lock 序列化，
    读操作直接访问文件（无锁，接受极低概率的脏读）。
    """

    def __init__(self, base_dir: Path) -> None:
        self._agents_dir = base_dir / "agents"
        self._tools_dir  = base_dir / "tools"
        self._weights_path = base_dir / "weights.json"
        self._locks: Dict[str, asyncio.Lock] = {}

    # ── 辅助 ────────────────────────────────────────────────────────────────────

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("[ScoringStore] 读取失败 %s: %s", path, e)
            return None

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 原子替换，防止写一半的文件被读到

    # ── Agent ───────────────────────────────────────────────────────────────────

    async def load_agent(self, agent_name: str) -> Optional[AgentStats]:
        path = self._agents_dir / f"{agent_name}.json"
        data = await asyncio.to_thread(self._read_json, path)
        if data is None:
            return None
        try:
            return AgentStats.from_dict(data)
        except Exception as e:
            logger.warning("[ScoringStore] agent 数据损坏 %s: %s", agent_name, e)
            return None

    async def save_agent(self, stats: AgentStats) -> None:
        path = self._agents_dir / f"{stats.agent_name}.json"
        async with self._lock(f"agent:{stats.agent_name}"):
            await asyncio.to_thread(self._write_json, path, stats.to_dict())

    async def delete_agent(self, agent_name: str) -> bool:
        path = self._agents_dir / f"{agent_name}.json"
        async with self._lock(f"agent:{agent_name}"):
            try:
                await asyncio.to_thread(path.unlink, True)
                return True
            except Exception:
                return False

    async def list_agents(self) -> List[str]:
        def _ls() -> List[str]:
            if not self._agents_dir.exists():
                return []
            return [p.stem for p in self._agents_dir.glob("*.json")]
        return await asyncio.to_thread(_ls)

    # ── Tool ────────────────────────────────────────────────────────────────────

    async def load_tool(self, tool_name: str) -> Optional[ToolStats]:
        path = self._tools_dir / f"{tool_name}.json"
        data = await asyncio.to_thread(self._read_json, path)
        if data is None:
            return None
        try:
            return ToolStats.from_dict(data)
        except Exception as e:
            logger.warning("[ScoringStore] tool 数据损坏 %s: %s", tool_name, e)
            return None

    async def save_tool(self, stats: ToolStats) -> None:
        path = self._tools_dir / f"{stats.tool_name}.json"
        async with self._lock(f"tool:{stats.tool_name}"):
            await asyncio.to_thread(self._write_json, path, stats.to_dict())

    async def delete_tool(self, tool_name: str) -> bool:
        path = self._tools_dir / f"{tool_name}.json"
        async with self._lock(f"tool:{tool_name}"):
            try:
                await asyncio.to_thread(path.unlink, True)
                return True
            except Exception:
                return False

    async def list_tools(self) -> List[str]:
        def _ls() -> List[str]:
            if not self._tools_dir.exists():
                return []
            return [p.stem for p in self._tools_dir.glob("*.json")]
        return await asyncio.to_thread(_ls)

    # ── 权重 ────────────────────────────────────────────────────────────────────

    async def load_weights(self) -> ScoreWeights:
        data = await asyncio.to_thread(self._read_json, self._weights_path)
        if data is None:
            return ScoreWeights()
        try:
            return ScoreWeights.from_dict(data)
        except Exception:
            return ScoreWeights()

    async def save_weights(self, weights: ScoreWeights) -> None:
        async with self._lock("weights"):
            await asyncio.to_thread(self._write_json, self._weights_path, weights.to_dict())
