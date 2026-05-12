"""
【模块说明】向量化服务（EmbeddingService）— 把文字转成数字向量

【什么是 Embedding（向量化）？】
  这是让 AI 理解文字语义相似度的关键技术。
  把一段文字喂给专门的"向量模型"，它会输出一组数字（例如 1024 个数字）。
  含义相近的文字，输出的数字组合也相近，这样就能用数学计算来比较文字的"语义距离"。

【支持的服务】
  本模块同时支持两种部署方式：
  - 本地运行（Ollama）：在自己电脑上跑，数据不离开本机，适合隐私保护需求
  - 在线服务（OpenAI 兼容格式）：硅基流动、阿里 DashScope、智谱 AI、OpenAI 等

【切片说明】
  长对话会被切成小片段（chunk）再向量化，保证每次搜索都能找到最精准的片段，
  相邻片段之间会保留一点重叠内容，避免在句子边界丢失语境。

EmbeddingService - OpenAI 兼容格式的 embedding 服务。

同时支持：
  - Ollama 本地服务（/v1/embeddings，Ollama ≥ 0.1.24 原生支持 OpenAI 格式）
  - 所有 OpenAI 兼容的在线服务（SiliconFlow、DashScope、ZhipuAI、OpenAI 等）

config/system_config.yaml 示例：

  Ollama 本地：
    embedding:
      provider: "ollama"
      api_url: "http://localhost:11434"
      api_key: ""               # Ollama 不需要 key
      model_name: "bge-m3:latest"
      model_dim: 1024

  在线服务（以 SiliconFlow 为例）：
    embedding:
      provider: "openai"
      api_url: "https://api.siliconflow.cn"
      api_key: "sk-..."
      model_name: "BAAI/bge-m3"
      model_dim: 1024

切片策略（针对对话历史）：
  - Q+A 合并长度 ≤ chunk_size → 生成一个 qa_combined chunk（最优路径）
  - 超长时 → 问题切成 question chunk(s)，回答切成 answer_part chunk(s)
  - 切分在句子边界处进行（中英文标点），相邻 chunk 保留 overlap 个字符衔接语境
"""

import asyncio
import logging
import unicodedata
import re
from typing import Any, Dict, List, Optional

import httpx

# Chunk 数据类和切片逻辑已迁移至 app.rag.chunker，此处保留向后兼容导出
from app.rag.chunker import Chunk, TurnChunker  # noqa: F401

logger = logging.getLogger(__name__)


class EmbeddingService:
    """OpenAI 兼容格式的 embedding 服务，同时支持 Ollama 本地与在线服务。"""

    def __init__(self, config: Dict[str, Any]):
        embed_cfg          = config.get("embedding", {})
        self.provider      = embed_cfg.get("provider", "ollama").lower()
        self.api_url       = embed_cfg.get("api_url", "http://localhost:11434").rstrip("/")
        self.api_key       = embed_cfg.get("api_key", "")
        self.model_name    = embed_cfg.get("model_name", "")
        self.dim           = int(embed_cfg.get("model_dim", 1024))
        self.chunk_size    = int(embed_cfg.get("chunk_size", 800))
        self.chunk_overlap = int(embed_cfg.get("chunk_overlap", 100))
        self.batch_size    = int(embed_cfg.get("batch_size", 16))
        self._http: Optional[httpx.AsyncClient] = None
        # 切片器：委托给 app.rag.chunker.TurnChunker，此处保留 chunk_turn() 向后兼容接口
        self._chunker = TurnChunker(self.chunk_size, self.chunk_overlap)

    # ── 可用性 ─────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self.model_name)

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60.0, headers=self._build_headers())
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def is_available(self) -> bool:
        """检查服务可用性。Ollama 通过 /api/tags，在线服务通过实际 embed 探测。"""
        if not self.enabled:
            return False
        try:
            client = await self._client()
            if self.provider == "ollama":
                r = await client.get(f"{self.api_url}/api/tags", timeout=5.0)
                return r.status_code == 200
            else:
                # 在线服务没有通用健康检查端点，用一次实际 embed 探测
                vec = await self.embed("ping")
                return vec is not None
        except Exception:
            return False

    # ── Embedding 接口 ─────────────────────────────────────────

    # bge-m3 上下文窗口 8192 token，汉字按 2-3 token/字计算安全上限约 2700 字。
    # 4000 汉字 ≈ 8000-12000 token，超限后注意力分数溢出产生 NaN。
    _MAX_INPUT_CHARS = 2000

    def _preprocess_text(self, text: str) -> str:
        """清洗并截断文本，防止 Ollama 模型产生 NaN embedding。"""
        # 去除 null 字节及不可打印控制字符（保留 \n \t \r）
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
        # Unicode 归一化
        text = unicodedata.normalize("NFKC", text)
        # 合并多余空白
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        # 截断到安全长度
        if len(text) > self._MAX_INPUT_CHARS:
            text = text[: self._MAX_INPUT_CHARS]
        return text

    async def embed(self, text: str) -> Optional[List[float]]:
        """生成单条文本的向量，500/NaN 时自动降级或重试。失败返回 None。"""
        if not self.enabled or not text.strip():
            return None
        text = self._preprocess_text(text)
        if not text:
            return None
        try:
            client = await self._client()
            return await self._call_openai_embed(client, text)
        except Exception as e:
            logger.warning(f"embed 请求异常: {e}")
            return None

    async def _call_ollama_native_embed(
        self, client: httpx.AsyncClient, text: str
    ) -> Optional[List[float]]:
        """Ollama 原生 /api/embed 接口（备用）。响应格式：{"embeddings": [[...]]}"""
        url = f"{self.api_url}/api/embed"
        try:
            r = await client.post(url, json={"model": self.model_name, "input": text})
            if r.status_code == 200:
                data = r.json()
                embeddings = data.get("embeddings") or []
                if embeddings and isinstance(embeddings[0], list):
                    return embeddings[0]
            logger.warning(f"native embed fallback status={r.status_code} body={r.text[:200]}")
        except Exception as e:
            logger.warning(f"native embed fallback 异常: {e}")
        return None

    async def _call_openai_embed(
        self, client: httpx.AsyncClient, text: str
    ) -> Optional[List[float]]:
        """调用 OpenAI 兼容的 /v1/embeddings 端点。
        500+NaN 时降级到 Ollama 原生接口；仍失败则渐进截断重试（50%→25%→12.5%）。
        """
        url = f"{self.api_url}/v1/embeddings"
        payload = {"model": self.model_name, "input": text}

        for attempt in range(2):
            try:
                r = await client.post(url, json=payload)
            except Exception as e:
                logger.warning(f"embed 网络异常: {e}")
                return None

            if r.status_code == 200:
                data = r.json()
                items = data.get("data") or []
                if items and isinstance(items[0].get("embedding"), list):
                    return items[0]["embedding"]
                logger.warning(f"embed 响应结构异常: {data}")
                return None

            error_text = r.text
            logger.warning(
                f"embed status={r.status_code} attempt={attempt + 1} "
                f"error={error_text[:300]}"
            )

            if r.status_code == 500:
                # NaN 是 Ollama 已知 bug：输入过长或含特殊 token 导致注意力溢出
                if "NaN" in error_text and self.provider == "ollama" and attempt == 0:
                    logger.warning("检测到 NaN 错误，降级至 Ollama 原生 /api/embed 接口")
                    result = await self._call_ollama_native_embed(client, text)
                    if result is not None:
                        return result
                    # 原生接口也失败，渐进截断重试（50% → 25% → 12.5%）
                    return await self._nan_truncation_retry(client, text)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
            return None

        return None

    async def _nan_truncation_retry(
        self, client: httpx.AsyncClient, text: str
    ) -> Optional[List[float]]:
        """NaN 兜底：渐进截断到 50% / 25% / 12.5% 再重试 OpenAI 兼容接口。

        bge-m3 的 NaN bug 是内容敏感的（特定词向量触发注意力溢出），
        逐步缩小输入范围可提高绕过概率，且保留更多有效语义。
        """
        url = f"{self.api_url}/v1/embeddings"
        current = text
        for level in range(3):
            current = current[: max(len(current) // 2, 20)]
            if not current.strip():
                return None
            await asyncio.sleep(0.5)
            try:
                r = await client.post(
                    url, json={"model": self.model_name, "input": current}
                )
            except Exception as e:
                logger.warning(f"embed 截断重试网络异常(level={level + 1}): {e}")
                return None
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") or []
                if items and isinstance(items[0].get("embedding"), list):
                    logger.debug(
                        f"embed NaN兜底成功：截断至 {len(current)}/{len(text)} 字符"
                    )
                    return items[0]["embedding"]
            if "NaN" not in r.text:
                break  # 非 NaN 错误，截断无意义
        return None

    async def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """批量 embedding，按 batch_size 分批串行调用。"""
        results: List[Optional[List[float]]] = []
        for i in range(0, len(texts), self.batch_size):
            for t in texts[i: i + self.batch_size]:
                results.append(await self.embed(t))
        return results

    # ── 文本切片（向后兼容接口，实现已迁移至 app.rag.chunker.TurnChunker）──

    def chunk_turn(
        self,
        user_input:         str,
        assistant_response: str,
        turn_id:            str,
        chat_index:         str,
        agent_outputs:      Optional[List[Dict[str, str]]] = None,
    ) -> List[Chunk]:
        """将一轮对话切分为若干 Chunk（委托给 TurnChunker）。"""
        return self._chunker.chunk(
            user_input, assistant_response, turn_id, chat_index,
            agent_outputs=agent_outputs,
        )


# 向后兼容别名
OllamaEmbeddingService = EmbeddingService
