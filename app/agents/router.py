"""
【模块说明】路由智能体（RouterAgent）— 决定"谁来干这件事"的调度大脑

当用户发来一条消息时，RouterAgent 负责分析这条消息，
制定整个多 Agent 执行计划。它依次完成三步工作：

第一步：意图识别（Identify）
  读取用户消息，判断用户想干什么（如：数据查询、代码生成、普通聊天等），
  结果是一个意图标签，如 "data_analysis" 或 "general_question"。

第二步：任务分解（Decompose）
  把用户的请求拆成具体可执行的子任务列表。
  简单请求只有一个任务，复杂请求可能有多个。

第三步：流水线规划（Plan）
  根据任务和可用 Agent，决定执行模式：
  - single（单 Agent）：一个 Agent 处理所有事
  - parallel（并行）：多个 Agent 同时处理，结果互不依赖
  - serial（串行）：多个 Agent 按顺序处理，后一个要用前一个的结果

完成规划后，HermesEngine 按照这个计划驱动各 Agent 逐步执行。
"""


import json
import logging
import uuid
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

_JUDGE_OVERALL_SYSTEM = """\
你是质量审核员，负责评估多智能体执行结果是否满足用户期望。

用户原始请求：{user_input}

各 Agent 执行结果摘要：
{results_text}

判断这些结果是否完整、准确地满足了用户的原始请求。
只返回 JSON：{{"satisfied": true, "issue": "", "suggestion": ""}}\
"""

_REPLAN_AGENTS_SYSTEM = """\
你是多智能体系统的重新规划器。

用户原始请求：{user_input}
上次执行问题：{issue}
改进建议：{suggestion}

各 Agent 上次执行结果：
{results_text}

可用 Agent（格式：name | role | 职责描述）：
{agents_info}

请重新规划 Agent 任务分工以解决上述问题。只返回 JSON 数组：
[{{"step": 0, "agent_name": "<name>", "task": {{"task_id": "task_1", "type": "<name>", "description": "清晰的任务描述（中文）"}}}}]\
"""

_ROUTE_AND_DECOMPOSE_SYSTEM = """\
你是多智能体系统的路由器和任务规划器。根据用户请求，将任务拆解为若干步骤，并为每步选择最合适的 Agent。

可用 Agent（格式：name | role | 职责描述摘要）：
{agents_info}

任务拆分原则：
1. 单一职责：若任务可由单个 Agent 独立完成，只输出一条任务
2. 多 Agent 协作：若任务包含"获取信息 + 处理信息"的组合，应拆分给不同 Agent
   典型模式：
   - 访问/抓取网页内容 → web_agent
   - 总结文本 / 生成报告 / 保存文件到本地 → summarizer
   - 数据查询与统计分析 → data_analyst
   - 编写/调试代码 → code_assistant
3. 任务描述必须包含用户提供的所有具体信息（URL、文件路径、数值等），不得省略
4. 后续步骤在 description 中可引用"上一步提取的内容"或"前序步骤的输出结果"
5. agent_name 必须来自上方 name 列表，无法判断时选 general_assistant

只返回 JSON 数组（不含其他文字）：
[
  {{"step": 0, "agent_name": "<name>", "task": {{"task_id": "task_1", "type": "<name>", "description": "清晰的任务描述，含所有具体参数"}}}},
  {{"step": 1, "agent_name": "<name>", "task": {{"task_id": "task_2", "type": "<name>", "description": "基于上一步的输出，..."}}}}
]\
"""

_IDENTIFY_AND_DECOMPOSE_SYSTEM = """\
你是任务路由与分解器，一次性完成意图识别和任务拆解。

可用意图（按所属智能体分组）：
{intent_groups}

规则：
- 识别最匹配的意图，无法判断时用 general_question
- 若任务简单无需拆分，tasks 只含一个元素
- description 必须保留用户请求中的所有具体信息（URL、文件名、数值、关键词等），不得泛化或省略

只返回 JSON，格式如下：
{{
  "intent": "<intent_name>",
  "tasks": [
    {{"task_id": "task_1", "type": "<intent_name>", "description": "清晰的任务描述，含用户提供的所有具体参数"}}
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

    async def _invoke_json(
        self,
        messages: list,
        fallback: Any,
        llm: Optional[BaseChatModel] = None,
    ) -> Any:
        """ainvoke → _strip_fence → json.loads；失败时用正则从响应中提取 JSON，再失败则返回 fallback。"""
        active = self._active(llm)
        if active is None:
            return fallback
        resp = await active.ainvoke(messages)
        text = resp.content.strip()
        raw = self._strip_fence(text)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 正则兜底：提取第一个 JSON 对象或数组
        for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
            m = re.search(pattern, text)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    continue
        logger.warning("_invoke_json: 无法从 LLM 响应中提取 JSON，返回 fallback；响应片段: %r", text[:200])
        return fallback

    # ── 意图识别 ──────────────────────────────────────────────────

    async def identify_intent(
        self,
        user_input: str,
        context: Dict[str, Any],
        llm: Optional[BaseChatModel] = None,
    ) -> str:
        if self._active(llm) is None:
            logger.warning("RouterAgent: LLM 未配置，意图识别回退为 general_question")
            return "general_question"
        messages = [
            SystemMessage(content=_IDENTIFY_SYSTEM.format(
                intent_groups=self._intent_groups_text)),
            HumanMessage(content=f"用户输入：{user_input}"),
        ]
        try:
            data = await self._invoke_json(messages, {}, llm)
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
            tasks = await self._invoke_json(messages, [], llm)
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
            data = await self._invoke_json(messages, {}, llm)
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
        messages = [
            SystemMessage(content=_VALIDATE_STEP_SYSTEM.format(
                agent_name=agent_name,
                task_desc=task_desc,
                result_preview=result[:500],
                next_task_desc=next_task_desc,
            )),
        ]
        try:
            data = await self._invoke_json(messages, {}, llm)
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
            data = await self._invoke_json([HumanMessage(content=prompt)], {}, llm)
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
    ) -> List[Dict[str, Any]]:
        """动态模式：1 次 LLM 调用完成多 Agent 路由 + 任务拆分，返回 pipeline 列表。"""
        from app.agents.registry import registry
        default_agent = "general_assistant"
        default_pipeline = [{"step": 0, "agent_name": default_agent, "task": {"task_id": "task_1", "type": default_agent, "description": user_input}}]

        active = self._active(llm)
        candidates = [a for a in registry.list_all() if a.name != self.name]
        if active is None or not candidates:
            return default_pipeline

        agents_info = "\n".join(
            f"  {a.name} | {a.role} | {(a.background or '')[:80].replace(chr(10), ' ')}"
            for a in candidates
        )
        messages = [
            SystemMessage(content=_ROUTE_AND_DECOMPOSE_SYSTEM.format(agents_info=agents_info)),
            HumanMessage(content=f"用户请求：{user_input}"),
        ]
        try:
            data = await self._invoke_json(messages, [], llm)
            if not isinstance(data, list) or not data:
                return default_pipeline

            pipeline: List[Dict[str, Any]] = []
            for i, item in enumerate(data):
                ag = str(item.get("agent_name", default_agent)).strip()
                if registry.get(ag) is None:
                    logger.warning("动态路由: LLM 选 '%s' 不存在，回退 %s", ag, default_agent)
                    ag = default_agent
                task_raw = item.get("task") or {}
                pipeline.append({
                    "step": i,
                    "agent_name": ag,
                    "task": {
                        "task_id": task_raw.get("task_id", f"task_{i+1}"),
                        "type": task_raw.get("type", ag),
                        "description": task_raw.get("description", user_input),
                    },
                })

            logger.debug(
                "多Agent路由: '%s' → steps=%d agents=%s",
                user_input[:60], len(pipeline), [p["agent_name"] for p in pipeline],
            )
            return pipeline
        except Exception as e:
            logger.warning("多Agent路由失败: %s，使用默认", e)
            return default_pipeline

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
            data = await self._invoke_json(messages, {}, llm)
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
            # ── 动态模式：1 次调用完成多 Agent 路由 + 分解 ──
            dyn_pipeline = await self._route_dynamic(user_input, llm=llm)
            intent = dyn_pipeline[0]["agent_name"] if dyn_pipeline else "general_assistant"
            tasks = [p["task"] for p in dyn_pipeline]
            tasks_with_agents = [{"agent_name": p["agent_name"], **p["task"]} for p in dyn_pipeline]

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

        # ── L1 任务写入（后台异步，不阻塞路由响应）──────────────────────────
        turn_id = context.get("turn_id") if isinstance(context, dict) else None
        if turn_id and len(pipeline) > 1:
            # 多 Agent 流水线才记录 L1 任务（单 agent 场景跳过以节省开销）
            import asyncio as _asyncio
            user_id = context.get("user_id", "") if isinstance(context, dict) else ""
            if user_id:
                _asyncio.ensure_future(
                    self._write_l1_tasks(user_id, turn_id, tasks_with_agents, llm)
                )

        return {
            "intent": intent,
            "mode": mode,
            "pipeline": pipeline,
            "target_agent": target_agent,
            "tasks": tasks,
            "turn_id": turn_id or "",
        }

    async def judge_overall_result(
        self,
        user_input: str,
        pipeline_results: List[Dict[str, Any]],
        llm: Optional[BaseChatModel] = None,
    ) -> Dict[str, Any]:
        """判断所有 Agent 执行结果是否满足用户的原始请求。

        Returns:
            {"satisfied": bool, "issue": str, "suggestion": str}
        """
        default = {"satisfied": True, "issue": "", "suggestion": ""}
        results_text = "\n".join(
            f"  [{r.get('agent', '?')}]: {str(r.get('result', ''))[:300]}"
            for r in pipeline_results
        )
        messages = [
            SystemMessage(content=_JUDGE_OVERALL_SYSTEM.format(
                user_input=user_input,
                results_text=results_text,
            )),
        ]
        try:
            data = await self._invoke_json(messages, {}, llm)
            satisfied = bool(data.get("satisfied", True))
            logger.debug(
                "整体结果判断: satisfied=%s issue=%s",
                satisfied, data.get("issue", ""),
            )
            return {
                "satisfied": satisfied,
                "issue": str(data.get("issue", "")),
                "suggestion": str(data.get("suggestion", "")),
            }
        except Exception as e:
            logger.warning("整体结果判断失败: %s，默认通过", e)
            return default

    async def replan_agents(
        self,
        user_input: str,
        issue: str,
        suggestion: str,
        prev_results: List[Dict[str, Any]],
        llm: Optional[BaseChatModel] = None,
    ) -> List[Dict[str, Any]]:
        """根据问题重新规划 Agent 任务分工，返回新 pipeline 列表。

        Returns:
            新 pipeline 列表，格式同 process() 返回的 pipeline 字段；失败时返回空列表。
        """
        from app.agents.registry import registry
        candidates = [a for a in registry.list_all() if a.name != self.name]
        agents_info = "\n".join(
            f"  {a.name} | {a.role} | {(a.background or '')[:60].replace(chr(10), ' ')}"
            for a in candidates
        ) or "  general_assistant | 通用助手 | 处理各类任务"

        results_text = "\n".join(
            f"  [{r.get('agent', '?')}]: {str(r.get('result', ''))[:200]}"
            for r in prev_results
        )
        messages = [
            SystemMessage(content=_REPLAN_AGENTS_SYSTEM.format(
                user_input=user_input,
                issue=issue,
                suggestion=suggestion,
                results_text=results_text,
                agents_info=agents_info,
            )),
        ]
        try:
            data = await self._invoke_json(messages, [], llm)
            if not isinstance(data, list) or not data:
                return []
            pipeline = []
            for i, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                ag = str(item.get("agent_name", "general_assistant")).strip()
                if registry.get(ag) is None:
                    logger.warning("重新规划: LLM 选 '%s' 不存在，回退 general_assistant", ag)
                    ag = "general_assistant"
                raw_task = item.get("task", {})
                if not isinstance(raw_task, dict):
                    raw_task = {}
                task = {
                    "task_id": raw_task.get("task_id", f"task_{i+1}"),
                    "type": raw_task.get("type", ag),
                    "description": raw_task.get("description", user_input),
                }
                pipeline.append({"step": i, "agent_name": ag, "task": task})
            logger.info("重新规划完成: %d 个 Agent tasks", len(pipeline))
            return pipeline
        except Exception as e:
            logger.warning("重新规划失败: %s", e)
            return []

    async def _write_l1_tasks(
        self,
        user_id:           str,
        turn_id:           str,
        tasks_with_agents: List[Dict[str, Any]],
        llm:               Optional[BaseChatModel],
    ) -> None:
        """将 Router 分解的 Agent 级任务写入 L1 TaskStore（后台任务）。"""
        try:
            from app.core.task_planner import make_l1_store
            store, _ = make_l1_store(user_id, turn_id)
            raw = [
                {
                    "content":    t.get("description", ""),
                    "tags":       [f"agent:{t.get('agent_name', '')}"],
                    "agent_name": t.get("agent_name", ""),
                }
                for t in tasks_with_agents
            ]
            await store.replace(raw)
            logger.debug(
                "[RouterAgent] L1 任务已写入 user=%s turn=%s tasks=%d",
                user_id, turn_id, len(raw),
            )
        except Exception as e:
            logger.warning("[RouterAgent] L1 任务写入失败: %s", e)
