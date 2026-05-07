"""通用助手 Agent — 处理一般性问题、文件读写、网页内容分析

执行流程:
  1. 从任务描述中探测文件路径 / URL，主动获取内容注入 LLM 上下文
  2. 若任务需要输出新文件，LLM 响应后自动写入目标路径
  3. 返回最终回复（含文件保存提示）
"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base import BaseAgent
from app.agents.decorators import agent
from app.utils.content_fetcher import (
    detect_output_request,
    fetch_context_for_input,
    write_output_file,
)

# event loop 注入的工具请求说明分隔符（不需要展示给 LLM）
_TRS_MARKER = "\n\n如果你需要一个当前不存在的专用工具"


@agent(
    name="general_assistant",
    role="通用智能助手",
    background=(
        "你是 Hermes 系统的通用智能助手，擅长处理各类综合性任务：回答问题、提供建议，"
        "以及需要读写文件、访问外部资源的任务。"
        "当任务包含文件路径或需要生成文件时，使用工具请求机制获取 file_reader / 写文件工具，"
        "不要直接猜测文件内容。"
    ),
)
class GeneralAssistantAgent(BaseAgent):
    async def execute(self, task: dict, context: dict, llm) -> dict:
        await self.load_skills()

        # ── 1. 准备基础消息 ────────────────────────────────────────────
        messages = [SystemMessage(content=self._build_system_prompt())]
        history = context.get("history", [])
        for turn in history[-5:]:
            if isinstance(turn, dict):
                u = turn.get("user_input", turn.get("human", ""))
                a = turn.get("assistant_response", turn.get("ai", ""))
                if u:
                    messages.append(HumanMessage(content=u))

        # ── 2. 提取任务描述（去掉 event loop 注入的工具请求说明） ────────
        description = task.get("description", task.get("task_id", ""))
        if _TRS_MARKER in description:
            description = description[:description.index(_TRS_MARKER)]

        # ── 3. 主动获取文件 / URL 内容 ────────────────────────────────
        fetched_context, input_paths = await fetch_context_for_input(description)

        # ── 4. 检测是否需要输出文件 ───────────────────────────────────
        needs_output, output_path = detect_output_request(description, input_paths)

        # ── 5. 组装最终 Human 消息 ────────────────────────────────────
        human_parts = [description]

        if fetched_context:
            human_parts.append(
                "---\n"
                "以下是系统自动获取的相关内容，请基于这些内容完成任务：\n\n"
                + fetched_context
            )

        if needs_output and output_path:
            human_parts.append(
                f"---\n"
                f"请直接输出完整的 Markdown 正文（不要包含前言或说明语），"
                f"系统会自动将你的回复保存至：{output_path}"
            )

        messages.append(HumanMessage(content="\n\n".join(human_parts)))

        # ── 6. 调用 LLM ───────────────────────────────────────────────
        try:
            result = await llm.ainvoke(messages)
            answer = result.content if hasattr(result, "content") else str(result)
        except Exception as e:
            return {"result": f"助手处理失败: {e}", "success": False, "metadata": {}}

        # ── 7. 写入输出文件 ───────────────────────────────────────────
        if needs_output and output_path:
            save_result = write_output_file(output_path, answer)
            answer = f"{answer}\n\n---\n> {save_result}"

        await self.update_skill(description, answer, success=True)
        return {"result": answer, "success": True, "metadata": {"agent": self.name}}
