"""Agent 内部事件循环

执行流程
────────
1. 调用目标 agent 执行任务（第一次迭代注入工具请求说明）
2. 若响应含 ToolCodeRequest JSON → 路由给 code_assistant 构建专用工具
3. 工具注入到 hermes_engine 工具注册表，清除 agent graph 缓存
4. 更新任务描述（通知 agent 工具已就绪），重新调用 agent
5. 重复直到任务完成 or 达最大迭代次数

用户决策门控
────────────
- 每次工具构建前读 ``user:{user_id}:decision_policy``
  - allow → 直接继续
  - ask   → 挂起协程，等用户通过 POST /decisions/{id}/resolve 确认
  - deny  → 中止并返回拒绝提示

调用日志
────────
- 每步写 Python 标准日志（INFO），同时推送 Redis List（SSE 可消费）
"""
from __future__ import annotations

import importlib
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.agents.loop.decision_gate import DecisionState, UserDecisionGate
from app.agents.loop.events import (
    BuiltTool, LoopEventType, LoopLogEntry, ToolCodeRequest, TOOL_REQUEST_SCHEMA,
)
from app.agents.loop.loop_logger import LoopLogger
from app.agents.loop.tool_builder import ToolBuilder

logger = logging.getLogger("agent_loop")

_MAX_ITER = 6  # 最大迭代次数（含首次调用）


class AgentEventLoop:
    """Agent 内部事件循环（单例绑定到 HermesEngine）。"""

    def __init__(self, hermes_engine) -> None:
        self._engine       = hermes_engine
        self._tool_builder = ToolBuilder()
        self._decision_gate = UserDecisionGate(redis_db=self._redis_db())

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(
        self,
        user_id:    str,
        agent_name: str,
        task:       Dict[str, Any],
        context:    Dict[str, Any],
        llm,
        session_id: Optional[str] = None,
    ) -> Tuple[str, List[LoopLogEntry]]:
        """执行事件循环，返回 (最终结果文本, 完整日志列表)。"""
        sid      = session_id or uuid.uuid4().hex[:8]
        loop_log = LoopLogger(user_id, sid, redis_db=self._redis_db())

        loop_log.log(
            LoopEventType.TASK_START, agent_name,
            f"任务开始: {task.get('description', '')[:80]}",
        )

        injected_tools: List[str] = []
        current_task    = self._inject_tool_instructions(task)

        for iteration in range(1, _MAX_ITER + 1):
            loop_log.log(
                LoopEventType.AGENT_EXECUTING, agent_name,
                f"第 {iteration} 次调用 | 已注入工具: {injected_tools or '无'}",
                iteration=iteration,
            )

            result, ok = await self._raw_call(
                agent_name, current_task, context, llm, injected_tools,
            )

            if not ok:
                loop_log.log(
                    LoopEventType.TASK_FAILED, agent_name,
                    f"Agent 调用出错: {result[:120]}",
                    iteration=iteration,
                )
                return result, loop_log.get_entries()

            # ── 检测工具请求 ─────────────────────────────────────────────────
            tool_req = ToolCodeRequest.parse(result, agent_name)
            if tool_req is None:
                # 任务正常完成
                loop_log.log(
                    LoopEventType.TASK_COMPLETE, agent_name,
                    f"任务完成（第 {iteration} 次迭代）",
                    iteration=iteration,
                )
                return result, loop_log.get_entries()

            # ── 需要构建工具 ─────────────────────────────────────────────────
            loop_log.log(
                LoopEventType.TOOL_REQUESTED, agent_name,
                f"请求构建工具: {tool_req.tool_name} — {tool_req.description[:60]}",
                iteration=iteration,
                data={"tool_name": tool_req.tool_name},
            )

            # 用户决策检查
            decision_id = f"{sid}_i{iteration}_{tool_req.tool_name}"
            loop_log.log(
                LoopEventType.DECISION_REQUIRED, agent_name,
                f"等待用户决策 decision_id={decision_id}",
                iteration=iteration,
                data={"decision_id": decision_id, "action": tool_req.description},
            )

            decision = await self._decision_gate.check_and_wait(
                user_id     =user_id,
                decision_id =decision_id,
                action_desc =f"构建工具 {tool_req.tool_name}: {tool_req.description}",
            )

            if decision == DecisionState.DENIED:
                loop_log.log(
                    LoopEventType.DECISION_DENIED, agent_name,
                    f"用户拒绝构建工具 {tool_req.tool_name}，循环终止",
                    iteration=iteration,
                )
                return (
                    f"用户拒绝了工具「{tool_req.tool_name}」的构建请求，任务无法继续完成。",
                    loop_log.get_entries(),
                )

            loop_log.log(
                LoopEventType.DECISION_GRANTED, agent_name,
                f"决策通过，开始构建工具: {tool_req.tool_name}",
                iteration=iteration,
            )

            # ── 构建工具 ─────────────────────────────────────────────────────
            loop_log.log(
                LoopEventType.TOOL_BUILDING, "code_assistant",
                f"code_assistant 开始生成代码: {tool_req.tool_name}",
                iteration=iteration,
            )

            built = await self._tool_builder.build(tool_req, llm)

            if not built.success:
                loop_log.log(
                    LoopEventType.TASK_FAILED, "code_assistant",
                    f"工具构建失败: {built.error}",
                    iteration=iteration,
                    data={"error": built.error},
                )
                return (
                    f"工具「{tool_req.tool_name}」构建失败：{built.error}",
                    loop_log.get_entries(),
                )

            loop_log.log(
                LoopEventType.TOOL_BUILT, "code_assistant",
                f"工具构建成功: {built.file_path}",
                iteration=iteration,
                data={
                    "module_path":   built.module_path,
                    "function_name": built.function_name,
                },
            )

            # ── 注入工具 ─────────────────────────────────────────────────────
            inject_ok = await self._inject_tool(agent_name, built)
            if not inject_ok:
                loop_log.log(
                    LoopEventType.TASK_FAILED, agent_name,
                    f"工具注入失败: {built.tool_name}",
                    iteration=iteration,
                )
                return f"工具「{built.tool_name}」注入失败，无法继续。", loop_log.get_entries()

            injected_tools.append(built.tool_name)
            loop_log.log(
                LoopEventType.TOOL_INJECTED, agent_name,
                f"工具 {built.tool_name} 已注入，准备第 {iteration + 1} 次调用",
                iteration=iteration,
            )

            # 更新任务：通知 agent 工具已就绪，请直接调用
            current_task = dict(task)
            current_task["description"] = (
                task.get("description", "") +
                f"\n\n[系统提示] 工具 `{built.tool_name}`（函数名 `{built.function_name}`）"
                "已在你的工具列表中就绪，请直接调用该工具完成任务，"
                "不要再发起新的工具构建请求。"
            )

            loop_log.log(
                LoopEventType.AGENT_RETRYING, agent_name,
                f"即将进行第 {iteration + 1} 次调用",
                iteration=iteration,
            )

        # 超出最大迭代次数
        loop_log.log(
            LoopEventType.LOOP_MAX_ITER, agent_name,
            f"已达最大迭代次数 {_MAX_ITER}，终止循环",
            iteration=_MAX_ITER,
        )
        return (
            f"任务在 {_MAX_ITER} 次迭代内未能完成，请将任务拆分为更小的子任务后重试。",
            loop_log.get_entries(),
        )

    # ── 内部：无循环 agent 调用 ───────────────────────────────────────────────

    async def _raw_call(
        self,
        agent_name:     str,
        task:           Dict[str, Any],
        context:        Dict[str, Any],
        llm,
        injected_tools: List[str],
    ) -> Tuple[str, bool]:
        """直接调用 agent，不经过事件循环（避免递归）。

        若 injected_tools 非空，使用 LangGraph ReAct 模式以支持工具调用；
        否则优先走 registry agent 的 execute()。
        """
        try:
            if injected_tools:
                # 有注入工具 → 走 worker_with_tools 以便 LangGraph 能调用工具
                result_str, _ = await self._engine._execute_worker_with_tools(
                    worker_name =agent_name,
                    tasks       =[task],
                    user_id     ="__loop__",
                    context     =context,
                    llm         =llm,
                    extra_tools =injected_tools,
                )
                return result_str, True

            # 无注入工具 → 优先走 registry agent
            ag = await self._engine._get_or_load_agent(agent_name, "__loop__")
            if ag is not None:
                res = await ag.execute(task, dict(context), llm)
                return res.get("result", ""), res.get("success", True)

            # 兜底：走 worker
            result_str, _ = await self._engine._execute_worker_with_tools(
                worker_name=agent_name,
                tasks=[task],
                user_id="__loop__",
                context=context,
                llm=llm,
            )
            return result_str, True

        except Exception as exc:
            logger.error("[EventLoop] _raw_call agent=%s 异常: %s", agent_name, exc)
            return str(exc), False

    # ── 内部：注入工具到引擎 ──────────────────────────────────────────────────

    async def _inject_tool(self, agent_name: str, built: BuiltTool) -> bool:
        """将已构建工具注入引擎注册表，并清除 agent graph 缓存使其重建。"""
        engine = self._engine
        try:
            # 1. 动态导入生成的模块与函数
            mod  = importlib.import_module(built.module_path)
            func = getattr(mod, built.function_name)

            # 2. 注册 tool_config（供 _load_tool_function 查找）
            tool_config = {
                "name":        built.tool_name,
                "description": built.description,
                "path":        f"{built.module_path}.{built.function_name}",
            }
            engine.tool_configs[built.tool_name]          = tool_config
            engine.loaded_tool_functions[built.tool_name] = func

            # 3. 缓存为 LangChain Tool
            from app.core.hermes_engine import LangChainToolWrapper
            lc_tool = LangChainToolWrapper(built.tool_name, func, tool_config)
            engine.langchain_tools[built.tool_name] = lc_tool

            # 4. 清除 agent graph 缓存，下次调用时携带新工具重建
            engine.agent_graphs.pop(agent_name, None)

            # 5. 更新 worker_config 工具列表（YAML worker 路径）
            if agent_name in engine.worker_configs:
                tools_list = engine.worker_configs[agent_name].setdefault("tools", [])
                if built.tool_name not in tools_list:
                    tools_list.append(built.tool_name)

            # 6. 更新 registry agent 的 tools 属性（code/db agent 路径）
            from app.agents.registry import registry
            ag = registry.get(agent_name)
            if ag and built.tool_name not in ag.tools:
                ag.tools.append(built.tool_name)

            logger.info(
                "[EventLoop] 工具 %s 注入成功 → agent=%s",
                built.tool_name, agent_name,
            )
            return True

        except Exception as exc:
            logger.error("[EventLoop] 工具注入失败 tool=%s: %s", built.tool_name, exc)
            return False

    # ── 内部：注入工具请求说明到任务 ─────────────────────────────────────────

    @staticmethod
    def _inject_tool_instructions(task: Dict[str, Any]) -> Dict[str, Any]:
        """首次迭代时将工具请求格式说明追加到任务描述中。"""
        updated = dict(task)
        desc = task.get("description", "")
        if TOOL_REQUEST_SCHEMA not in desc:
            updated["description"] = desc + "\n\n" + TOOL_REQUEST_SCHEMA
        return updated

    # ── 内部：获取 Redis 连接 ─────────────────────────────────────────────────

    def _redis_db(self):
        mm = getattr(self._engine, "memory_manager", None)
        if mm is None:
            return None
        return getattr(mm, "_redis", None)
