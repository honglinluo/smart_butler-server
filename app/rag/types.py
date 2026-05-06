"""RAG 核心数据类型。"""

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
