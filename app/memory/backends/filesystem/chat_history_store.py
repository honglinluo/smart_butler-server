"""
【模块说明】文件系统聊天历史存储后端（FilesystemChatHistoryStore）

基于本地文件系统实现 ChatHistoryBackend 接口，无需 Elasticsearch。
适合轻量部署、开发调试或离线场景。

【存储结构】
  data/chat_history/{user_id}/
  ├── turns/
  │   └── {YYYY-MM}/
  │       └── {turn_id}.json     ← 每轮对话完整 JSON
  └── index.jsonl                ← 追加式时间线索引（turn_id + timestamp 等轻量字段）

【接口说明】
  save_turn()           — 写 JSON 文件 + 追加 index.jsonl
  get_recent_messages() — 从 index.jsonl 读取最近 N 轮，展开为 role/content 列表
  get_recent_turns()    — 从 index.jsonl 读取最近 N 轮，返回完整轮次对象
  list_indices()        — 列出 chat_history/ 下的用户目录名
  count_index_docs()    — 统计指定用户目录下的 JSON 文件数量
  summarize_recent()    — 拼接最近时间段内的聊天记录文本
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.paths import PROJECT_ROOT
from app.memory.base import ChatHistoryBackend

logger = logging.getLogger(__name__)

_HISTORY_ROOT = PROJECT_ROOT / "data" / "chat_history"

_SYSTEM_TYPES = {"compression_summary", "monthly_summary", "yearly_summary"}


class FilesystemChatHistoryStore(ChatHistoryBackend):
    """基于文件系统的聊天历史存储，实现 ChatHistoryBackend 接口。

    线程安全：文件 I/O 通过 asyncio.to_thread 在线程池执行，避免阻塞事件循环。
    并发安全：同一 user_id 的写操作由 per-user asyncio.Lock 串行化。
    """

    def __init__(self) -> None:
        self._root: Path = _HISTORY_ROOT
        self._write_locks: Dict[str, asyncio.Lock] = {}

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _write_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._write_locks:
            self._write_locks[user_id] = asyncio.Lock()
        return self._write_locks[user_id]

    def _user_dir(self, user_id: str) -> Path:
        d = self._root / user_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _turn_path(self, user_id: str, turn_id: str, timestamp: str) -> Path:
        month = timestamp[:7] if len(timestamp) >= 7 else datetime.now().strftime("%Y-%m")
        d = self._user_dir(user_id) / "turns" / month
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{turn_id}.json"

    def _index_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "index.jsonl"

    # ── 同步 I/O（在线程池内执行）────────────────────────────────────────────

    @staticmethod
    def _sync_write_json(path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _sync_read_json(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _sync_append_jsonl(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _sync_read_jsonl(path: Path) -> List[dict]:
        if not path.exists():
            return []
        lines: List[dict] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return lines

    @staticmethod
    def _sync_find_turn(turns_dir: Path, turn_id: str) -> Optional[Path]:
        if not turns_dir.exists():
            return None
        for p in turns_dir.rglob(f"{turn_id}.json"):
            return p
        return None

    @staticmethod
    def _sync_count_jsons(turns_dir: Path) -> int:
        if not turns_dir.exists():
            return 0
        return sum(1 for _ in turns_dir.rglob("*.json"))

    # ── 异步 I/O 封装 ─────────────────────────────────────────────────────────

    async def _read_turn(
        self, user_id: str, turn_id: str, timestamp: str
    ) -> Optional[dict]:
        path = self._turn_path(user_id, turn_id, timestamp)
        if not await asyncio.to_thread(path.exists):
            turns_dir = self._user_dir(user_id) / "turns"
            path_found = await asyncio.to_thread(
                self._sync_find_turn, turns_dir, turn_id
            )
            if path_found is None:
                return None
            path = path_found
        return await asyncio.to_thread(self._sync_read_json, path)

    async def _read_index(self, user_id: str) -> List[dict]:
        return await asyncio.to_thread(
            self._sync_read_jsonl, self._index_path(user_id)
        )

    # ── ChatHistoryBackend 核心接口 ───────────────────────────────────────────

    async def save_turn(
        self,
        user_id: str,
        user_input: str,
        assistant_response: str,
        turn_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """持久化一轮对话到文件系统。

        写完整 JSON 文件 + 追加轻量 index.jsonl 索引。
        写操作通过 per-user Lock 串行化，避免 index.jsonl 并发追加乱序。
        """
        turn_id = turn_id or uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        meta = metadata or {}

        # 过滤系统摘要类型，不写入聊天历史
        if isinstance(meta.get("metadata"), dict):
            if meta["metadata"].get("type") in _SYSTEM_TYPES:
                return turn_id
        if user_input == "[系统摘要]":
            return turn_id

        turn_data: Dict[str, Any] = {
            "turn_id":            turn_id,
            "user_id":            user_id,
            "user_input":         user_input,
            "assistant_response": assistant_response,
            "timestamp":          timestamp,
            "client_type":        meta.get("client_type", "") or "api",
            "intent":             meta.get("intent", ""),
            "mode":               meta.get("mode", ""),
            "pipeline":           meta.get("pipeline", []),
            "agent_outputs":      meta.get("agent_outputs", []),
        }

        index_entry = {
            "turn_id":    turn_id,
            "timestamp":  timestamp,
            "client_type": turn_data["client_type"],
            "preview":    user_input[:80],
        }

        async with self._write_lock(user_id):
            path = self._turn_path(user_id, turn_id, timestamp)
            await asyncio.to_thread(self._sync_write_json, path, turn_data)
            await asyncio.to_thread(
                self._sync_append_jsonl, self._index_path(user_id), index_entry
            )

        logger.debug(
            "FilesystemChatHistory.save_turn user=%s turn=%s", user_id, turn_id
        )
        return turn_id

    async def get_recent_messages(
        self,
        user_id: str,
        size: int = 5,
        from_: int = 0,
    ) -> List[Dict[str, Any]]:
        """返回展开后的 role/content 消息列表（降序，分页）。

        index.jsonl 末尾为最新，取末尾 size 条后展开为 user/assistant 消息对。
        """
        index = await self._read_index(user_id)
        if not index:
            return []

        # 降序分页：从末尾截取
        total = len(index)
        start = max(0, total - from_ - size)
        end = max(0, total - from_)
        page = list(reversed(index[start:end]))  # 最新在前

        # 并发加载完整 turn 数据
        tasks = [
            self._read_turn(user_id, e["turn_id"], e.get("timestamp", ""))
            for e in page
        ]
        loaded = await asyncio.gather(*tasks)

        messages: List[Dict[str, Any]] = []
        for turn in loaded:
            if turn is None:
                continue
            ts = turn.get("timestamp")
            tid = turn.get("turn_id")
            messages.append({
                "role":      "user",
                "content":   turn.get("user_input", ""),
                "timestamp": ts,
                "turn_id":   tid,
            })
            messages.append({
                "role":      "assistant",
                "content":   turn.get("assistant_response", ""),
                "timestamp": ts,
                "turn_id":   tid,
            })

        return messages

    async def get_recent_turns(
        self,
        user_id: str,
        size: int = 20,
        from_: int = 0,
        client_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """返回对话轮次列表，每条含 user_input + assistant_response（降序，分页）。

        client_type 非空时只返回该客户端的对话。
        """
        index = await self._read_index(user_id)
        if not index:
            return []

        if client_type:
            index = [e for e in index if e.get("client_type") == client_type]

        total = len(index)
        start = max(0, total - from_ - size)
        end = max(0, total - from_)
        page = list(reversed(index[start:end]))

        tasks = [
            self._read_turn(user_id, e["turn_id"], e.get("timestamp", ""))
            for e in page
        ]
        loaded = await asyncio.gather(*tasks)

        turns: List[Dict[str, Any]] = []
        for turn in loaded:
            if turn is None:
                continue
            turns.append({
                "turn_id":            turn.get("turn_id", ""),
                "user_input":         turn.get("user_input", ""),
                "assistant_response": turn.get("assistant_response", ""),
                "timestamp":          turn.get("timestamp"),
                "intent":             turn.get("intent", ""),
                "mode":               turn.get("mode", ""),
                "pipeline":           turn.get("pipeline", []),
                "agent_outputs":      turn.get("agent_outputs", []),
                "client_type":        turn.get("client_type", ""),
            })

        return turns

    # ── 扩展功能 ──────────────────────────────────────────────────────────────

    async def list_indices(self, prefix: Optional[str] = None) -> List[str]:
        """列出 chat_history/ 下的用户目录名（可按前缀过滤）。"""
        try:
            if not self._root.exists():
                return []
            dirs = await asyncio.to_thread(
                lambda: [d.name for d in self._root.iterdir() if d.is_dir()]
            )
            if prefix:
                dirs = [d for d in dirs if d.startswith(prefix)]
            return sorted(dirs)
        except Exception as e:
            logger.warning(f"列出聊天历史目录失败: {e}")
            return []

    async def count_index_docs(
        self, user_id: str, client_type: Optional[str] = None
    ) -> int:
        """统计指定用户的对话轮次数量。

        client_type 非空时从 index.jsonl 统计过滤后的数量；
        否则直接统计 turns/ 目录下 JSON 文件数（更快）。
        """
        try:
            if client_type:
                index = await self._read_index(user_id)
                return sum(1 for e in index if e.get("client_type") == client_type)
            turns_dir = self._user_dir(user_id) / "turns"
            return await asyncio.to_thread(self._sync_count_jsons, turns_dir)
        except Exception as e:
            logger.warning(f"统计聊天历史文档数失败: {e}")
            return 0

    async def summarize_recent(
        self, user_id: str, hours: int = 24, max_messages: int = 200
    ) -> str:
        """读取最近时间范围内的聊天记录并返回拼接文本。"""
        try:
            msgs = await self.get_recent_messages(user_id, size=max_messages)
            if not msgs:
                return ""

            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            filtered = []
            for m in msgs:
                ts = m.get("timestamp")
                t = None
                if ts:
                    try:
                        t = datetime.fromisoformat(ts)
                    except Exception:
                        pass
                if t is None or t >= cutoff:
                    filtered.append(m)

            parts = [f"[{m.get('role')}] {m.get('content')}" for m in reversed(filtered)]
            return "\n".join(parts)
        except Exception as e:
            logger.warning(f"摘要最近聊天记录失败: {e}")
            return ""
