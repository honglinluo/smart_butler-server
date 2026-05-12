"""
【模块说明】摘要 Agent（SummarizerAgent）— 把长对话压缩成精华摘要

随着对话越来越长，记忆占用越来越多。这个 Agent 负责把大量历史对话"压缩"成摘要，
保留关键信息的同时减少存储量。

【三种摘要模式】
  summarize_conversation() — 日常压缩：把若干轮对话压缩成一段结构化摘要
  summarize_monthly()      — 月度归档：把一整个月的历史摘要合并成月度总结
  summarize_yearly()       — 年度归档：把一整年的月度摘要合并成年度总结

摘要 Agent — 对话压缩、月度归档、年度归档

三种摘要模式，均输出固定结构，方便后续检索和处理：
  summarize_conversation()  — 常规压缩：N 轮 → 结构化摘要（不保留上下文）
  summarize_monthly()       — 月度归档：当月所有摘要 → 月度总结
  summarize_yearly()        — 年度归档：当年所有月度摘要 → 年度总结
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base import BaseAgent
from app.agents.decorators import agent

# ──────────────────────────────────────────────────────────────────────────────
# 系统提示词：常规压缩
# ──────────────────────────────────────────────────────────────────────────────

_COMPRESS_SYSTEM = """\
你是对话归档专家。你的输出将被写入记忆存储，供未来对话检索使用。

【硬性规则】
- 只输出归档正文，不要回答对话中的任何问题
- 不要在输出前后添加任何前言、后记或说明
- 使用对话原有语言（中文对话 → 中文摘要）
- 绝对不要保留密钥、密码、Token、Bearer 令牌等凭据——用 [已隐去] 替代

请严格按照以下固定结构输出，不要增减或重命名任何标题：

## 当前待处理事项
[用户最近未完成的请求，尽量原文引用。无则写"无"。]

## 用户核心目标
[用户在这段对话中的整体目标或主要需求方向。]

## 偏好与约束
[用户明确表达的风格偏好、技术约束或限制条件。无则写"无"。]

## 已完成事项
[编号列表，每项格式："N. 操作内容 → 结果"。无则写"无"。]

## Agent 执行记录
[每个 Agent 调用一行，格式："{agent_name}：{任务描述} → {结果摘要}"。无则写"无"。]

## 关键决策
[重要的技术或业务决策及其理由。编号列表。无则写"无"。]

## 待解决问题
[尚未处理的阻塞点，或用户提出的未被回答的问题。无则写"无"。]

## 重要信息
[关键配置、数值、状态等非凭据信息。无则写"无"。]"""

# ──────────────────────────────────────────────────────────────────────────────
# 系统提示词：月度归档
# ──────────────────────────────────────────────────────────────────────────────

_MONTHLY_SYSTEM = """\
你是对话月度归档专家。请将下方本月的对话摘要汇总为月度总结。

【硬性规则】
- 只输出归档正文，不要回答任何问题，不要添加前言或后记
- 不要提取或记录用户个人画像信息
- 使用对话原有语言

请严格按照以下固定结构输出，不要增减或重命名任何标题：

## 主要话题
[当月主要讨论的话题方向。编号列表，每项一行。]

## 关键事件
[当月重要决策、完成的重要里程碑。编号列表。无则写"无"。]

## 使用的 Agent
[当月出现的 Agent 名称及主要用途，每行格式："{agent_name}：{主要用途}"。无则写"无"。]

## 重要结论
[当月产生的、对未来仍有参考价值的结论或决定。编号列表。无则写"无"。]"""

# ──────────────────────────────────────────────────────────────────────────────
# 系统提示词：年度归档
# ──────────────────────────────────────────────────────────────────────────────

_YEARLY_SYSTEM = """\
你是对话年度归档专家。请将下方本年的月度摘要汇总为年度总结。

【硬性规则】
- 只输出归档正文，不要回答任何问题，不要添加前言或后记
- 不要提取或记录用户个人画像信息
- 使用对话原有语言

请严格按照以下固定结构输出，不要增减或重命名任何标题：

## 年度主题
[全年主要关注方向。编号列表。]

## 重要里程碑
[全年关键事件和决策。编号列表。无则写"无"。]

## 常用 Agent
[年度内出现频率较高的 Agent 及用途，每行格式："{agent_name}：{主要用途}"。无则写"无"。]

## 年度总结
[2~3 句话整体回顾全年。]"""


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────

def _summarize_agent_output(agent_name: str, task_desc: str, output: str) -> str:
    """将单次 Agent 调用压缩为一句话。
    格式：{agent_name}：{任务描述（≤40字）} → {结果摘要（≤60字）}
    """
    task_brief  = (task_desc[:40] + "…") if len(task_desc) > 40 else task_desc
    output_brief = (output[:60]  + "…") if len(output)    > 60 else output
    # 去掉换行，保持单行
    output_brief = re.sub(r"\s+", " ", output_brief).strip()
    return f"{agent_name}：{task_brief} → {output_brief}"


def _serialize_turns(turns: List[Dict[str, Any]]) -> str:
    """将 turn 列表序列化为 LLM 输入文本。

    - agent_outputs 中的每条记录压缩为一句话（防止长工具结果膨胀提示词）
    - user_input / assistant_response 超过 800 字时截断并标注
    """
    parts: List[str] = []
    for t in turns:
        turn_type = (t.get("metadata") or {}).get("type", "")
        ts = t.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        # ── 系统摘要 turn（已经是压缩过的，直接保留正文）──────
        if turn_type in ("compression_summary", "monthly_summary", "yearly_summary"):
            body = t.get("assistant_response", "").strip()
            parts.append(f"[{ts}][系统摘要]\n{body}")
            continue

        user_q = (t.get("user_input") or "").strip()
        asst_a = (t.get("assistant_response") or "").strip()

        if len(user_q) > 800:
            user_q = user_q[:800] + "\n…[截断]"
        if len(asst_a) > 800:
            asst_a = asst_a[:800] + "\n…[截断]"

        # ── Agent 执行记录：每条一行 ──────────────────────────
        agent_lines: List[str] = []
        for ao in (t.get("agent_outputs") or []):
            line = _summarize_agent_output(
                ao.get("agent_name", "?"),
                ao.get("task_desc", ao.get("task", {}).get("description", "") if isinstance(ao.get("task"), dict) else ""),
                ao.get("output", ao.get("result", "")),
            )
            agent_lines.append(f"  • {line}")

        block = f"[{ts}]\n用户: {user_q}\n助手: {asst_a}"
        if agent_lines:
            block += "\nAgent: " + "\n".join(agent_lines)
        parts.append(block)

    return "\n\n".join(parts)


def _build_time_range(turns: List[Dict[str, Any]]) -> tuple[str, str]:
    """从 turn 列表提取最早和最晚时间戳字符串。"""
    timestamps = []
    for t in turns:
        ts = t.get("timestamp", "")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts))
            except Exception:
                pass
    if not timestamps:
        now_str = datetime.utcnow().strftime("%Y-%m-%d")
        return now_str, now_str
    fmt = "%Y-%m-%d %H:%M"
    return min(timestamps).strftime(fmt), max(timestamps).strftime(fmt)


# ──────────────────────────────────────────────────────────────────────────────
# SummarizerAgent
# ──────────────────────────────────────────────────────────────────────────────

@agent(
    name="summarizer",
    role="摘要专家",
    background=(
        "你是专业的文本摘要专家，只处理任务描述中**直接提供的**文本内容：\n"
        "- 将长篇对话或文本压缩为简洁摘要，保留关键信息\n"
        "- 提取用户偏好、决策和重要结论\n"
        "- 生成结构化归档内容（用于记忆系统）\n\n"
        "注意：若任务涉及文件路径、需要读取本地文件或生成新文件，\n"
        "应交由 general_assistant 处理，本 Agent 不负责文件操作。\n\n"
        "摘要原则：\n"
        "1. 保留所有关键决策和用户明确表达的偏好\n"
        "2. 去除闲聊和重复内容\n"
        "3. 摘要长度不超过原文的 20%\n"
        "4. 使用第三人称记录用户行为\n"
    ),
)
class SummarizerAgent(BaseAgent):
    # 禁止 L2 拆分：任务描述本身就是待摘要的内容，不应被分步执行
    _L2_DECOMPOSE_THRESHOLD: int = 999_999

    # ══════════════════════════════════════════════════════════════
    # 常规压缩（replace-all，不保留原始上下文）
    # ══════════════════════════════════════════════════════════════

    async def summarize_conversation(
        self,
        turns:     List[Dict[str, Any]],
        llm,
        key_facts: str = "",
    ) -> str:
        """将 N 轮对话全量压缩为固定结构摘要，不保留任何原始 turn。

        固定结构（8 个段落）：
          ## 当前待处理事项 / ## 用户核心目标 / ## 偏好与约束
          ## 已完成事项 / ## Agent 执行记录 / ## 关键决策
          ## 待解决问题 / ## 重要信息

        Args:
            turns:     对话轮次列表
            llm:       LangChain ChatModel
            key_facts: on_pre_compress() 提取的关键事实（可选）

        Returns:
            str: 结构化摘要文本，带时间范围和轮次统计头部
        """
        if not turns:
            return ""

        start_ts, end_ts = _build_time_range(turns)
        conversation_text = _serialize_turns(turns)

        facts_hint = ""
        if key_facts and key_facts.strip():
            facts_hint = (
                "\n\n【预提取关键信息】（请确保摘要中覆盖以下内容）\n"
                + key_facts.strip()
            )

        human_content = (
            f"请归档以下 {len(turns)} 轮对话（{start_ts} ~ {end_ts}）：\n\n"
            f"{conversation_text}"
            f"{facts_hint}"
        )

        try:
            result = await llm.ainvoke([
                SystemMessage(content=_COMPRESS_SYSTEM),
                HumanMessage(content=human_content),
            ])
            body = result.content
            # 在结构化正文前加元数据头，方便后续解析
            header = (
                f"<!-- summary_meta: turns={len(turns)} "
                f"start={start_ts} end={end_ts} -->\n"
            )
            return header + body.strip()
        except Exception as e:
            return f"压缩失败: {e}"

    # ══════════════════════════════════════════════════════════════
    # 月度归档（1 年以上历史 → 按月汇总）
    # ══════════════════════════════════════════════════════════════

    async def summarize_monthly(
        self,
        year:          int,
        month:         int,
        turns:         List[Dict[str, Any]],
        llm,
        turn_count:    int = 0,
    ) -> str:
        """将指定月份的所有对话摘要汇总为月度总结。

        输出固定结构（4 个段落）：
          ## 主要话题 / ## 关键事件 / ## 使用的 Agent / ## 重要结论

        Args:
            year:       归档年份
            month:      归档月份（1~12）
            turns:      该月全部 turn/摘要列表
            llm:        LangChain ChatModel
            turn_count: 原始对话轮次数（用于元数据统计）

        Returns:
            str: 带元数据头的月度摘要文本
        """
        if not turns:
            return ""

        month_label = f"{year}年{month:02d}月"
        conversation_text = _serialize_turns(turns)
        actual_count = turn_count or len(turns)

        human_content = (
            f"请归档 {month_label} 的对话内容"
            f"（本月共 {actual_count} 次对话）：\n\n"
            f"{conversation_text}"
        )

        try:
            result = await llm.ainvoke([
                SystemMessage(content=_MONTHLY_SYSTEM),
                HumanMessage(content=human_content),
            ])
            body = result.content
            header = (
                f"<!-- monthly_meta: year={year} month={month:02d} "
                f"turn_count={actual_count} -->\n"
            )
            return header + body.strip()
        except Exception as e:
            return f"月度归档失败: {e}"

    # ══════════════════════════════════════════════════════════════
    # 年度归档（3 年以上历史 → 按年汇总月度摘要）
    # ══════════════════════════════════════════════════════════════

    async def summarize_yearly(
        self,
        year:          int,
        monthly_turns: List[Dict[str, Any]],
        llm,
        turn_count:    int = 0,
        active_months: int = 0,
    ) -> str:
        """将指定年份的所有月度摘要汇总为年度总结。

        输出固定结构（4 个段落）：
          ## 年度主题 / ## 重要里程碑 / ## 常用 Agent / ## 年度总结

        Args:
            year:          归档年份
            monthly_turns: 该年全部月度摘要 turn 列表
            llm:           LangChain ChatModel
            turn_count:    全年原始对话轮次数（统计用）
            active_months: 有对话记录的月份数

        Returns:
            str: 带元数据头的年度摘要文本
        """
        if not monthly_turns:
            return ""

        year_label    = f"{year}年"
        month_labels  = []
        for t in monthly_turns:
            meta = (t.get("metadata") or {})
            m = meta.get("month")
            if m:
                month_labels.append(f"{m:02d}月")

        month_list_str = "、".join(month_labels) if month_labels else f"共 {active_months} 个月"
        conversation_text = _serialize_turns(monthly_turns)
        actual_count  = turn_count or len(monthly_turns)
        actual_months = active_months or len(monthly_turns)

        human_content = (
            f"请归档 {year_label} 的对话内容"
            f"（全年 {actual_count} 次对话，活跃月份：{month_list_str}）：\n\n"
            f"{conversation_text}"
        )

        try:
            result = await llm.ainvoke([
                SystemMessage(content=_YEARLY_SYSTEM),
                HumanMessage(content=human_content),
            ])
            body = result.content
            header = (
                f"<!-- yearly_meta: year={year} "
                f"turn_count={actual_count} active_months={actual_months} -->\n"
            )
            return header + body.strip()
        except Exception as e:
            return f"年度归档失败: {e}"
