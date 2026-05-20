"""
【模块说明】文件系统记忆后端（OpenViking 风格）

以本地文件系统为持久化层，实现 MemoryBackend 接口。
无需 Redis / MySQL / Elasticsearch，适合轻量部署或开发调试。

目录结构（参照 OpenViking 虚拟文件系统 URI 树设计）：
  data/memory/{user_id}/
  ├── turns/
  │   └── {YYYY-MM}/
  │       └── {turn_id}.json      ← 每轮对话独立 JSON 文件（L2 详情）
  ├── manifest.jsonl               ← 追加式时间线索引（轻量，快速读取最近 N 条）
  ├── profile.json                 ← 用户画像摘要（L0 抽象层）
  └── delegations.jsonl            ← Agent 委派链日志

检索策略：
  - 最近对话：从 manifest.jsonl 读取末尾 N 条，加载对应 JSON 文件
  - 语义检索：对最近 200 条做关键词评分（search.py），无向量依赖
  - 预取：后台 asyncio Task 异步执行检索并缓存到内存，下轮 0 延迟命中
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.paths import PROJECT_ROOT
from app.memory.base import MemoryBackend

logger = logging.getLogger(__name__)

# 存储根目录
_MEMORY_ROOT = PROJECT_ROOT / "data" / "memory"

# manifest 中保留的时间线条目数上限（防止无限增长）
_MANIFEST_MAX_LINES = 10_000


class FilesystemMemoryBackend(MemoryBackend):
    """OpenViking 风格文件系统记忆后端。

    线程安全：文件读写通过 asyncio.to_thread 在线程池执行，避免阻塞事件循环。
    并发安全：同一 user_id 的写操作由 per-user asyncio.Lock 串行化。
    """

    context_length_limit: int = 20_000

    def __init__(self, config: Dict[str, Any]) -> None:
        self._root: Path = _MEMORY_ROOT
        self._recent_n: int = int(config.get("recent_turns", 20))
        # 内存预取缓存：{user_id: [turn_dict, ...]}
        self._prefetch_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._prefetch_lock = asyncio.Lock()
        # per-user 写锁，避免并发写 manifest 导致数据交叉
        self._write_locks: Dict[str, asyncio.Lock] = {}

    # ── 内部工具 ────────────────────────────────────────────────────────────

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

    def _manifest_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "manifest.jsonl"

    def _profile_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "profile.json"

    def _delegations_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "delegations.jsonl"

    # ── 同步 I/O（在线程池内执行）──────────────────────────────────────────

    @staticmethod
    def _sync_write_json(path: Path, data: Dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _sync_read_json(path: Path) -> Optional[Dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _sync_append_jsonl(path: Path, record: Dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _sync_read_jsonl(path: Path) -> List[Dict]:
        """读取 JSONL 文件，忽略格式错误行。"""
        if not path.exists():
            return []
        lines: List[Dict] = []
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
        """在 turns/ 目录树中按 turn_id 查找文件（fallback 扫描）。"""
        if not turns_dir.exists():
            return None
        for p in turns_dir.rglob(f"{turn_id}.json"):
            return p
        return None

    # ── 异步 I/O 封装 ───────────────────────────────────────────────────────

    async def _write_turn(self, path: Path, data: Dict) -> None:
        await asyncio.to_thread(self._sync_write_json, path, data)

    async def _read_turn(
        self, user_id: str, turn_id: str, timestamp: str
    ) -> Optional[Dict]:
        path = self._turn_path(user_id, turn_id, timestamp)
        if not await asyncio.to_thread(path.exists):
            # 跨月或 timestamp 有偏差时兜底扫描
            turns_dir = self._user_dir(user_id) / "turns"
            path_found = await asyncio.to_thread(
                self._sync_find_turn, turns_dir, turn_id
            )
            if path_found is None:
                return None
            path = path_found
        return await asyncio.to_thread(self._sync_read_json, path)

    async def _read_manifest(self, user_id: str) -> List[Dict]:
        return await asyncio.to_thread(
            self._sync_read_jsonl, self._manifest_path(user_id)
        )

    async def _append_manifest(self, user_id: str, entry: Dict) -> None:
        await asyncio.to_thread(
            self._sync_append_jsonl, self._manifest_path(user_id), entry
        )

    async def _append_delegations(self, user_id: str, record: Dict) -> None:
        await asyncio.to_thread(
            self._sync_append_jsonl, self._delegations_path(user_id), record
        )

    # ── MemoryBackend 核心接口 ──────────────────────────────────────────────

    async def store_turn(
        self,
        user_id: str,
        turn_id: str,
        user_input: str,
        assistant_response: str,
        metadata: Optional[Dict[str, Any]] = None,
        agent_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """持久化一轮对话到文件系统。

        写操作通过 per-user Lock 串行化，避免 manifest 并发追加乱序。
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        meta = metadata or {}
        client_type = meta.get("client_type", "") or "api"

        turn_data: Dict[str, Any] = {
            "turn_id":            turn_id,
            "user_id":            user_id,
            "user_input":         user_input,
            "assistant_response": assistant_response,
            "timestamp":          timestamp,
            "client_type":        client_type,
            "intent":             meta.get("intent", ""),
            "agent_outputs":      agent_outputs or [],
            **{k: v for k, v in meta.items() if k not in ("client_type", "intent")},
        }

        async with self._write_lock(user_id):
            # 写入完整 JSON 文件
            path = self._turn_path(user_id, turn_id, timestamp)
            await self._write_turn(path, turn_data)

            # 追加 manifest 轻量索引（仅保留元数据）
            manifest_entry = {
                "turn_id":    turn_id,
                "timestamp":  timestamp,
                "client_type": client_type,
                "preview":    user_input[:80],
            }
            await self._append_manifest(user_id, manifest_entry)

        logger.debug(
            "FilesystemMemory.store_turn user=%s turn=%s path=%s",
            user_id, turn_id, path,
        )

    async def get_recent_turns(
        self,
        user_id: str,
        client_type: str = "",
    ) -> List[Dict[str, Any]]:
        """从 manifest 读取最近 N 条，加载对应 JSON 文件，时间升序返回。"""
        manifest = await self._read_manifest(user_id)

        if client_type:
            manifest = [m for m in manifest if m.get("client_type") == client_type]

        # 取最近 N 条（manifest 末尾为最新）
        recent_entries = manifest[-self._recent_n:]

        # 并发加载 turn 文件
        tasks = [
            self._read_turn(user_id, e["turn_id"], e.get("timestamp", ""))
            for e in recent_entries
        ]
        loaded = await asyncio.gather(*tasks, return_exceptions=False)

        # 过滤 None，保持原顺序（旧→新）
        turns = [t for t in loaded if t is not None]
        return turns

    async def retrieve_memory(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """关键词检索最近 200 条对话，返回最相关的 top_k 条。"""
        from app.memory.backends.filesystem.search import keyword_search

        manifest = await self._read_manifest(user_id)
        if not manifest:
            return []

        # 加载候选集（最近 200 条，避免全量扫描）
        candidates = manifest[-200:]
        tasks = [
            self._read_turn(user_id, e["turn_id"], e.get("timestamp", ""))
            for e in candidates
        ]
        loaded = await asyncio.gather(*tasks)
        loaded_turns = [t for t in loaded if t is not None]

        return keyword_search(query, loaded_turns, top_k=top_k)

    # ── 扩展功能 ─────────────────────────────────────────────────────────────

    async def build_system_prompt_block(self, user_id: str) -> str:
        """从 profile.json 读取用户画像并格式化为系统提示块。"""
        path = self._profile_path(user_id)
        if not await asyncio.to_thread(path.exists):
            return ""
        profile = await asyncio.to_thread(self._sync_read_json, path)
        if not profile:
            return ""

        lines = ["【用户画像参考】"]
        prefs = profile.get("preferences") or []
        pinfo = profile.get("personal_info") or []
        work  = profile.get("work_content") or []

        if prefs:
            lines.append("偏好: " + "；".join(str(p) for p in prefs[:5]))
        if pinfo:
            lines.append("个人信息: " + "；".join(str(p) for p in pinfo[:5]))
        if work:
            lines.append("工作背景: " + "；".join(str(p) for p in work[:5]))

        return "\n".join(lines) if len(lines) > 1 else ""

    async def save_profile(self, user_id: str, profile: Dict[str, Any]) -> None:
        """将用户画像写入 profile.json（供 auth 登录时调用）。"""
        path = self._profile_path(user_id)
        await asyncio.to_thread(self._sync_write_json, path, profile)
        logger.debug("FilesystemMemory.save_profile user=%s", user_id)

    async def on_delegation(
        self,
        user_id: str,
        agent_name: str,
        task: str,
        result: str,
        turn_id: str = "",
    ) -> None:
        """追加 Agent 委派记录到 delegations.jsonl。"""
        record = {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "turn_id":    turn_id,
            "agent_name": agent_name,
            "task":       task[:200],
            "result":     result[:200],
        }
        await self._append_delegations(user_id, record)

    async def compress_immediately(
        self,
        user_id: str,
        reason: str = "context_overflow",
    ) -> None:
        """文件系统后端暂不支持自动压缩，记录日志即可。"""
        logger.info(
            "FilesystemMemory: compress_immediately 被调用 user=%s reason=%s"
            "（文件系统后端暂不执行压缩）",
            user_id, reason,
        )

    def queue_prefetch(self, user_id: str, query: str) -> None:
        """异步预取：后台 Task 执行检索，结果缓存到内存供下轮使用。"""
        asyncio.create_task(self._run_prefetch(user_id, query))

    async def _run_prefetch(self, user_id: str, query: str) -> None:
        try:
            results = await self.retrieve_memory(user_id, query, top_k=5)
            async with self._prefetch_lock:
                self._prefetch_cache[user_id] = results
            logger.debug(
                "FilesystemMemory._run_prefetch user=%s hits=%d",
                user_id, len(results),
            )
        except Exception as e:
            logger.warning("FilesystemMemory 预取失败 user=%s: %s", user_id, e)

    async def get_prefetched_context(self, user_id: str) -> List[Dict[str, Any]]:
        """读取并清除预取缓存（一次性使用）。"""
        async with self._prefetch_lock:
            return self._prefetch_cache.pop(user_id, [])

    # ── 注入钩子（无操作，接口兼容）────────────────────────────────────────

    def set_vector_store(self, vector_store: Any) -> None:
        pass  # 文件系统后端不使用向量存储

    def set_default_llm(self, llm: Any) -> None:
        pass

    def set_archiver(self, archiver: Any) -> None:
        pass
