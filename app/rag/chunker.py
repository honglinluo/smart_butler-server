"""对话轮次文本切片器。

从 EmbeddingService 提取的 Chunk 数据类和切分逻辑，作为独立 RAG 组件维护。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Chunk:
    """向量存储单元，对应一轮对话的一个语义片段。

    chunk_id 格式：
      {turn_id}_q{i}          — 用户问题片段
      {turn_id}_a{i}          — 模型回复片段
      {turn_id}_{agent}_{i}   — 指定 Agent 的输出片段
    """
    chunk_id:       str
    chunk_text:     str
    chunk_type:     str            # "question" | "answer" | "agent_output"
    chunk_index:    int            # 本 turn 内的位置索引
    total_chunks:   int            # 本 turn 生成的 chunk 总数（事后填充）
    ref_doc_id:     str            # 关联的聊天历史 ES 文档 ID（turn_id）
    ref_chat_index: str            # 关联的聊天历史 ES 完整索引名
    agent_name:     str            = ""
    metadata:       Dict[str, Any] = field(default_factory=dict)


class TurnChunker:
    """将一轮对话切分为若干 Chunk，供后续 Embedding 使用。

    切分策略：
    - 用户问题和模型回复始终独立成 chunk，不再合并为 qa_combined
    - 若提供 agent_outputs，每个 Agent 的输出单独成一组 chunk
    - 用户输入短于 _MIN_Q_CHARS 字符视为确认/决策操作，不生成 question chunk
    """

    _MIN_Q_CHARS = 10

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100) -> None:
        self.chunk_size    = chunk_size
        self.chunk_overlap = chunk_overlap

    def _split_text(self, text: str, max_chars: int, overlap: int) -> List[str]:
        """在句子边界切分文本，相邻片段保留 overlap 字符衔接上下文。"""
        if len(text) <= max_chars:
            return [text]

        sentences = re.split(r'(?<=[。！？!?.…\n])', text)
        sentences = [s for s in sentences if s.strip()]

        chunks: List[str] = []
        current = ""
        for sent in sentences:
            if len(current) + len(sent) <= max_chars:
                current += sent
            else:
                if current:
                    chunks.append(current.strip())
                tail    = current[-overlap:] if overlap > 0 and len(current) > overlap else ""
                current = tail + sent

        if current.strip():
            chunks.append(current.strip())

        result: List[str] = []
        for c in chunks:
            if len(c) <= max_chars:
                result.append(c)
            else:
                for start in range(0, len(c), max_chars - overlap):
                    result.append(c[start: start + max_chars])
        return result if result else [text[:max_chars]]

    def chunk(
        self,
        user_input:         str,
        assistant_response: str,
        turn_id:            str,
        chat_index:         str,
        agent_outputs:      Optional[List[Dict[str, str]]] = None,
    ) -> List[Chunk]:
        """将一轮对话切分为若干 Chunk。

        Args:
            user_input:         用户原始输入
            assistant_response: 汇总后的模型回复
            turn_id:            ES 文档 ID
            chat_index:         关联的聊天历史 ES 索引名
            agent_outputs:      多 Agent 结构化输出列表（可选）

        Returns:
            切分后的 Chunk 列表，total_chunks 字段已回填。
        """
        chunks: List[Chunk] = []

        # ── 用户问题 ──────────────────────────────────────────
        q_text = user_input.strip()
        if len(q_text) >= self._MIN_Q_CHARS:
            for i, part in enumerate(self._split_text(q_text, self.chunk_size, 0)):
                chunks.append(Chunk(
                    chunk_id       = f"{turn_id}_q{i}",
                    chunk_text     = part,
                    chunk_type     = "question",
                    chunk_index    = len(chunks),
                    total_chunks   = 0,
                    ref_doc_id     = turn_id,
                    ref_chat_index = chat_index,
                    agent_name     = "",
                ))

        # ── 模型回复 ──────────────────────────────────────────
        if agent_outputs:
            # 多 Agent 结构化输出：每个 Agent 单独成块
            for agent_out in agent_outputs:
                name = (agent_out.get("agent_name") or "").strip()
                text = (agent_out.get("output") or "").strip()
                if not text:
                    continue
                safe_name = re.sub(r"\W+", "_", name) if name else "agent"
                for i, part in enumerate(
                    self._split_text(text, self.chunk_size, self.chunk_overlap)
                ):
                    chunks.append(Chunk(
                        chunk_id       = f"{turn_id}_{safe_name}_{i}",
                        chunk_text     = part,
                        chunk_type     = "agent_output",
                        chunk_index    = len(chunks),
                        total_chunks   = 0,
                        ref_doc_id     = turn_id,
                        ref_chat_index = chat_index,
                        agent_name     = name,
                    ))
        else:
            # 无结构化输出时：合并回复整体切片
            a_text = assistant_response.strip()
            if a_text:
                for i, part in enumerate(
                    self._split_text(a_text, self.chunk_size, self.chunk_overlap)
                ):
                    chunks.append(Chunk(
                        chunk_id       = f"{turn_id}_a{i}",
                        chunk_text     = part,
                        chunk_type     = "answer",
                        chunk_index    = len(chunks),
                        total_chunks   = 0,
                        ref_doc_id     = turn_id,
                        ref_chat_index = chat_index,
                        agent_name     = "",
                    ))

        total = len(chunks)
        for c in chunks:
            c.total_chunks = total
        return chunks
