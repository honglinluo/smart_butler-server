"""
【模块说明】Agent 事件循环日志记录器（LoopLogger）— 记录 Agent 每一步执行过程

当 Agent 执行复杂任务时，需要知道它每一步做了什么、中间发生了什么事。
这个模块负责把每个执行事件（任务开始、工具被请求、工具构建完成等）记录下来，
同时推送到 Redis 队列，供前端实时显示进度。

【两路输出】
  1. Python 标准日志（INFO 级别）：写到服务器日志文件，供运维查看
  2. Redis List：推送到 Redis，前端通过 SSE 长连接实时读取，展示"AI 正在做什么"

Agent 事件循环 — 结构化调用日志记录器
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from app.agents.loop.events import LoopEventType, LoopLogEntry

logger = logging.getLogger("agent_loop")

_LOG_KEY_TTL = 3600  # Redis list TTL：1 h


class LoopLogger:
    """记录 AgentEventLoop 的执行日志，同时推送到 Redis 供实时消费。

    每条日志同时写入：
    - Python 标准 logger（INFO 级别，可在进程日志中查看）
    - Redis List ``user:{user_id}:loop_logs:{session_id}``（供 SSE/轮询消费）
    """

    def __init__(
        self,
        user_id:    str,
        session_id: str,
        redis_db    = None,
    ) -> None:
        self.user_id    = user_id
        self.session_id = session_id
        self._redis     = redis_db
        self._entries:  List[LoopLogEntry] = []

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    def log(
        self,
        event_type: LoopEventType,
        agent_name: str,
        message:    str,
        iteration:  int = 0,
        data:       Optional[Dict[str, Any]] = None,
    ) -> LoopLogEntry:
        """记录一条事件，写标准日志并异步推送 Redis。"""
        entry = LoopLogEntry(
            event_type=event_type,
            agent_name=agent_name,
            message   =message,
            iteration =iteration,
            data      =data,
        )
        self._entries.append(entry)

        logger.info(
            "[AgentLoop][sess=%s][i=%d][%s] %s: %s",
            self.session_id[:8], iteration, agent_name,
            event_type.value, message,
        )

        if self._redis is not None:
            try:
                asyncio.create_task(self._push(entry))
            except RuntimeError:
                pass  # 没有运行中的事件循环（如单元测试）

        return entry

    def get_entries(self) -> List[LoopLogEntry]:
        return list(self._entries)

    def format_log(self) -> str:
        """格式化全部日志为多行文本（可嵌入最终响应或写入文件）。"""
        return "\n".join(e.format_line() for e in self._entries)

    # ── 内部 ──────────────────────────────────────────────────────────────────

    async def _push(self, entry: LoopLogEntry) -> None:
        try:
            key     = f"user:{self.user_id}:loop_logs:{self.session_id}"
            payload = json.dumps(entry.to_dict(), ensure_ascii=False)
            await self._redis.rpush(key, payload)
            await self._redis.expire(key, _LOG_KEY_TTL)
        except Exception as exc:
            logger.debug("loop log 推送 Redis 失败: %s", exc)
