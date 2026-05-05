"""Skill 生成 Agent — 根据规格生成/优化 Agent 技能记忆文件（Markdown）"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base import BaseAgent
from app.agents.decorators import agent

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 输入数据格式规范（供调用方参考）
# ══════════════════════════════════════════════════════════════════════

INPUT_SCHEMA: Dict[str, Any] = {
    "$comment": (
        "SkillBuilderAgent 支持两种 action。"
        "task 字典可直接包含以下字段，也可将 JSON 序列化后放入 task.description。"
    ),
    "generate": {
        "action": "generate",
        "skill_name": "string — 技能名称，用于文件标题（例：'数据分析技能'）",
        "target_path": "string — 保存路径，绝对或相对路径（例：'/data/skills/analyst.md'）",
        "spec": {
            "description": "string [必填] — 一句话说明技能用途",
            "trigger_conditions": [
                "string — 何时触发此技能（例：'用户请求数据查询时'）"
            ],
            "prerequisites": [
                "string — 执行前置条件（例：'需要数据库连接'）"
            ],
            "steps": [
                {
                    "step": "1",
                    "action": "string — 执行动作描述",
                    "note": "string（可选）— 补充说明",
                }
            ],
            "examples": [
                {
                    "input": "string — 用户输入示例",
                    "output": "string — 期望输出示例",
                }
            ],
            "notes": "string（可选）— 额外约束、边界情况或常见错误",
        },
    },
    "optimize": {
        "action": "optimize",
        "skill_name": "string — 技能名称",
        "target_path": "string — 保存路径（与现有文件相同则覆盖，不同则另存）",
        "spec": {
            "existing_content": (
                "string [必填，若为空则自动读取 target_path 文件] — 当前技能文件内容"
            ),
            "improvement_notes": "string [必填] — 需要优化的方向或具体问题",
            "usage_feedback": "string（可选）— 该技能的实际使用反馈，辅助改进",
        },
    },
}


# ══════════════════════════════════════════════════════════════════════
# LLM 提示词
# ══════════════════════════════════════════════════════════════════════

_GENERATE_SYSTEM = """\
你是一个 Agent 技能文件生成专家。根据给定的技能规格，生成一份结构完整的 Markdown 技能记忆文件。

该文件将被 Agent 在运行时读取并注入提示词，因此要求：
1. 语言精炼、直接，避免冗余描述
2. 步骤可执行、边界清晰
3. 示例具代表性，覆盖正常和边界情况
4. 全文使用中文

输出格式（严格遵守，不要输出任何额外内容）：

---
name: <技能名称>
description: <一句话描述>
created_at: <YYYY-MM-DD>
version: "1.0"
---

## 描述
<2-3 句详细描述，说明技能的能力边界>

## 触发条件
<何时使用此技能，用无序列表>

## 前置条件
<执行前需满足的条件，若无写"无">

## 执行步骤
<有序步骤列表，每步一行，格式：数字. 动作描述>

## 示例
<输入/输出示例，用引用块或代码块>

## 注意事项
<约束、常见错误、边界情况，若无写"无">
"""

_OPTIMIZE_SYSTEM = """\
你是一个 Agent 技能文件优化专家。根据优化方向和使用反馈，对现有技能文件进行改进。

优化原则：
1. 保留原有正确内容，只补充和修正
2. frontmatter 中 version 字段在原版本号 +0.1（如 "1.0" → "1.1"），并添加/更新 last_updated 字段
3. 根据使用反馈修正步骤、补充示例或澄清边界
4. 输出完整的改进后文件（含 frontmatter），不要附加任何解释性文字
"""


# ══════════════════════════════════════════════════════════════════════
# SkillBuilderAgent
# ══════════════════════════════════════════════════════════════════════

@agent(
    name="skill_builder",
    role="技能生成专家",
    background=(
        "你是 Hermes 系统的技能文件生成与优化专家。\n"
        "职责：\n"
        "- 根据结构化规格（JSON）生成 Agent 技能记忆文件（Markdown 格式）\n"
        "- 分析现有技能文件并按优化方向生成改进版本\n"
        "- 将生成文件写入指定路径（自动创建父目录）\n\n"
        "生成的技能文件将被其他 Agent 在运行时注入提示词，因此必须清晰、可执行。"
    ),
    tools=["file_write", "file_read"],
)
class SkillBuilderAgent(BaseAgent):
    """
    技能记忆文件生成与优化 Agent。

    调用方式（其他 Agent / 用户）：

    方式 A — task 直接包含字段：
        task = SkillBuilderAgent.build_generate_task(
            skill_name="数据分析技能",
            target_path="/data/skills/analyst.md",
            description="帮助 Agent 完成 SQL 查询和数据可视化任务",
            trigger_conditions=["用户请求数据查询", "用户要求生成图表"],
            steps=[{"step": "1", "action": "解析用户意图，提取关键指标"}],
        )
        result = await agent.execute(task, context, llm)

    方式 B — task.description 为 JSON 字符串：
        task = {"description": json.dumps({...}, ensure_ascii=False)}

    完整输入格式见 SkillBuilderAgent.INPUT_SCHEMA。
    """

    INPUT_SCHEMA = INPUT_SCHEMA

    # ── 主入口 ────────────────────────────────────────────────────────

    async def execute(self, task: dict, context: dict, llm) -> dict:
        if llm is None:
            return {
                "result": "LLM 未配置，无法生成技能文件",
                "success": False,
                "metadata": {},
            }

        parsed = self._parse_task(task)
        if "error" in parsed:
            return {
                "result": parsed["error"],
                "success": False,
                "metadata": {"input_schema": self.INPUT_SCHEMA},
            }

        action = parsed["action"]
        skill_name = parsed["skill_name"]
        target_path = parsed["target_path"]
        spec = parsed["spec"]

        if action == "generate":
            return await self._do_generate(skill_name, target_path, spec, llm)
        else:  # optimize
            return await self._do_optimize(skill_name, target_path, spec, llm)

    # ── 生成技能文件 ──────────────────────────────────────────────────

    async def _do_generate(
        self, skill_name: str, target_path: str, spec: dict, llm
    ) -> dict:
        if not spec.get("description"):
            return {
                "result": "spec.description 为必填项，请提供技能用途描述",
                "success": False,
                "metadata": {"required_field": "spec.description"},
            }

        spec_json = json.dumps(spec, ensure_ascii=False, indent=2)
        human_content = (
            f"技能名称：{skill_name}\n"
            f"当前日期：{datetime.now().strftime('%Y-%m-%d')}\n\n"
            f"技能规格（JSON）：\n```json\n{spec_json}\n```\n\n"
            "请生成完整的技能记忆文件。"
        )

        try:
            resp = await llm.ainvoke([
                SystemMessage(content=_GENERATE_SYSTEM),
                HumanMessage(content=human_content),
            ])
            content = resp.content if hasattr(resp, "content") else str(resp)
            saved_path = self._write_file(target_path, content)

            await self.update_skill(
                f"生成技能:{skill_name}",
                f"target={target_path} steps={len(spec.get('steps', []))}",
                success=True,
            )
            logger.info("技能文件已生成: skill=%s path=%s", skill_name, saved_path)
            return {
                "result": f"✅ 技能文件已生成并保存到 {saved_path}",
                "success": True,
                "metadata": {
                    "agent": self.name,
                    "action": "generate",
                    "skill_name": skill_name,
                    "saved_path": str(saved_path),
                    "content_preview": content[:300],
                },
            }
        except PermissionError as e:
            logger.error("技能文件写入权限不足: path=%s error=%s", target_path, e)
            return {
                "result": f"写入失败（权限不足）: {target_path}",
                "success": False,
                "metadata": {},
            }
        except Exception as e:
            logger.error("技能生成失败: skill=%s error=%s", skill_name, e)
            return {"result": f"生成失败: {e}", "success": False, "metadata": {}}

    # ── 优化技能文件 ──────────────────────────────────────────────────

    async def _do_optimize(
        self, skill_name: str, target_path: str, spec: dict, llm
    ) -> dict:
        existing_content = spec.get("existing_content", "").strip()
        improvement_notes = spec.get("improvement_notes", "").strip()

        # existing_content 为空时尝试从 target_path 读取
        if not existing_content:
            try:
                p = Path(target_path)
                if p.exists():
                    existing_content = p.read_text(encoding="utf-8")
                    logger.debug("从文件读取现有技能内容: %s", target_path)
            except Exception as e:
                logger.warning("读取技能文件失败: path=%s error=%s", target_path, e)

        if not existing_content:
            return {
                "result": (
                    "spec.existing_content 为空，且无法读取 target_path 处的文件。\n"
                    "请在 spec.existing_content 中提供当前技能文件内容，"
                    "或确认 target_path 路径正确且文件存在。"
                ),
                "success": False,
                "metadata": {},
            }

        if not improvement_notes:
            return {
                "result": "spec.improvement_notes 为必填项，请描述需要优化的方向",
                "success": False,
                "metadata": {},
            }

        human_content = (
            f"技能名称：{skill_name}\n"
            f"优化方向：{improvement_notes}\n"
            f"使用反馈：{spec.get('usage_feedback', '无')}\n"
            f"当前日期：{datetime.now().strftime('%Y-%m-%d')}\n\n"
            f"当前技能文件内容：\n```markdown\n{existing_content}\n```\n\n"
            "请生成改进后的完整技能文件内容。"
        )

        try:
            resp = await llm.ainvoke([
                SystemMessage(content=_OPTIMIZE_SYSTEM),
                HumanMessage(content=human_content),
            ])
            content = resp.content if hasattr(resp, "content") else str(resp)
            saved_path = self._write_file(target_path, content)

            await self.update_skill(
                f"优化技能:{skill_name}",
                improvement_notes[:80],
                success=True,
            )
            logger.info("技能文件已优化: skill=%s path=%s", skill_name, saved_path)
            return {
                "result": f"✅ 技能文件已优化并保存到 {saved_path}",
                "success": True,
                "metadata": {
                    "agent": self.name,
                    "action": "optimize",
                    "skill_name": skill_name,
                    "saved_path": str(saved_path),
                    "content_preview": content[:300],
                },
            }
        except PermissionError as e:
            logger.error("技能文件写入权限不足: path=%s error=%s", target_path, e)
            return {
                "result": f"写入失败（权限不足）: {target_path}",
                "success": False,
                "metadata": {},
            }
        except Exception as e:
            logger.error("技能优化失败: skill=%s error=%s", skill_name, e)
            return {"result": f"优化失败: {e}", "success": False, "metadata": {}}

    # ── 输入解析与校验 ────────────────────────────────────────────────

    def _parse_task(self, task: dict) -> dict:
        """从 task 字典或 task.description JSON 中提取并校验输入字段。"""
        # 优先从 task 顶层读取
        if "action" in task:
            return self._validate(task)

        # 尝试将 description 作为 JSON 解析
        raw = task.get("description", "")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data:
                    return self._validate(data)
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "error": (
                "无法解析输入。task 应包含顶层字段 action/skill_name/target_path/spec，"
                "或 task.description 应为包含这些字段的 JSON 字符串。\n"
                "可使用 SkillBuilderAgent.build_generate_task() / "
                "build_optimize_task() 构建标准输入格式。\n\n"
                f"完整格式参考：\n{json.dumps(self.INPUT_SCHEMA, ensure_ascii=False, indent=2)}"
            )
        }

    @staticmethod
    def _validate(data: dict) -> dict:
        required = ("action", "skill_name", "target_path", "spec")
        missing = [f for f in required if not data.get(f)]
        if missing:
            return {"error": f"缺少必填字段: {missing}"}
        if data["action"] not in ("generate", "optimize"):
            return {
                "error": (
                    f"action 必须为 'generate' 或 'optimize'，"
                    f"当前值: {data['action']!r}"
                )
            }
        if not isinstance(data["spec"], dict):
            return {"error": "spec 必须为字典（dict）"}
        return data

    @staticmethod
    def _write_file(target_path: str, content: str) -> Path:
        """将内容写入文件，自动创建父目录。"""
        p = Path(target_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info("已写入技能文件: %s (%d bytes)", p, len(content.encode()))
        return p

    # ── 便捷构建方法（供其他 Agent 调用时使用）──────────────────────

    @classmethod
    def build_generate_task(
        cls,
        skill_name: str,
        target_path: str,
        description: str,
        trigger_conditions: Optional[List[str]] = None,
        prerequisites: Optional[List[str]] = None,
        steps: Optional[List[Dict[str, str]]] = None,
        examples: Optional[List[Dict[str, str]]] = None,
        notes: str = "",
    ) -> dict:
        """
        构建 generate 任务字典，直接传入 execute()。

        其他 Agent 在完成任务后若需生成技能记忆，调用此方法构建 task：

            from app.agents.workers.skill_builder import SkillBuilderAgent
            from app.agents.registry import registry

            task = SkillBuilderAgent.build_generate_task(
                skill_name="SQL 查询技能",
                target_path="/data/skills/sql_query.md",
                description="帮助 Agent 构造并执行 SQL 查询语句",
                trigger_conditions=["用户要求查询数据库"],
                steps=[{"step": "1", "action": "解析用户意图"}],
            )
            builder = registry.get("skill_builder")
            result = await builder.execute(task, context={}, llm=llm)
        """
        return {
            "action": "generate",
            "skill_name": skill_name,
            "target_path": target_path,
            "spec": {
                "description": description,
                "trigger_conditions": trigger_conditions or [],
                "prerequisites": prerequisites or [],
                "steps": steps or [],
                "examples": examples or [],
                "notes": notes,
            },
        }

    @classmethod
    def build_optimize_task(
        cls,
        skill_name: str,
        target_path: str,
        improvement_notes: str,
        existing_content: str = "",
        usage_feedback: str = "",
    ) -> dict:
        """
        构建 optimize 任务字典，直接传入 execute()。

        existing_content 为空时，Agent 会自动读取 target_path 处的文件。

            task = SkillBuilderAgent.build_optimize_task(
                skill_name="SQL 查询技能",
                target_path="/data/skills/sql_query.md",
                improvement_notes="步骤描述不够详细，缺少错误处理示例",
                usage_feedback="执行复杂 JOIN 时步骤不清晰",
            )
        """
        return {
            "action": "optimize",
            "skill_name": skill_name,
            "target_path": target_path,
            "spec": {
                "existing_content": existing_content,
                "improvement_notes": improvement_notes,
                "usage_feedback": usage_feedback,
            },
        }

    # ── 后台便捷调用（其他 Agent 无需 await 结果时使用）──────────────

    @staticmethod
    async def dispatch(
        action: str,
        skill_name: str,
        target_path: str,
        spec: dict,
        llm,
        context: Optional[dict] = None,
    ) -> dict:
        """
        从其他 Agent 或路由器直接调用 SkillBuilderAgent，无需通过 registry 获取实例。

            result = await SkillBuilderAgent.dispatch(
                action="generate",
                skill_name="...",
                target_path="...",
                spec={...},
                llm=llm,
            )
        """
        from app.agents.registry import registry
        builder = registry.get("skill_builder")
        if builder is None:
            builder = SkillBuilderAgent()
        task = {
            "action": action,
            "skill_name": skill_name,
            "target_path": target_path,
            "spec": spec,
        }
        return await builder.execute(task, context or {}, llm)
