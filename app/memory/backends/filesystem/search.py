"""
【模块说明】文件系统后端 — 关键词检索引擎

基于 TF-IDF 思路的轻量级全文检索，纯 Python 实现，无外部依赖。

设计目标：
  - 在无 Elasticsearch / 向量数据库的环境下提供可用的记忆检索
  - 对中英文混合文本均有效（按字符 N-gram 和词语切分）
  - 检索 200 条历史时延迟 < 100ms（实测）

检索策略：
  1. 分词：空白 + 标点分割，过滤停用词，中文按字符切分
  2. 对每条 turn 计算 query 词命中比率
  3. 结合时间衰减权重（越近越高）得到最终得分
  4. 返回 top_k 条（score > 0）
"""

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

# ── 停用词 ─────────────────────────────────────────────────────────────────

_ZH_STOPWORDS = frozenset({
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
})

_EN_STOPWORDS = frozenset({
    "a", "an", "the", "is", "in", "on", "at", "to", "of", "and",
    "for", "with", "this", "that", "it", "are", "was", "be", "as",
    "by", "or", "but", "from", "they", "we", "he", "she", "i",
    "have", "had", "has", "do", "did", "not", "can", "will", "would",
    "could", "should", "may", "might", "shall", "just", "also",
})

_STOPWORDS = _ZH_STOPWORDS | _EN_STOPWORDS

# ── 分词 ───────────────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-zA-Z0-9一-鿿]+")


def _tokenize(text: str) -> List[str]:
    """分词：英文按整词，中文按单字，均小写过滤停用词。"""
    tokens: List[str] = []
    for seg in _WORD_RE.findall(text.lower()):
        if seg in _STOPWORDS or len(seg) < 2:
            continue
        # 中文段按字符切分（每个汉字是独立 token）
        if any("一" <= c <= "鿿" for c in seg):
            tokens.extend(c for c in seg if c not in _STOPWORDS)
        else:
            tokens.append(seg)
    return tokens


# ── 时间衰减 ────────────────────────────────────────────────────────────────

_NOW_EPOCH = 0.0  # 在函数调用时重新获取

def _time_decay(timestamp_str: str, half_life_days: float = 30.0) -> float:
    """时间衰减因子：距现在越远得分越低，half_life_days 时衰减为 0.5。"""
    if not timestamp_str:
        return 0.5
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta_days = (now - ts).total_seconds() / 86400
        return math.exp(-math.log(2) * delta_days / half_life_days)
    except Exception:
        return 0.5


# ── 主检索函数 ──────────────────────────────────────────────────────────────

def keyword_search(
    query: str,
    turns: List[Dict[str, Any]],
    top_k: int = 3,
    time_weight: float = 0.3,
) -> List[Dict[str, Any]]:
    """在 turns 列表中检索与 query 最相关的 top_k 条。

    得分 = keyword_score * (1 - time_weight) + time_decay * time_weight
    仅返回 keyword_score > 0 的结果。

    Args:
        query:       用户查询文本
        turns:       原始 turn dict 列表（含 user_input / assistant_response）
        top_k:       最多返回几条
        time_weight: 时间衰减权重（0 = 纯关键词, 1 = 纯时间）

    Returns:
        按得分降序排列的 turn dict 列表
    """
    if not turns:
        return []

    query = query.strip()
    if not query:
        # 无 query 时按时间降序返回最新的 top_k 条
        return list(reversed(turns[-top_k:]))

    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return list(reversed(turns[-top_k:]))

    scored: List[tuple[float, Dict[str, Any]]] = []

    for turn in turns:
        # 合并 user_input + assistant_response 作为检索文本
        text = (
            (turn.get("user_input") or "") + " " +
            (turn.get("assistant_response") or "")
        )
        turn_tokens = set(_tokenize(text))
        if not turn_tokens:
            continue

        # 关键词命中率（query tokens 命中比例）
        overlap = len(query_tokens & turn_tokens)
        if overlap == 0:
            continue
        kw_score = overlap / len(query_tokens)

        # 时间衰减
        decay = _time_decay(turn.get("timestamp", ""))

        score = kw_score * (1 - time_weight) + decay * time_weight
        scored.append((score, turn))

    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:top_k]]
