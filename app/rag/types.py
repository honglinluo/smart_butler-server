"""
【模块说明】RAG 数据类型定义 — 描述"检索到的上下文"长什么样

RagContext 是 RAG 检索流程的输出结果，包含：
  - 最近几轮对话历史（让 AI 知道这次对话之前聊了什么）
  - 从长期记忆中检索到的相关片段（让 AI 知道更久以前的相关内容）
  - 格式化后的记忆文本（已处理成 AI 提示词格式，可直接使用）

RAG 核心数据类型。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class RagContext:
    """RAG pipeline 产生的上下文快照，供下游 LLM 推理使用。

    Attributes:
        history:      最近 N 轮对话，格式 [{"role": ..., "content": ...}]
        memories:     通过相关性过滤后的历史记忆列表
        memory_text:  面向提示词格式化的记忆文本；无相关记忆时为空字符串
        base_context: 调用方传入的原始 context dict（透传，不修改）
    """
    history:      List[Dict[str, Any]]
    memories:     List[Dict[str, Any]]
    memory_text:  str
    base_context: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_context(self) -> Dict[str, Any]:
        """合并为传入 LLM 的 context 字典。"""
        merged = dict(self.base_context)
        merged["history"]     = self.history
        merged["memory_text"] = self.memory_text
        merged["memories"]    = self.memories
        return merged
