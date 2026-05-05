"""路由智能体 - 意图识别、任务分解、流水线规划与串行结果校验"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

_IDENTIFY_SYSTEM = """你是一个意图分类器。根据用户输入，从以下意图列表中选择最合适的一个。

可用意图（按所属智能体分组）：
{intent_groups}

只返回 JSON，例如：{{"intent": "data_analysis"}}
意图名称必须来自上面的列表，无法判断时返回 {{"intent": "general_question"}}"""

_DECOMPOSE_SYSTEM = """你是一个任务分解器。将用户请求拆解为具体可执行的子任务列表。

只返回 JSON 数组，每个子任务包含：
- task_id: "task_1"、"task_2" ...
- type: 与识别的意图一致
- description: 清晰具体的任务描述（中文）

若任务简单无需拆分，返回只含一个元素的数组。
示例：[{{"task_id": "task_1", "type": "data_query", "description": "查询上月销售额"}}]"""

_PLAN_MODE_SYSTEM = """你是一个多智能体编排器。

已识别意图：{intent}
任务列表：
{tasks_text}

各任务对应的 Agent：
{task_agent_map}

可用 Agent 说明：
{agents_info}

请判断执行模式：
- "single"   : 只有一个 Agent 处理
- "parallel" : 多个 Agent 同时并行处理，互不依赖
- "serial"   : 多个 Agent 按顺序处理，后一个需要前一个的结果

只返回 JSON：{{"mode": "single|parallel|serial", "reasoning": "..."}}"""

_VALIDATE_STEP_SYSTEM = """你是一个质量检验员。

当前串行任务链的上一步已完成：
  Agent：{agent_name}
  任务：{task_desc}
  结果：{result_preview}

下一步任务：{next_task_desc}

请判断上一步的结果是否足够完整，满足下一步任务的输入要求。

只返回 JSON：{{"can_proceed": true, "issue": "", "suggestion": ""}}"""

_ROUTE_AND_DECOMPOSE_SYSTEM = """\
你是多智能体系统的路由器。一次性完成两件事：选出最合适的 Agent，并将用户请求拆解为子任务。

可用 Agent（格式：name | role | 职责描述摘要）：
{agents_info}

规则：
- 优先选职责最匹配的 Agent，无法判断时选 general_assistant
- 若任务简单无需拆分，tasks 只含一个元素
- agent 字段必须来自上面的 name 列表

只返回 JSON，格式如下：
{{
  "agent": "<agent_name>",
  "tasks": [
    {{"task_id": "task_1", "type": "<agent_name>", "description": "清晰的任务描述（中文）"}}
  ]
}}\
"""

_IDENTIFY_AND_DECOMPOSE_SYSTEM = """\
你是任务路由与分解器，一次性完成意图识别和任务拆解。

可用意图（按所属智能体分组）：
{intent_groups}

规则：
- 识别最匹配的意图，无法判断时用 general_question
- 若任务简单无需拆分，tasks 只含一个元素

只返回 JSON，格式如下：
{{
  "intent": "<intent_name>",
  "tasks": [
    {{"task_id": "task_1", "type": "<intent_name>", "description": "清晰的任务描述（中文）"}}
  ]
}}\
"""

_CAPABLE_MODEL_KEYWORDS = (
    "gpt-4", "gpt4", "claude-3", "claude3", "gemini-pro",
    "qwen-max", "deepseek", "mixtral-8x22b", "llama-3-70b",
)


class RouterAgent:
    """
    路由智能体。

    职责：
    1. 意图识别（LLM 驱动）
    2. 任务分解（LLM 驱动）
    3. 流水线规划（single / serial / parallel）
    4. 模型适配性检查（日志警告）
    5. 串行步骤结果校验
    """

    def __init__(
        self,
        name: str = "router",
        config: Dict[str, Any] = None,
        llm: Optional[BaseChatModel] = None,
        intent_agent_mapping: Dict[str, str] = None,
    ):
        self.name = name
        self.config = config or {}
        self.llm = llm
        self.intent_agent_mapping = intent_agent_mapping or {}
        self._intent_groups_text = self._build_intent_groups()
        logger.info(
            "RouterAgent 初始化: llm=%s intents=%d",
            "有" if llm else "无",
            len(self.intent_agent_mapping),
        )

    def set_llm(self, llm: BaseChatModel) -> None:
        self.llm = llm
        logger.info("RouterAgent LLM 已更新")

    def _build_intent_groups(self) -> str:
        groups: Dict[str, List[str]] = {}
        for intent, ag in self.intent_agent_mapping.items():
            groups.setdefault(ag, []).append(intent)
        lines = [
            f"  [{ag}]: {', '.join(intents)}"
            for ag, intents in groups.items()
        ]
        return "\n".join(lines) if lines else "  general_question"

    @staticmethod
    def _strip_fence(raw: str) -> str:
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        return raw

    def _active(self, llm: Optional[BaseChatModel]) -> Optional[BaseChatModel]:
        return llm or self.llm

    # ── 意图识别 ──────────────────────────────────────────────────

    async def identify_intent(
        self,
        user_input: str,
        context: Dict[str, Any],
        llm: Optional[BaseChatModel] = None,
    ) -> str:
        active = self._active(llm)
        if active is None:
            logger.warning("RouterAgent: LLM 未配置，意图识别回退为 general_question")
            return "general_question"
        messages = [
            SystemMessage(content=_IDENTIFY_SYSTEM.format(
                intent_groups=self._intent_groups_text)),
            HumanMessage(content=f"用户输入：{user_input}"),
        ]
        try:
            resp = await active.ainvoke(messages)
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            data = json.loads(raw)
            intent = str(data.get("intent", "general_question")).strip()
            if intent not in self.intent_agent_mapping:
                logger.warning("LLM 返回未知意图 '%s'，使用 general_question", intent)
                intent = "general_question"
            logger.debug("意图识别: %r → %s", user_input[:60], intent)
            return intent
        except Exception as e:
            logger.warning("意图识别失败: %s，回退 general_question", e)
            return "general_question"

    # ── 任务分解 ──────────────────────────────────────────────────

    async def decompose_task(
        self,
        intent: str,
        user_input: str,
        llm: Optional[BaseChatModel] = None,
    ) -> List[Dict[str, Any]]:
        default = [{"task_id": "task_1", "type": intent, "description": user_input}]
        active = self._active(llm)
        if active is None:
            return default
        messages = [
            SystemMessage(content=_DECOMPOSE_SYSTEM),
            HumanMessage(content=f"意图：{intent}\n用户请求：{user_input}"),
        ]
        try:
            resp = await active.ainvoke(messages)
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            tasks = json.loads(raw)
            if not isinstance(tasks, list) or not tasks:
                return default
            validated = [
                {
                    "task_id": t.get("task_id", f"task_{i+1}"),
                    "type": t.get("type", intent),
                    "description": t.get("description", user_input),
                }
                for i, t in enumerate(tasks)
            ]
            logger.debug("任务分解: intent=%s tasks=%d", intent, len(validated))
            return validated
        except Exception as e:
            logger.warning("任务分解失败: %s，使用单任务", e)
            return default

    # ── Agent 选择 ────────────────────────────────────────────────

    async def decide_next_agent(self, intent: str) -> str:
        ag = self.intent_agent_mapping.get(intent)
        if ag:
            logger.debug("路由决策: intent=%s → agent=%s", intent, ag)
            return ag
        all_agents = set(self.intent_agent_mapping.values())
        if intent in all_agents:
            return intent
        logger.debug("路由决策: 未匹配意图 '%s'，由 router 自处理", intent)
        return self.name

    # ── 流水线模式规划 ────────────────────────────────────────────

    async def _plan_mode(
        self,
        intent: str,
        tasks_with_agents: List[Dict[str, Any]],
        llm: Optional[BaseChatModel] = None,
    ) -> str:
        agent_names = {t["agent_name"] for t in tasks_with_agents}
        if len(agent_names) <= 1:
            return "single"

        active = self._active(llm)
        if active is None:
            return "serial"

        from app.agents.registry import registry
        agents_info = "\n".join(
            f"  [{n}]: {(registry.get(n).role if registry.get(n) else '未知角色')}"
            for n in agent_names
        )
        tasks_text = "\n".join(
            f"  step {i}: [{t['agent_name']}] {t['description']}"
            for i, t in enumerate(tasks_with_agents)
        )
        task_agent_map = "\n".join(
            f"  {t['task_id']} → {t['agent_name']}"
            for t in tasks_with_agents
        )
        messages = [
            SystemMessage(content=_PLAN_MODE_SYSTEM.format(
                intent=intent,
                tasks_text=tasks_text,
                task_agent_map=task_agent_map,
                agents_info=agents_info,
            )),
        ]
        try:
            resp = await active.ainvoke(messages)
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            data = json.loads(raw)
            mode = str(data.get("mode", "serial")).strip()
            if mode not in ("single", "serial", "parallel"):
                mode = "serial"
            logger.debug(
                "流水线规划: intent=%s agents=%s → mode=%s",
                intent, list(agent_names), mode,
            )
            return mode
        except Exception as e:
            logger.warning("流水线规划失败: %s，默认串行", e)
            return "serial"

    # ── 模型适配性检查 ────────────────────────────────────────────

    def check_model_suitability(self, agent_name: str, model_name: str) -> bool:
        """
        检查当前模型是否适合目标 Agent。
        不满足时仅记录警告，不阻断执行。
        """
        from app.agents.registry import registry
        ag = registry.get(agent_name)
        if ag is None:
            return True
        code_tools = {"code_generation", "code_review", "syntax_check", "bug_fix"}
        if set(ag.tools) & code_tools:
            capable = any(kw in model_name.lower() for kw in _CAPABLE_MODEL_KEYWORDS)
            if not capable:
                logger.warning(
                    "模型适配警告: agent=%s 包含代码工具，建议使用更强的模型（当前 model=%s）",
                    agent_name, model_name,
                )
                return False
        return True

    # ── 串行步骤结果校验 ──────────────────────────────────────────

    async def validate_step_result(
        self,
        agent_name: str,
        task_desc: str,
        result: str,
        next_task_desc: str,
        llm: Optional[BaseChatModel] = None,
    ) -> Dict[str, Any]:
        """
        校验串行流水线中某步骤的输出是否满足下一步输入要求。

        Returns:
            {"can_proceed": bool, "issue": str, "suggestion": str}
        """
        default = {"can_proceed": True, "issue": "", "suggestion": ""}
        active = self._active(llm)
        if active is None:
            return default
        messages = [
            SystemMessage(content=_VALIDATE_STEP_SYSTEM.format(
                agent_name=agent_name,
                task_desc=task_desc,
                result_preview=result[:500],
                next_task_desc=next_task_desc,
            )),
        ]
        try:
            resp = await active.ainvoke(messages)
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            data = json.loads(raw)
            logger.debug(
                "串行校验: agent=%s can_proceed=%s issue=%s",
                agent_name, data.get("can_proceed"), data.get("issue", ""),
            )
            return {
                "can_proceed": bool(data.get("can_proceed", True)),
                "issue": str(data.get("issue", "")),
                "suggestion": str(data.get("suggestion", "")),
            }
        except Exception as e:
            logger.warning("串行结果校验失败: %s，默认放行", e)
            return default

    # ── 并行结果完整性校验 ────────────────────────────────────────

    async def validate_parallel_completeness(
        self,
        intent: str,
        results: List[Dict[str, Any]],
        llm: Optional[BaseChatModel] = None,
    ) -> bool:
        """并行多 Agent 结果合并后是否完整。"""
        active = self._active(llm)
        if active is None:
            return True
        results_text = "\n".join(
            f"  [{r.get('agent', '?')}] {str(r.get('result', ''))[:200]}"
            for r in results
        )
        prompt = (
            f"原始意图：{intent}\n\n各 Agent 返回的结果：\n{results_text}\n\n"
            "请判断这些结果合并后是否能完整地回应原始意图。\n"
            '只返回 JSON：{"complete": true, "missing": ""}'
        )
        try:
            resp = await active.ainvoke([HumanMessage(content=prompt)])
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            data = json.loads(raw)
            complete = bool(data.get("complete", True))
            if not complete:
                logger.warning(
                    "并行结果不完整: intent=%s missing=%s",
                    intent, data.get("missing", ""),
                )
            return complete
        except Exception as e:
            logger.warning("并行完整性校验失败: %s，默认通过", e)
            return True

    # ── 合并路由（一次 LLM 调用完成选 agent + 任务分解）─────────

    async def _route_dynamic(
        self,
        user_input: str,
        llm: Optional[BaseChatModel],
    ) -> tuple:
        """动态模式：1 次 LLM 调用同时完成 agent 选择 + 任务分解。"""
        from app.agents.registry import registry
        candidates = [a for a in registry.list_all() if a.name != self.name]
        default_agent = "general_assistant"
        default_tasks = [{"task_id": "task_1", "type": default_agent, "description": user_input}]

        active = self._active(llm)
        if active is None or not candidates:
            return default_agent, default_tasks

        agents_info = "\n".join(
            f"  {a.name} | {a.role} | {(a.background or '')[:80].replace(chr(10), ' ')}"
            for a in candidates
        )
        messages = [
            SystemMessage(content=_ROUTE_AND_DECOMPOSE_SYSTEM.format(agents_info=agents_info)),
            HumanMessage(content=f"用户请求：{user_input}"),
        ]
        try:
            resp = await active.ainvoke(messages)
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            data = json.loads(raw)

            chosen = str(data.get("agent", default_agent)).strip()
            if registry.get(chosen) is None:
                logger.warning("动态路由: LLM 选 '%s' 不存在，回退 %s", chosen, default_agent)
                chosen = default_agent

            raw_tasks = data.get("tasks", [])
            tasks = (
                [
                    {
                        "task_id": t.get("task_id", f"task_{i+1}"),
                        "type": t.get("type", chosen),
                        "description": t.get("description", user_input),
                    }
                    for i, t in enumerate(raw_tasks)
                ]
                if isinstance(raw_tasks, list) and raw_tasks
                else default_tasks
            )
            logger.debug("动态路由+分解: '%s' → agent=%s tasks=%d", user_input[:60], chosen, len(tasks))
            return chosen, tasks
        except Exception as e:
            logger.warning("动态路由+分解失败: %s，使用默认", e)
            return default_agent, default_tasks

    async def _route_static(
        self,
        user_input: str,
        context: Dict[str, Any],
        llm: Optional[BaseChatModel],
    ) -> tuple:
        """静态模式：1 次 LLM 调用同时完成意图识别 + 任务分解。"""
        default_intent = "general_question"
        default_tasks = [{"task_id": "task_1", "type": default_intent, "description": user_input}]

        active = self._active(llm)
        if active is None:
            return default_intent, default_tasks

        messages = [
            SystemMessage(content=_IDENTIFY_AND_DECOMPOSE_SYSTEM.format(
                intent_groups=self._intent_groups_text
            )),
            HumanMessage(content=f"用户请求：{user_input}"),
        ]
        try:
            resp = await active.ainvoke(messages)
            raw = self._strip_fence(getattr(resp, "content", str(resp)).strip())
            data = json.loads(raw)

            intent = str(data.get("intent", default_intent)).strip()
            if intent not in self.intent_agent_mapping:
                logger.warning("LLM 返回未知意图 '%s'，使用 %s", intent, default_intent)
                intent = default_intent

            raw_tasks = data.get("tasks", [])
            tasks = (
                [
                    {
                        "task_id": t.get("task_id", f"task_{i+1}"),
                        "type": t.get("type", intent),
                        "description": t.get("description", user_input),
                    }
                    for i, t in enumerate(raw_tasks)
                ]
                if isinstance(raw_tasks, list) and raw_tasks
                else default_tasks
            )
            logger.debug("静态路由+分解: '%s' → intent=%s tasks=%d", user_input[:60], intent, len(tasks))
            return intent, tasks
        except Exception as e:
            logger.warning("静态路由+分解失败: %s，使用默认", e)
            return default_intent, default_tasks

    # ── 主入口 ───────────────────────────────────────────────────

    async def process(
        self,
        user_input: str,
        context: Dict[str, Any],
        llm: Optional[BaseChatModel] = None,
    ) -> Dict[str, Any]:
        """
        路由主流程（1 次 LLM 调用完成路由+分解）。

        静态模式（intent_agent_mapping 非空）：意图识别 + 任务分解合并为 1 次调用。
        动态模式（intent_agent_mapping 为空）：agent 选择 + 任务分解合并为 1 次调用。

        总 LLM 调用次数：1（路由） + 1（执行） = 2 次。

        Returns::

            {
                "intent":       str,
                "mode":         "single" | "serial" | "parallel",
                "pipeline":     [{"step": int, "agent_name": str, "task": dict}],
                "target_agent": str,
                "tasks":        list,
            }
        """
        if self.intent_agent_mapping:
            # ── 静态模式：1 次调用完成意图识别 + 分解 ──
            intent, tasks = await self._route_static(user_input, context, llm=llm)
            tasks_with_agents: List[Dict[str, Any]] = []
            for task in tasks:
                ag = await self.decide_next_agent(task.get("type", intent))
                tasks_with_agents.append({**task, "agent_name": ag})
        else:
            # ── 动态模式：1 次调用完成 agent 选择 + 分解 ──
            chosen, tasks = await self._route_dynamic(user_input, llm=llm)
            intent = chosen
            tasks_with_agents = [{**t, "agent_name": chosen} for t in tasks]

        mode = await self._plan_mode(intent, tasks_with_agents, llm=llm)

        pipeline = [
            {"step": i, "agent_name": t["agent_name"], "task": t}
            for i, t in enumerate(tasks_with_agents)
        ]
        target_agent = pipeline[0]["agent_name"] if pipeline else self.name

        logger.info(
            "路由完成: intent=%s mode=%s steps=%d agents=%s",
            intent, mode, len(pipeline),
            [p["agent_name"] for p in pipeline],
        )
        return {
            "intent": intent,
            "mode": mode,
            "pipeline": pipeline,
            "target_agent": target_agent,
            "tasks": tasks,
        }
