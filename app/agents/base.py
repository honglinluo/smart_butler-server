"""Agent 基础类 - 所有 Agent 的公共接口与技能记忆实现"""

import asyncio
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool as LCBaseTool

from app.database.pool import get_connection, release_connection
from app.core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

_AGENT_TEMPLATE_DIR = PROJECT_ROOT / "config" / "templates"

try:
    from langgraph.prebuilt import create_react_agent as _create_react_agent
    _HAS_LANGGRAPH = True
except ImportError:
    _create_react_agent = None  # type: ignore
    _HAS_LANGGRAPH = False


# ── Registry 工具 → LangChain Tool 适配器 ─────────────────────────────────────

class _RegistryToolAdapter(LCBaseTool):
    """将 app.tools.base.BaseTool 适配为 LangChain Tool，供 ReAct agent 使用。"""

    def __init__(self, registry_tool: Any, user_id: str, agent_name: str = ""):
        super().__init__()
        self.name        = registry_tool.name
        self.description = (registry_tool.description or f"Tool: {registry_tool.name}")
        self._rt         = registry_tool
        self._user_id    = user_id
        self._agent_name = agent_name

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("_RegistryToolAdapter 必须在异步上下文中通过 _arun 调用")

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        from app.utils.log_bus import get_bus
        bus = get_bus()

        # LangGraph 以 kwargs 传入；无 schema 时 LangChain 可能以单字符串传入
        if args and isinstance(args[0], str) and not kwargs:
            try:
                params = json.loads(args[0])
            except Exception:
                params = {"input": args[0]}
        else:
            params = dict(kwargs)

        context = {"user_id": self._user_id, "agent_name": self._agent_name}
        bus.tool_call(self._user_id, self._agent_name, self.name, params)
        t0 = time.monotonic()
        try:
            result = await self._rt.execute(params, context)
            result_str = (
                json.dumps(result, ensure_ascii=False)
                if isinstance(result, dict)
                else str(result)
            )
            bus.tool_result(
                self._user_id, self._agent_name, self.name,
                result_str, (time.monotonic() - t0) * 1000,
            )
            return result_str
        except Exception as e:
            error_msg = f"工具 {self.name} 执行失败: {e}"
            bus.tool_error(self._user_id, self._agent_name, self.name, str(e))
            # 将错误作为 ToolMessage 内容反馈给 LLM，不静默忽略
            return error_msg


@dataclass
class AgentSkill:
    """Agent 技能条目 - 记录某类任务的成功工作模式。"""
    skill_id:     str
    description:  str    # 技能适用场景
    pattern:      str    # 成功的工作模式（注入提示词）
    success_rate: float = 1.0
    usage_count:  int   = 0
    last_updated: str   = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BaseAgent:
    """
    Agent 基础类。

    代码方式（继承）::

        class DataAnalystAgent(BaseAgent):
            name       = "data_analyst"
            role       = "数据分析工程师"
            background = "你是一个全能的数据分析师..."
            tools      = ["sql_query"]

            async def execute(self, task, context, llm):
                ...

    装饰器方式::

        @agent(name="data_analyst", role="数据分析工程师", background="...")
        class DataAnalystAgent(BaseAgent):
            async def execute(self, task, context, llm):
                ...

    DB 方式（API 创建）：无需代码，系统自动使用默认 execute() 实现。

    调用计数：子类 execute() 结束后（无论成功或异常）自动触发 _record_call()，
    无需在子类中显式调用。
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """在子类定义 execute() 时自动包装，使其结束后执行 _record_call()。"""
        super().__init_subclass__(**kwargs)
        if "execute" in cls.__dict__:
            _orig = cls.__dict__["execute"]

            async def _auto_execute(
                self: "BaseAgent",
                task: Dict[str, Any],
                context: Dict[str, Any],
                llm: Any,
                _f: Any = _orig,
            ) -> Dict[str, Any]:
                try:
                    from app.skills.loader import load_skills_text
                    file_skills = await load_skills_text(self.name)
                    if isinstance(context, dict) and file_skills:
                        context["_file_skills"] = file_skills
                except Exception:
                    pass
                try:
                    return await _f(self, task, context, llm)
                finally:
                    self._record_call()

            cls.execute = _auto_execute

    # 子类通过类变量声明元信息 ─────────────────────────────────
    name:       ClassVar[str]       = ""
    role:       ClassVar[str]       = ""
    background: ClassVar[str]       = ""
    tools:      ClassVar[List[str]] = []
    is_public:  ClassVar[bool]      = True
    source:     ClassVar[str]       = "code"  # "code" | "db"

    def __init__(
        self,
        name:       Optional[str]       = None,
        role:       Optional[str]       = None,
        background: Optional[str]       = None,
        tools:      Optional[List[str]] = None,
        is_public:  Optional[bool]      = None,
        source:     Optional[str]       = None,
        user_id:    str                 = "0",
        db_id:      Optional[int]       = None,
        max_skills: int                 = 10,
    ):
        self.name       = name       if name       is not None else (self.__class__.name or self.__class__.__name__)
        self.role       = role       if role       is not None else self.__class__.role
        self.background = background if background is not None else self.__class__.background
        self.tools      = tools      if tools      is not None else list(self.__class__.tools)
        self.is_public  = is_public  if is_public  is not None else self.__class__.is_public
        self.source     = source     if source     is not None else self.__class__.source
        self.user_id    = user_id
        self.db_id      = db_id
        self.max_skills = max_skills

        self._skills: List[AgentSkill] = []
        self._skills_loaded: bool      = False

        # 仅对 code 类型的系统 agent 做模板查找（db agent 背景来自数据库）
        if self.source == "code":
            self._load_background_from_template()

    # ── 工具收集与调用 ────────────────────────────────────────────────

    def collect_tools(self, user_id: str = "") -> List[LCBaseTool]:
        """收集当前 agent 可用的 LangChain 工具列表。

        规则：
          - visibility=public  + exec_location=server：所有 agent 均可用的通用工具
          - visibility=exclusive + owner_agent==self.name：本 agent 专属工具
          - exec_location=client 的工具需客户端执行，不纳入（LLM 无法直接调用）

        Returns:
            适配为 LangChain Tool 的工具列表，可直接传给 bind_tools / create_react_agent。
        """
        from app.tools.registry import registry as _tool_registry
        from app.tools.base import EXEC_SERVER

        uid      = user_id or self.user_id
        available = _tool_registry.list_available_for(user_id=uid, agent_name=self.name)
        lc_tools: List[LCBaseTool] = []
        for t in available:
            if t.exec_location != EXEC_SERVER:
                continue
            lc_tools.append(_RegistryToolAdapter(t, user_id=uid, agent_name=self.name))

        if lc_tools:
            logger.debug(
                "[BaseAgent] %s 收集工具 %d 个: %s",
                self.name, len(lc_tools), [t.name for t in lc_tools],
            )
        return lc_tools

    async def call_tool(
        self,
        tool_name: str,
        params:    Dict[str, Any],
        context:   Dict[str, Any],
    ) -> Any:
        """按名称直接调用指定工具。

        Args:
            tool_name: registry 中注册的工具名称
            params:    工具参数 dict
            context:   执行上下文（含 user_id 等）

        Returns:
            工具 execute() 的返回值（通常为 dict）

        Raises:
            ValueError: 工具不存在于 registry 时
            Exception:  工具执行过程中的异常（透传给调用方处理）
        """
        from app.tools.registry import registry as _tool_registry

        tool = _tool_registry.get(tool_name)
        if tool is None:
            raise ValueError(f"工具 '{tool_name}' 不存在于 registry")

        ctx = dict(context)
        ctx.setdefault("user_id", self.user_id)
        ctx.setdefault("agent_name", self.name)
        return await tool.execute(params, ctx)

    # ── 核心执行（DB Agent 直接使用此实现；代码 Agent 可重写）───────

    # L2 拆分阈值：任务描述超过此字数时触发 L2 拆分（可通过子类覆盖）
    _L2_DECOMPOSE_THRESHOLD: int = 80

    async def execute(
        self,
        task:    Dict[str, Any],
        context: Dict[str, Any],
        llm,
    ) -> Dict[str, Any]:
        """使用 LLM 执行任务（默认实现）。

        流程：
          1. 加载技能记忆，构建 system prompt
          2. 收集可用工具（公共 + 专属）
          3. 若任务复杂（描述超过阈值），先做 L2 拆分，再逐步执行
          4. 有工具且 LangGraph 可用 → ReAct agent；否则 bind_tools / 纯 LLM

        Returns:
            {"result": str, "success": bool, "metadata": {...}}
        """
        if llm is None:
            return {"result": "LLM 未配置，无法执行任务", "success": False, "metadata": {}}

        try:
            try:
                from app.skills.loader import load_skills_text
                file_skills = await load_skills_text(self.name)
                if isinstance(context, dict) and file_skills:
                    context["_file_skills"] = file_skills
            except Exception:
                pass
            await self.load_skills()
            system_prompt = self._build_system_prompt(context=context)
            task_desc     = task.get("description", "")
            user_id       = context.get("user_id", self.user_id) if isinstance(context, dict) else self.user_id
            lc_tools      = self.collect_tools(user_id)
            extra_ctx     = self._build_context_messages(context)

            # 注入历史上下文（最近 5 轮）
            history_lines = []
            for turn in (context.get("history", []) if isinstance(context, dict) else [])[-5:]:
                if isinstance(turn, dict):
                    u = turn.get("user_input", turn.get("human", ""))
                    a = turn.get("assistant_response", turn.get("ai", ""))
                    if u:
                        history_lines.append(f"用户: {u}")
                    if a:
                        history_lines.append(f"助手: {a}")

            # ── L2 拆分：任务复杂时先分解再按步执行 ─────────────────────────
            t0 = time.monotonic()
            if len(task_desc) >= self._L2_DECOMPOSE_THRESHOLD:
                result_text, l2_meta = await self._execute_with_l2(
                    task_desc, user_id, lc_tools, system_prompt, history_lines, llm, context,
                    extra_messages=extra_ctx,
                )
            else:
                human_content = task_desc
                if history_lines:
                    human_content = "历史对话:\n" + "\n".join(history_lines) + "\n\n当前任务:\n" + task_desc
                result_text = await self._invoke_with_tools(
                    llm, system_prompt, human_content, lc_tools, user_id=user_id,
                    extra_messages=extra_ctx,
                )
                l2_meta = {}

            from app.utils.log_bus import get_bus
            get_bus().llm_output(
                user_id, self.name, result_text, (time.monotonic() - t0) * 1000,
            )
            await self.update_skill(task_desc[:50], result_text, success=True)
            logger.debug("Agent 执行完成: name=%s tools=%d task_len=%d",
                         self.name, len(lc_tools), len(task_desc))
            return {
                "result":   result_text,
                "success":  True,
                "metadata": {"agent": self.name, "tools_used": len(lc_tools), **l2_meta},
            }

        except Exception as e:
            logger.error("Agent 执行异常: name=%s error=%s", self.name, e)
            return {"result": f"执行出错: {e}", "success": False, "metadata": {}}

        finally:
            self._record_call()

    async def _execute_with_l2(
        self,
        task_desc:      str,
        user_id:        str,
        lc_tools:       List[LCBaseTool],
        system_prompt:  str,
        history_lines:  List[str],
        llm:            Any,
        context:        Dict[str, Any],
        extra_messages: Optional[list] = None,
    ) -> tuple:
        """L2 拆分后逐步执行，返回 (最终结果文字, l2 元数据字典)。

        执行策略
        ────────
        - 按 priority 顺序执行步骤
        - blocked 步骤等待前置完成后自动解锁
        - on_fail=terminate 的步骤失败时立即终止并返回错误
        - on_fail=skip 的步骤失败时跳过并继续
        - on_fail=retry:N 的步骤失败时最多重试 N 次
        """
        from app.core.task_planner import make_l2_store, TaskStatus
        import uuid as _uuid

        exec_id = _uuid.uuid4().hex[:8]
        store, decomposer = make_l2_store(user_id, self.name, exec_id)
        store._cache = []   # 绕过 Redis（无连接时降级为内存）

        # 工具名称摘要，供 L2 提示词参考粒度
        tools_info = ", ".join(t.name for t in lc_tools) if lc_tools else "（无专用工具）"

        steps = await decomposer.decompose(task_desc, llm, tools_info=tools_info)
        if not steps:
            # 拆分失败，降级为整体执行
            logger.warning("[L2] 步骤拆分失败，降级整体执行 agent=%s", self.name)
            human_content = task_desc
            if history_lines:
                human_content = "历史对话:\n" + "\n".join(history_lines) + "\n\n当前任务:\n" + task_desc
            result = await self._invoke_with_tools(
                llm, system_prompt, human_content, lc_tools, extra_messages=extra_messages,
            )
            return result, {"l2_steps": 0, "l2_fallback": True}

        logger.info("[L2] agent=%s 任务已拆分为 %d 步", self.name, len(steps))

        step_results: List[str] = []
        completed_ids: List[str] = []
        failed_ids:    List[str] = []

        from app.utils import progress_bus as _pb

        for step in steps:
            if step.status == TaskStatus.BLOCKED:
                # 依赖未完成，跳过（不应发生，_recompute_blocked 已处理）
                logger.debug("[L2] 步骤 %s 仍被阻塞，跳过", step.task_id)
                continue

            await store.set_status(step.task_id, TaskStatus.IN_PROGRESS)
            _pb.push("step_start", {
                "agent_name":  self.name,
                "step_id":     step.task_id,
                "description": step.content[:120],
            })

            # 构建单步输入
            prev_context = "\n".join(step_results[-3:])   # 最近 3 步结果作为上下文
            step_input = step.content
            if prev_context:
                step_input = f"前序执行结果：\n{prev_context}\n\n当前步骤：{step.content}"
            if history_lines:
                step_input = "历史对话:\n" + "\n".join(history_lines) + "\n\n" + step_input

            on_fail     = decomposer.get_on_fail(step)
            retry_count = decomposer.get_retry_count(step)
            attempts    = max(1, retry_count + 1)
            step_ok     = False
            step_result = ""

            for attempt in range(attempts):
                try:
                    step_result = await self._invoke_with_tools(
                        llm, system_prompt, step_input, lc_tools, user_id=user_id,
                        extra_messages=extra_messages,
                    )
                    step_ok = True
                    break
                except Exception as exc:
                    logger.warning(
                        "[L2] 步骤 %s 第 %d/%d 次执行失败 agent=%s: %s",
                        step.task_id, attempt + 1, attempts, self.name, exc,
                    )
                    if attempt + 1 == attempts:
                        step_result = f"[步骤失败: {exc}]"

            _pb.push("step_done", {
                "agent_name": self.name,
                "step_id":    step.task_id,
                "success":    step_ok,
                "result":     step_result[:150] if step_ok else "",
            })

            if step_ok:
                await store.set_status(step.task_id, TaskStatus.COMPLETED)
                completed_ids.append(step.task_id)
                step_results.append(f"步骤「{step.content[:40]}」结果：{step_result[:200]}")
            else:
                await store.set_status(step.task_id, TaskStatus.CANCELLED)
                failed_ids.append(step.task_id)

                if on_fail == "terminate" or decomposer.should_terminate(step):
                    logger.warning("[L2] 步骤 %s 失败且策略为 terminate，中止任务 agent=%s", step.task_id, self.name)
                    partial = "\n".join(step_results) if step_results else "（无已完成步骤）"
                    return (
                        f"任务执行中止（步骤「{step.content[:40]}」失败）。\n已完成部分：\n{partial}",
                        {"l2_steps": len(steps), "l2_completed": len(completed_ids), "l2_terminated": True},
                    )
                # skip：记录并继续
                step_results.append(f"步骤「{step.content[:40]}」已跳过（失败）")

        # 汇总所有步骤结果，交给 LLM 生成最终输出
        summary_input = (
            f"以下是任务「{task_desc[:100]}」各步骤的执行结果，请综合整理为完整的最终回答：\n\n"
            + "\n".join(step_results)
        )
        final_result = await self._invoke_with_tools(
            llm, system_prompt, summary_input, lc_tools, user_id=user_id,
            extra_messages=extra_messages,
        )

        return final_result, {
            "l2_steps":     len(steps),
            "l2_completed": len(completed_ids),
            "l2_failed":    len(failed_ids),
        }

    async def _invoke_with_tools(
        self,
        llm:            Any,
        system_prompt:  str,
        human_content:  str,
        lc_tools:       List[LCBaseTool],
        user_id:        str = "",
        extra_messages: Optional[list] = None,
    ) -> str:
        """统一的 LLM 调用入口，按工具可用情况选择最优执行策略。

        策略优先级：
          1. 有工具 + LangGraph 可用 → create_react_agent（完整 ReAct 循环）
          2. 有工具 + 无 LangGraph   → bind_tools 手动单轮工具调用
          3. 无工具                   → 直接 ainvoke（extra_messages 注入在此路径生效）
        """
        from app.utils.log_bus import get_bus
        bus = get_bus()
        uid = user_id or self.user_id
        bus.llm_input(
            uid, self.name, human_content, system_prompt,
            [t.name for t in lc_tools],
        )

        if not lc_tools:
            msgs: list = [SystemMessage(content=system_prompt)]
            if extra_messages:
                msgs.extend(extra_messages)
            msgs.append(HumanMessage(content=human_content))
            resp = await llm.ainvoke(msgs)
            return resp.content

        if _HAS_LANGGRAPH and _create_react_agent is not None:
            return await self._run_react_agent(llm, system_prompt, human_content, lc_tools, user_id=uid)

        return await self._run_bind_tools(llm, system_prompt, human_content, lc_tools, user_id=uid)

    async def _run_react_agent(
        self,
        llm:           Any,
        system_prompt: str,
        human_content: str,
        lc_tools:      List[LCBaseTool],
        user_id:       str = "",
    ) -> str:
        """使用 LangGraph create_react_agent 执行（支持多轮工具调用）。

        工具调用日志由 _RegistryToolAdapter._arun 自动发射，无需在此重复。
        """
        graph  = _create_react_agent(llm, lc_tools, state_modifier=system_prompt)
        output = await graph.ainvoke({"messages": [HumanMessage(content=human_content)]})
        msgs   = output.get("messages", [])
        final  = msgs[-1] if msgs else None
        return final.content if final else ""

    async def _run_bind_tools(
        self,
        llm:           Any,
        system_prompt: str,
        human_content: str,
        lc_tools:      List[LCBaseTool],
        user_id:       str = "",
    ) -> str:
        """bind_tools 手动单轮工具调用（LangGraph 不可用时的降级实现）。

        工具错误以 ToolMessage 形式反馈给 LLM，不静默忽略。
        _RegistryToolAdapter.arun 已处理工具级别的日志；
        此处额外记录非 RegistryToolAdapter 工具的错误。
        """
        from langchain_core.messages import ToolMessage
        from app.utils.log_bus import get_bus
        bus = get_bus()
        uid = user_id or self.user_id

        llm_with_tools = llm.bind_tools(lc_tools)
        tool_map       = {t.name: t for t in lc_tools}
        messages: list = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_content),
        ]

        # 最多执行 5 轮工具调用，防止无限循环
        for _ in range(5):
            resp       = await llm_with_tools.ainvoke(messages)
            tool_calls = getattr(resp, "tool_calls", None)
            if not tool_calls:
                break

            messages.append(resp)
            for tc in tool_calls:
                if isinstance(tc, dict):
                    call_id, name, args = tc.get("id", ""), tc.get("name", ""), tc.get("args", {})
                else:
                    call_id = getattr(tc, "id", "")
                    name    = getattr(tc, "name", "")
                    args    = getattr(tc, "args", {})

                tool = tool_map.get(name)
                if tool:
                    try:
                        output = await tool.arun(args)
                    except Exception as e:
                        # 非 _RegistryToolAdapter 工具：单独记录错误并将错误信息反馈给 LLM
                        bus.tool_error(uid, self.name, name, str(e))
                        output = f"工具 {name} 执行失败: {e}"
                else:
                    output = f"未找到工具: {name}"
                    logger.warning("[bind_tools] 未找到工具 %s agent=%s", name, self.name)

                messages.append(ToolMessage(content=str(output), tool_call_id=call_id))

        final = messages[-1]
        return final.content

    def _load_background_from_template(self) -> None:
        """在 config/templates/{name}.txt 中查找背景模板，找到则覆盖 self.background。
        未找到时使用类定义中的兜底背景，并输出 WARNING 日志提示补充模板。
        """
        tpl_path = _AGENT_TEMPLATE_DIR / f"{self.name}.txt"
        try:
            content = tpl_path.read_text(encoding="utf-8").strip()
            if content:
                self.background = content
                logger.debug("[BaseAgent] %s 已从模板加载背景: %s", self.name, tpl_path)
                return
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("[BaseAgent] %s 读取模板失败 %s: %s", self.name, tpl_path, e)

        # 未找到模板文件
        if not self.background:
            logger.warning(
                "[BaseAgent] 未找到 agent '%s' 的模板文件 %s，且无兜底背景，将使用空提示词",
                self.name, tpl_path,
            )
        else:
            logger.warning(
                "[BaseAgent] 未找到 agent '%s' 的模板文件 %s，使用代码内置兜底背景",
                self.name, tpl_path,
            )

    def _build_system_prompt(self, context: Optional[Dict[str, Any]] = None) -> str:
        """构建包含角色、背景、技能、客户端环境和用户画像的系统提示词。

        Args:
            context: 当前请求上下文，含 _client_type/_client_version/_user_profile 键时自动注入。
        """
        from app.utils.client_env import format_env_for_prompt

        parts = []
        if self.role:
            parts.append(f"你是{self.role}。")
        if self.background:
            parts.append(self.background)
        skills_ctx = self.get_skills_context()
        if skills_ctx:
            parts.append("")
            parts.append(skills_ctx)

        if isinstance(context, dict):
            file_skills_ctx = context.get("_file_skills", "")
            if file_skills_ctx:
                parts.append("")
                parts.append(str(file_skills_ctx))
            env_block = format_env_for_prompt(
                context.get("_client_type"), context.get("_client_version")
            )
            if env_block:
                parts.append("")
                parts.append(env_block)
            profile_block = context.get("_user_profile", "")
            if profile_block:
                parts.append("")
                parts.append(str(profile_block))

        return "\n".join(parts) or "你是一个智能助手。"

    def _build_context_messages(self, context: Dict[str, Any]) -> list:
        """子类重写此方法，返回额外的上下文 Message 列表（注入在 HumanMessage 之前）。"""
        return []

    # ── 技能记忆系统 ────────────────────────────────────────────────

    async def load_skills(self) -> List[AgentSkill]:
        """从 DB 加载技能列表（运行时缓存，进程重启后重新加载）。"""
        if self._skills_loaded:
            return self._skills

        conn = await get_connection("mysql", None)
        if conn:
            try:
                df = await conn.execute_raw(
                    "SELECT skill_id, description, pattern, success_rate, usage_count, last_updated "
                    "FROM agent_skills WHERE agent_name = :name "
                    "ORDER BY success_rate DESC, usage_count DESC",
                    {"name": self.name},
                )
                if df is not None and len(df) > 0:
                    self._skills = [
                        AgentSkill(
                            skill_id    =str(row["skill_id"]),
                            description =str(row["description"]),
                            pattern     =str(row["pattern"]),
                            success_rate=float(row.get("success_rate", 1.0)),
                            usage_count =int(row.get("usage_count", 0)),
                            last_updated=str(row.get("last_updated", "")),
                        )
                        for _, row in df.iterrows()
                    ]
            except Exception as e:
                logger.debug("加载 agent 技能失败 agent=%s: %s", self.name, e)
            finally:
                await release_connection("mysql", conn)

        self._skills_loaded = True
        return self._skills

    async def update_skill(
        self,
        description: str,
        pattern:     str,
        success:     bool,
    ) -> None:
        """
        根据本次执行结果更新或新增技能记录。

        - 找到相似技能 → 更新 success_rate + usage_count
        - 无相似 & 未达上限 → 新增
        - 无相似 & 已达上限 → 替换 success_rate 最低的（仅当新技能更好时）
        """
        skills = await self.load_skills()

        # 简单相似匹配（前30字包含）
        matched: Optional[AgentSkill] = None
        for s in skills:
            if description[:30] in s.description or s.description[:30] in description:
                matched = s
                break

        conn = await get_connection("mysql", None)
        if not conn:
            return

        try:
            now = datetime.now()
            if matched:
                new_rate = (matched.success_rate * matched.usage_count + (1.0 if success else 0.0)) \
                           / (matched.usage_count + 1)
                await conn.execute_raw(
                    "UPDATE agent_skills "
                    "SET success_rate=:rate, usage_count=usage_count+1, pattern=:pat, last_updated=:ts "
                    "WHERE skill_id=:sid",
                    {"rate": round(new_rate, 4), "pat": pattern, "ts": now, "sid": matched.skill_id},
                )
                matched.success_rate = new_rate
                matched.usage_count += 1
                matched.pattern      = pattern
            else:
                new_sid  = uuid.uuid4().hex
                new_rate = 1.0 if success else 0.0
                if len(skills) < self.max_skills:
                    await conn.execute_raw(
                        "INSERT INTO agent_skills "
                        "(skill_id, agent_name, description, pattern, success_rate, usage_count, last_updated) "
                        "VALUES (:sid, :name, :desc, :pat, :rate, 1, :ts)",
                        {"sid": new_sid, "name": self.name, "desc": description,
                         "pat": pattern, "rate": new_rate, "ts": now},
                    )
                    self._skills.append(AgentSkill(new_sid, description, pattern, new_rate, 1))
                else:
                    worst = min(skills, key=lambda s: s.success_rate)
                    if new_rate > worst.success_rate:
                        await conn.execute_raw(
                            "UPDATE agent_skills "
                            "SET description=:desc, pattern=:pat, success_rate=:rate, "
                            "usage_count=1, last_updated=:ts WHERE skill_id=:sid",
                            {"desc": description, "pat": pattern,
                             "rate": new_rate, "ts": now, "sid": worst.skill_id},
                        )
                        skills.remove(worst)
                        skills.append(AgentSkill(worst.skill_id, description, pattern, new_rate, 1))
        except Exception as e:
            logger.warning("更新 agent 技能失败: %s", e)
        finally:
            await release_connection("mysql", conn)

    def get_skills_context(self) -> str:
        """将技能列表格式化为提示词片段，注入 LLM。"""
        if not self._skills:
            return ""
        lines = ["已积累的工作技能："]
        for i, s in enumerate(self._skills, 1):
            lines.append(f"  [{i}] {s.description}（成功率 {s.success_rate:.0%}）")
            if s.pattern:
                lines.append(f"       {s.pattern}")
        return "\n".join(lines)

    def invalidate_skills_cache(self) -> None:
        """清除技能缓存，下次 load_skills() 时重新从 DB 加载。"""
        self._skills        = []
        self._skills_loaded = False

    # ── 调用统计（私有）────────────────────────────────────────────

    def _record_call(self) -> None:
        """后台记录一次调用，不阻塞主流程。由 __init_subclass__ 自动触发，无需手动调用。"""
        asyncio.create_task(self._do_record_call())

    async def _do_record_call(self) -> None:
        conn = await get_connection("mysql", None)
        if not conn:
            return
        try:
            await conn.execute_raw(
                "INSERT INTO agent_call_stats (agent_name, source, called_at) "
                "VALUES (:name, :src, :ts)",
                {"name": self.name, "src": self.source, "ts": datetime.now()},
            )
        except Exception as e:
            logger.debug("记录调用统计失败: %s", e)
        finally:
            await release_connection("mysql", conn)

    # ── 元信息 ──────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":       self.name,
            "role":       self.role,
            "background": self.background,
            "tools":      self.tools,
            "is_public":  self.is_public,
            "source":     self.source,
            "user_id":    self.user_id,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} source={self.source!r}>"
