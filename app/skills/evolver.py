"""
【模块说明】Skill 自演进引擎（SkillEvolver）— 让 Agent 的技能自动成长

随着用户不断使用某个 Agent，系统积累了大量"哪些任务成功了、用了什么方法"的数据。
这个模块利用这些数据，让 AI 自动从成功经验中学习，生成或优化 Skill 文件。

【演进的两种操作】
  生成（generate）：Agent 还没有 Skill 文件时，从历史成功案例中提炼出第一份经验
  优化（optimize）：Agent 已有 Skill 文件，但有更多使用数据积累，AI 对现有 Skill 进行改进

【触发条件（满足任一即可）】
  - 距上次演进后，Agent 又积累了至少 20 次调用（EVOLUTION_MIN_CALLS）
  - 管理员手动触发（force=True）

【安全机制】
  每次演进前先备份现有 Skill，如果新 Skill 格式验证失败或生成异常，自动回滚到备份版本

Skill 自演进引擎

演进流程：
  1. 从 agent_skills 表读取该 agent 的成功任务模式
  2. 若 agent 无 file skill → generate（生成新 skill）
     若已有 file skill     → optimize（优化现有 skill）
  3. 调用 SkillBuilderAgent 执行（_write_file 写到 target_path）
     → 注意：SkillBuilderAgent 写文件前我们先备份轮转，出错则自动回滚
  4. 校验新 skill 格式，失败则回滚
  5. 更新 Redis 最后演进时间戳

触发条件（任一满足）：
  - agent 距上次演进已有 >= EVOLUTION_MIN_CALLS 次调用
  - force=True（手动触发）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.paths import PROJECT_ROOT
from app.skills.manager import skill_manager, SKILLS_ROOT, _validate_skill_content

logger = logging.getLogger(__name__)

EVOLUTION_MIN_CALLS = 20          # 触发演进的最少调用次数
_REDIS_KEY = "skill:evo:{agent}:last_ts"
_REDIS_TTL = 90 * 86_400          # 90 天


# ─────────────────────────────────────────────────────────────────────────────
# SkillEvolver
# ─────────────────────────────────────────────────────────────────────────────

class SkillEvolver:
    """Agent skill 自演进引擎（单例，通过 skill_evolver 使用）。"""

    # ── Redis 时间戳 ──────────────────────────────────────────────────────────

    async def _get_last_ts(self, agent_name: str) -> Optional[datetime]:
        try:
            from app.database.pool import get_connection
            conn = await get_connection("redis", None)
            if conn and conn.redis_client:
                val = conn.redis_client.get(_REDIS_KEY.format(agent=agent_name))
                if val:
                    s = val if isinstance(val, str) else val.decode()
                    return datetime.fromisoformat(s)
        except Exception:
            pass
        return None

    async def _save_ts(self, agent_name: str) -> None:
        try:
            from app.database.pool import get_connection
            conn = await get_connection("redis", None)
            if conn and conn.redis_client:
                conn.redis_client.set(
                    _REDIS_KEY.format(agent=agent_name),
                    datetime.now().isoformat(),
                    ex=_REDIS_TTL,
                )
        except Exception as e:
            logger.debug("[SkillEvolver] 保存时间戳失败 agent=%s: %s", agent_name, e)

    # ── 调用次数 ──────────────────────────────────────────────────────────────

    async def _call_count_since_last(self, agent_name: str) -> int:
        from app.database.pool import get_connection, release_connection
        last = await self._get_last_ts(agent_name)
        conn = await get_connection("mysql", None)
        try:
            if last:
                sql = ("SELECT COUNT(*) AS cnt FROM agent_call_stats "
                       "WHERE agent_name = :name AND called_at > :ts")
                df = await conn.execute_raw(sql, {"name": agent_name, "ts": last})
            else:
                sql = "SELECT COUNT(*) AS cnt FROM agent_call_stats WHERE agent_name = :name"
                df = await conn.execute_raw(sql, {"name": agent_name})
            if df is not None and len(df) > 0:
                return int(df.iloc[0].get("cnt", 0))
        except Exception as e:
            logger.debug("[SkillEvolver] 查询调用次数失败 agent=%s: %s", agent_name, e)
        finally:
            await release_connection("mysql", conn)
        return 0

    # ── Agent 元数据 & 历史模式 ───────────────────────────────────────────────

    async def _gather_agent_data(self, agent_name: str) -> Dict[str, Any]:
        """读取 agent 的角色信息和 DB 中成功任务模式，供演进提示词构造使用。"""
        from app.database.pool import get_connection, release_connection

        patterns: List[Dict] = []
        role = ""
        background = ""

        # 从 agent_skills 表读取成功模式
        conn = await get_connection("mysql", None)
        if conn:
            try:
                df = await conn.execute_raw(
                    "SELECT description, pattern, success_rate, usage_count "
                    "FROM agent_skills WHERE agent_name = :name "
                    "ORDER BY success_rate DESC, usage_count DESC LIMIT 5",
                    {"name": agent_name},
                )
                if df is not None and len(df) > 0:
                    for _, row in df.iterrows():
                        patterns.append({
                            "description": str(row.get("description", "")),
                            "pattern":     str(row.get("pattern", "")),
                            "success_rate": float(row.get("success_rate", 1.0)),
                            "usage_count":  int(row.get("usage_count", 0)),
                        })
            except Exception as e:
                logger.debug("[SkillEvolver] 读取 agent_skills 失败 agent=%s: %s", agent_name, e)
            finally:
                await release_connection("mysql", conn)

        # 从 registry 读取角色元数据
        try:
            from app.agents.registry import registry
            ag = registry.get(agent_name)
            if ag:
                role = ag.role or ""
                background = (ag.background or "")[:300]
        except Exception:
            pass

        return {"patterns": patterns, "role": role, "background": background}

    # ── 演进判断 ──────────────────────────────────────────────────────────────

    async def should_evolve(self, agent_name: str, force: bool = False) -> bool:
        if force:
            return True
        cnt = await self._call_count_since_last(agent_name)
        return cnt >= EVOLUTION_MIN_CALLS

    # ── 主演进逻辑 ────────────────────────────────────────────────────────────

    async def evolve_agent(
        self,
        agent_name: str,
        llm,
        force: bool = False,
    ) -> Dict[str, Any]:
        """执行 agent skill 演进。

        Returns::

            {
              "success":    bool,
              "action":     "generate" | "optimize" | "skip",
              "skill_name": str,
              "message":    str,
            }
        """
        if not await self.should_evolve(agent_name, force):
            return {
                "success": True, "action": "skip",
                "skill_name": "", "message": "调用次数不足，跳过演进",
            }

        agent_data = await self._gather_agent_data(agent_name)
        if not agent_data["patterns"] and not agent_data["role"]:
            return {
                "success": False, "action": "skip",
                "skill_name": "", "message": "无 agent 数据，跳过演进",
            }

        existing = skill_manager.list_skills(agent_name)
        skill_name = existing[0] if existing else "main"
        skill_path = str(SKILLS_ROOT / agent_name / f"{skill_name}.md")

        from app.agents.workers.skill_builder import SkillBuilderAgent

        # ── 构建 SkillBuilderAgent 任务 ───────────────────────────────────────
        if existing:
            action = "optimize"
            existing_content = skill_manager.read_skill(agent_name, skill_name) or ""
            top_patterns = "\n".join(
                f"- {p['description']}: {p['pattern'][:80]} "
                f"（成功率 {p['success_rate']:.0%}，调用 {p['usage_count']} 次）"
                for p in agent_data["patterns"][:3]
            )
            spec = {
                "existing_content":  existing_content,
                "improvement_notes": (
                    f"基于最新调用数据优化技能文档。高频成功模式：\n{top_patterns}"
                ),
                "usage_feedback": top_patterns,
            }
        else:
            action = "generate"
            steps = [
                {
                    "step": str(i + 1),
                    "action": p["description"],
                    "note":   p["pattern"][:100],
                }
                for i, p in enumerate(agent_data["patterns"][:5])
            ]
            spec = {
                "description":        f"{agent_data['role']} 核心工作技能",
                "trigger_conditions": [f"处理 {agent_name} 类型任务时"],
                "prerequisites":      [],
                "steps":              steps if steps else [{"step": "1", "action": "按角色职责完成任务"}],
                "examples":           [],
                "notes":              agent_data["background"],
            }

        # ── 预备份（SkillBuilderAgent 直接写文件，需要我们提前轮转备份）─────────
        skill_dir = skill_manager.get_skill_dir(agent_name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        if existing:
            skill_manager._rotate_backups(skill_dir, skill_name)

        # ── 调用 SkillBuilderAgent ────────────────────────────────────────────
        try:
            result = await SkillBuilderAgent.dispatch(
                action=action,
                skill_name=f"{agent_name} 技能",
                target_path=skill_path,
                spec=spec,
                llm=llm,
            )
        except Exception as e:
            logger.error("[SkillEvolver] SkillBuilderAgent 调用异常 agent=%s: %s", agent_name, e)
            if existing:
                skill_manager.rollback_skill(agent_name, skill_name)
            return {"success": False, "action": action, "skill_name": skill_name, "message": str(e)}

        if not result.get("success"):
            # 演进失败 → 回滚
            if existing:
                skill_manager.rollback_skill(agent_name, skill_name)
            return {
                "success": False, "action": action,
                "skill_name": skill_name, "message": result.get("result", "演进失败"),
            }

        # ── 校验生成的 skill 文件 ─────────────────────────────────────────────
        new_content = skill_manager.read_skill(agent_name, skill_name)
        if new_content:
            err = _validate_skill_content(new_content)
            if err:
                logger.warning("[SkillEvolver] 新 skill 格式异常，回滚 agent=%s: %s", agent_name, err)
                skill_manager.rollback_skill(agent_name, skill_name)
                return {
                    "success": False, "action": action,
                    "skill_name": skill_name, "message": f"新 skill 校验失败: {err}，已回滚",
                }

        await self._save_ts(agent_name)
        logger.info("[SkillEvolver] 演进成功 agent=%s action=%s skill=%s",
                    agent_name, action, skill_name)
        return {
            "success": True, "action": action,
            "skill_name": skill_name,
            "message": result.get("result", "演进成功"),
        }

    # ── 批量演进（调度器调用）─────────────────────────────────────────────────

    async def evolve_all_agents(
        self,
        llm,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """遍历所有已注册的 code-type agent，逐个判断并执行演进。"""
        try:
            from app.agents.registry import registry
            agents = [a for a in registry.list_all() if a.source == "code"]
        except Exception as e:
            logger.error("[SkillEvolver] 获取 agent 列表失败: %s", e)
            return []

        results = []
        for ag in agents:
            try:
                r = await self.evolve_agent(ag.name, llm, force=force)
                results.append({"agent": ag.name, **r})
            except Exception as e:
                logger.error("[SkillEvolver] 演进异常 agent=%s: %s", ag.name, e)
                results.append({
                    "agent": ag.name, "success": False,
                    "action": "error", "skill_name": "", "message": str(e),
                })
        return results


skill_evolver = SkillEvolver()
