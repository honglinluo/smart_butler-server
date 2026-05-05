"""工具权限管理 — 危险操作同意核查与授权记录。

同意级别（低 → 高）：
  once    — 仅本次调用有效（内存临时标记，不持久化）
  session — 本会话内有效（按 session_id 缓存）
  project — 本项目内有效（按 project_id 持久化到 MySQL）
  always  — 永久有效（持久化到 MySQL）

流程：
  1. BaseTool.__init_subclass__ 生成的 _wrapped_execute 调用 check_consented()
  2. 未授权 → 抛出 ConsentRequiredException
  3. 上层（HermesEngine / API 层）捕获后向前端推送授权请求
  4. 用户选择授权级别后调用 grant_consent()
  5. 重新执行工具（once 级别由 HermesEngine 在重试时临时注入 context）
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

from app.database.pool import get_connection, release_connection
from app.tools.base import (
    CONSENT_ALWAYS, CONSENT_ONCE, CONSENT_PROJECT, CONSENT_SESSION,
)

logger = logging.getLogger(__name__)

# 内存缓存结构：
#   _session_cache[(tool_name, op, session_id)] = True
#   _once_cache   [(tool_name, op, user_id)]    = True  （单次调用后立即清除）
_session_cache: Dict[Tuple[str, str, str], bool] = {}
_once_granted:  Set[Tuple[str, str, str]]        = set()


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
        """检查用户是否已为指定工具操作授权。按 once > session > project > always 顺序命中即返回。"""

        # 1. once 级别（本次调用，调用后由调用方手动清除）
        if (tool_name, operation, user_id) in _once_granted:
            return True

        # 2. session 级别（内存缓存）
        if session_id and _session_cache.get((tool_name, operation, session_id)):
            return True

        # 3. project / always 级别（MySQL 持久化）
        return await self._check_db_consent(
            tool_name, operation, user_id, session_id, project_id
        )

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
            if not conn:
                return False

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
        """临时授权本次调用（内存，不持久化）。HermesEngine 重试前调用，执行后必须调用 revoke_once()。"""
        _once_granted.add((tool_name, operation, user_id))
        logger.debug("once 授权: tool=%s op=%s user=%s", tool_name, operation, user_id)

    def revoke_once(self, tool_name: str, operation: str, user_id: str) -> None:
        """清除 once 级别授权。"""
        _once_granted.discard((tool_name, operation, user_id))

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
            if not conn:
                return
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
            if not conn:
                return
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
            if not conn:
                return []
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
