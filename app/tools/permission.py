"""
【模块说明】工具危险操作授权管理 — AI 执行危险动作前的"安全门"

当 AI 要执行可能有破坏性的操作时（如修改文件、执行命令、删除数据），
本模块负责检查用户是否已经授权，未授权则暂停执行并等待用户确认。

【授权级别（从低到高）】
  once         — 仅此一次有效，下次同样操作还会再问
  conversation — 本轮对话（一次用户消息）内全部放行，发新消息后恢复
  session      — 本次登录会话内有效
  project      — 本项目内永久有效（存入数据库）
  always       — 永久不再询问（存入数据库）

【流式对话中的授权流程】
  1. AI 工具执行危险操作前调用 check_consented() 检查
  2. 未授权 → 通过 SSE 向前端推送"授权请求"弹窗，工具执行暂停
  3. 用户在弹窗中选择"允许/拒绝/当前对话全部允许"
  4. 前端调用 POST /chat/consent 传递决策
  5. 工具收到决策后继续执行或返回拒绝结果

【性能优化】
  _is_op_enabled() 查询数据库的结果会缓存 60 秒，
  用户在设置页面切换开关后会立即让缓存失效，确保下次调用就生效。

工具权限管理 — 危险操作同意核查与授权记录。

同意级别（低 → 高）：
  once         — 仅本次调用有效（内存临时标记，不持久化）
  conversation — 当前用户消息轮次内全部允许（下次提问时清除）
  session      — 本会话内有效（按 session_id 缓存）
  project      — 本项目内有效（按 project_id 持久化到 MySQL）
  always       — 永久有效（持久化到 MySQL）

流程（流式场景）：
  1. BaseTool._wrapped_execute 调用 check_consented()
  2. 未授权且存在 consent_hook → hook 推送 SSE 事件 + await Future（暂停）
  3. 用户在前端选择后调用 POST /chat/consent → resolve Future
  4. hook 返回决策，工具继续或返回拒绝结果

非流式场景（无 hook）：
  2. 未授权 → 抛出 ConsentRequiredException
  3. 上层捕获后向前端推送授权请求
  4. 用户授权后调用 grant_consent()
"""

import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set, Tuple

from app.database.pool import get_connection, release_connection
from app.tools.base import (
    CONSENT_ALWAYS, CONSENT_CONVERSATION, CONSENT_ONCE, CONSENT_PROJECT, CONSENT_SESSION,
)

logger = logging.getLogger(__name__)

# ── _is_op_enabled TTL 缓存（避免每次工具调用都打 MySQL） ────────────────────
_OP_ENABLED_TTL = 60.0  # 秒；用户在设置页面切换后最长 60s 生效
_op_enabled_cache: Dict[Tuple[str, str], Tuple[bool, float]] = {}  # (user_id, op) → (enabled, ts)


def _invalidate_op_cache(user_id: str, operation: str) -> None:
    """toggling dangerous-op 时由 tools_api 调用以立即生效。"""
    _op_enabled_cache.pop((user_id, operation), None)


# ── 内存缓存 ─────────────────────────────────────────────────────────────────
# _session_cache         [(tool_name, op, session_id)] = True
# _once_granted          [(tool_name, op, user_id)]    = True  （单次调用后清除）
# _conversation_cache    [(tool_name, op, turn_id)]    = True  （下次用户提问后失效）
# _conversation_blanket  {turn_id}                     — 本轮全量放行（用户选"当前对话允许"时设置）
_session_cache:         Dict[Tuple[str, str, str], bool] = {}
_once_granted:          Set[Tuple[str, str, str]]        = set()
_conversation_cache:    Dict[Tuple[str, str, str], bool] = {}
_conversation_blanket:  Set[str]                         = set()

# ── ContextVar：流式场景由 HermesEngine 注入 ─────────────────────────────────
# hook: async (ConsentRequiredException) -> str  ("allow" | "deny" | "conversation")
_CONSENT_HOOK: ContextVar[Optional[Callable]] = ContextVar("consent_hook", default=None)
# turn_id：当前用户消息轮次，用于 conversation 级别缓存的 key
_CONSENT_TURN_ID: ContextVar[str] = ContextVar("consent_turn_id", default="")


def set_consent_hook(hook: Optional[Callable]) -> Any:
    """设置当前 asyncio 上下文的 consent hook，返回 Token 供 reset。"""
    return _CONSENT_HOOK.set(hook)


def get_consent_hook() -> Optional[Callable]:
    return _CONSENT_HOOK.get()


def set_consent_turn_id(turn_id: str) -> Any:
    return _CONSENT_TURN_ID.set(turn_id)


class ConsentManager:
    """
    工具危险操作授权管理器（进程级单例）。

    外部使用示例::

        from app.tools.permission import consent_manager

        # 核查（BaseTool 自动调用，通常不需要手动调用）
        ok = await consent_manager.check_consented(
            "file_delete", "delete", user_id, session_id, project_id
        )

        # 授权（前端返回用户选择后由 API 调用）
        await consent_manager.grant_consent(
            "file_delete", "delete", user_id, "session", session_id=session_id
        )

        # once 级别：由 HermesEngine 在重试前临时注入，执行后自动清除
        consent_manager.grant_once("file_delete", "delete", user_id)
    """

    _instance: Optional["ConsentManager"] = None

    def __new__(cls) -> "ConsentManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── 核查 ──────────────────────────────────────────────────────────────────

    async def check_consented(
        self,
        tool_name:  str,
        operation:  str,
        user_id:    str,
        session_id: str = "",
        project_id: str = "",
    ) -> bool:
        """检查用户是否已为指定工具操作授权。

        优先级：op_disabled > once > conversation > session > project/always
        """
        # 0. 若用户已关闭该操作类型，直接放行（无需授权）
        if not await self._is_op_enabled(operation, user_id):
            return True

        # 1. once 级别
        if (tool_name, operation, user_id) in _once_granted:
            return True

        # 2. conversation 级别（当前消息轮次）
        turn_id = _CONSENT_TURN_ID.get()
        if turn_id and turn_id in _conversation_blanket:
            return True
        if turn_id and _conversation_cache.get((tool_name, operation, turn_id)):
            return True

        # 3. session 级别（内存缓存）
        if session_id and _session_cache.get((tool_name, operation, session_id)):
            return True

        # 4. project / always 级别（MySQL 持久化）
        return await self._check_db_consent(
            tool_name, operation, user_id, session_id, project_id
        )

    async def _is_op_enabled(self, operation: str, user_id: str) -> bool:
        """查询用户是否开启了该危险操作类型（无记录 = 默认开启）。结果按 TTL 缓存。"""
        cache_key = (user_id, operation)
        cached = _op_enabled_cache.get(cache_key)
        if cached is not None:
            enabled, ts = cached
            if time.monotonic() - ts < _OP_ENABLED_TTL:
                return enabled

        conn = None
        try:
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                "SELECT is_enabled FROM dangerous_op_configs "
                "WHERE user_id = :uid AND op_type = :op",
                {"uid": user_id, "op": operation},
            )
            if rows is None or len(rows) == 0:
                result = True
            else:
                result = bool(rows.iloc[0]["is_enabled"])
            _op_enabled_cache[cache_key] = (result, time.monotonic())
            return result
        except Exception as e:
            logger.debug("查询 dangerous_op_configs 失败 op=%s: %s", operation, e)
            return True
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def _check_db_consent(
        self,
        tool_name:  str,
        operation:  str,
        user_id:    str,
        session_id: str,
        project_id: str,
    ) -> bool:
        """查询 MySQL tool_consent_records，匹配 always 或当前 project 记录。"""
        conn = None
        try:
            conn = await get_connection("mysql", None)

            rows = await conn.execute_raw(
                """
                SELECT consent_level, project_id
                FROM tool_consent_records
                WHERE tool_name = :tool
                  AND operation  = :op
                  AND user_id    = :uid
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND consent_level IN ('always', 'project')
                ORDER BY consent_level DESC
                """,
                {"tool": tool_name, "op": operation, "uid": user_id},
            )

            if rows is None or len(rows) == 0:
                return False

            for _, row in rows.iterrows():
                level = row["consent_level"]
                if level == CONSENT_ALWAYS:
                    return True
                if level == CONSENT_PROJECT and project_id:
                    if row.get("project_id") == project_id:
                        return True

            return False

        except Exception as e:
            logger.debug("查询工具授权记录失败 tool=%s: %s", tool_name, e)
            return False
        finally:
            if conn:
                await release_connection("mysql", conn)

    # ── 授权 ──────────────────────────────────────────────────────────────────

    def grant_once(self, tool_name: str, operation: str, user_id: str) -> None:
        """临时授权本次调用（内存，不持久化）。"""
        _once_granted.add((tool_name, operation, user_id))
        logger.debug("once 授权: tool=%s op=%s user=%s", tool_name, operation, user_id)

    def revoke_once(self, tool_name: str, operation: str, user_id: str) -> None:
        _once_granted.discard((tool_name, operation, user_id))

    def grant_conversation(self, tool_name: str, operation: str, turn_id: str = "") -> None:
        """当前消息轮次内允许该工具的指定操作（内存，不持久化）。"""
        _tid = turn_id or _CONSENT_TURN_ID.get()
        if not _tid:
            logger.warning("grant_conversation: 无 turn_id，降级为 once 授权")
            return
        _conversation_cache[(tool_name, operation, _tid)] = True
        logger.debug("conversation 授权: tool=%s op=%s turn=%s", tool_name, operation, _tid[:8])

    def grant_conversation_all(self, turn_id: str = "") -> None:
        """当前消息轮次内放行全部危险操作（用户选"当前对话允许"时调用）。"""
        _tid = turn_id or _CONSENT_TURN_ID.get()
        if not _tid:
            logger.warning("grant_conversation_all: 无 turn_id，无法设置全量放行")
            return
        _conversation_blanket.add(_tid)
        logger.debug("conversation 全量放行: turn=%s", _tid[:8])

    def revoke_conversation(self, turn_id: str) -> int:
        """清除指定轮次的全部 conversation 级授权（含全量放行）。"""
        keys = [k for k in _conversation_cache if k[2] == turn_id]
        for k in keys:
            del _conversation_cache[k]
        had_blanket = turn_id in _conversation_blanket
        _conversation_blanket.discard(turn_id)
        return len(keys) + (1 if had_blanket else 0)

    async def grant_consent(
        self,
        tool_name:     str,
        operation:     str,
        user_id:       str,
        consent_level: str,
        session_id:    str = "",
        project_id:    str = "",
    ) -> None:
        """
        记录用户授权决定。

        Args:
            consent_level: once / session / project / always
        """
        if consent_level == CONSENT_ONCE:
            # once 不持久化，调用方通过 grant_once() 临时注入
            return

        if consent_level == CONSENT_CONVERSATION:
            self.grant_conversation(tool_name, operation)
            return

        if consent_level == CONSENT_SESSION:
            if not session_id:
                logger.warning("session 级别授权缺少 session_id，已忽略")
                return
            _session_cache[(tool_name, operation, session_id)] = True
            logger.info(
                "session 授权: tool=%s op=%s session=%s", tool_name, operation, session_id[:8]
            )
            return

        # project / always → 持久化到 MySQL
        await self._save_db_consent(
            tool_name, operation, user_id, consent_level, session_id, project_id
        )

    async def _save_db_consent(
        self,
        tool_name:     str,
        operation:     str,
        user_id:       str,
        consent_level: str,
        session_id:    str,
        project_id:    str,
    ) -> None:
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                INSERT INTO tool_consent_records
                    (tool_name, operation, user_id, consent_level, session_id, project_id, granted_at)
                VALUES
                    (:tool, :op, :uid, :level, :sid, :pid, :ts)
                ON DUPLICATE KEY UPDATE
                    consent_level = VALUES(consent_level),
                    granted_at    = VALUES(granted_at)
                """,
                {
                    "tool":  tool_name,
                    "op":    operation,
                    "uid":   user_id,
                    "level": consent_level,
                    "sid":   session_id or None,
                    "pid":   project_id or None,
                    "ts":    datetime.now(timezone.utc),
                },
            )
            logger.info(
                "工具授权已持久化: tool=%s op=%s user=%s level=%s",
                tool_name, operation, user_id, consent_level,
            )
        except Exception as e:
            logger.warning("持久化工具授权失败 tool=%s: %s", tool_name, e)
        finally:
            if conn:
                await release_connection("mysql", conn)

    # ── 撤销 ──────────────────────────────────────────────────────────────────

    def revoke_session(self, session_id: str) -> int:
        """清除指定会话的所有 session 级别授权（会话结束时调用）。"""
        keys = [k for k in _session_cache if k[2] == session_id]
        for k in keys:
            del _session_cache[k]
        if keys:
            logger.info("会话授权已清除: session=%s count=%d", session_id[:8], len(keys))
        return len(keys)

    async def revoke_always(
        self, tool_name: str, operation: str, user_id: str
    ) -> None:
        """撤销 always 级别授权（用户主动取消时调用）。"""
        conn = None
        try:
            conn = await get_connection("mysql", None)
            await conn.execute_raw(
                """
                DELETE FROM tool_consent_records
                WHERE tool_name = :tool AND operation = :op
                  AND user_id = :uid AND consent_level = 'always'
                """,
                {"tool": tool_name, "op": operation, "uid": user_id},
            )
        except Exception as e:
            logger.warning("撤销授权失败 tool=%s: %s", tool_name, e)
        finally:
            if conn:
                await release_connection("mysql", conn)

    async def list_user_consents(self, user_id: str) -> list:
        """查询用户所有持久化授权记录（用于前端展示/管理）。"""
        conn = None
        try:
            conn = await get_connection("mysql", None)
            rows = await conn.execute_raw(
                """
                SELECT tool_name, operation, consent_level, project_id, granted_at
                FROM tool_consent_records
                WHERE user_id = :uid
                ORDER BY granted_at DESC
                """,
                {"uid": user_id},
            )
            if rows is None or len(rows) == 0:
                return []
            return rows.to_dict(orient="records")
        except Exception as e:
            logger.warning("查询用户授权列表失败: %s", e)
            return []
        finally:
            if conn:
                await release_connection("mysql", conn)


# 全局单例
consent_manager = ConsentManager()
