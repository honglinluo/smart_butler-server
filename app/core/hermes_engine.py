"""
【模块说明】赫尔墨斯引擎（HermesEngine）— AI 对话调度总控中心

这是整个系统最核心的模块，负责接收用户的消息，然后协调所有 AI 组件来生成回复。
可以把它理解为一个"智能调度员"：

┌─────────────────────────────────────────────────────────┐
│                     用户发来消息                          │
│                         ↓                               │
│  1. 理解意图（用户想做什么？简单聊天还是需要专业帮助？）       │
│                         ↓                               │
│  2. 路由分发（交给哪个 Agent 处理最合适？）                  │
│                         ↓                               │
│  3. Agent 执行（Agent 使用工具完成任务，可能触发授权弹窗）     │
│                         ↓                               │
│  4. 生成回复（把结果整理成自然语言返回给用户）                 │
│                         ↓                               │
│  5. 保存记忆（把这轮对话存入记忆系统，下次会记得）             │
└─────────────────────────────────────────────────────────┘

【流式输出】
  支持两种回复模式：
  - 普通模式：等全部处理完后一次性返回
  - 流式模式：像打字机一样逐字实时推送（通过 SSE 协议）

【危险操作授权】
  当 Agent 要执行危险动作时，引擎会暂停并向前端推送授权请求，
  等用户同意/拒绝后再继续或中止。

【记忆注入】
  每次对话前，引擎会从记忆系统取出相关的历史记录注入到提示词中，
  让 AI 能"记住"之前的对话内容。
"""


import asyncio
import importlib
import inspect
import json
import logging
import os
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from typing import Dict, List, Any, Optional, Callable, Coroutine, Tuple

try:
    import httpx as _httpx
    _HTTPX_CONNECT_ERRORS = (_httpx.ConnectError, _httpx.ConnectTimeout, _httpx.TimeoutException)
    _HTTPX_STATUS_ERRORS  = (_httpx.HTTPStatusError,)
except ImportError:
    _HTTPX_CONNECT_ERRORS = (OSError,)
    _HTTPX_STATUS_ERRORS  = ()

try:
    from openai import (
        APIConnectionError as _OAIConnectionError,
        APITimeoutError    as _OAITimeoutError,
        APIStatusError     as _OAIStatusError,
    )
    _OPENAI_CONNECT_ERRORS = (_OAIConnectionError, _OAITimeoutError)
    _OPENAI_STATUS_ERRORS  = (_OAIStatusError,)
except ImportError:
    _OPENAI_CONNECT_ERRORS = ()
    _OPENAI_STATUS_ERRORS  = ()

# 所有"LLM 端点无法连接"的异常类型（含超时），统一用于 except 子句
_LLM_CONNECT_ERRORS = _HTTPX_CONNECT_ERRORS + _OPENAI_CONNECT_ERRORS
# LLM API 返回 HTTP 非 200 状态码时的异常类型
_LLM_HTTP_ERRORS    = _HTTPX_STATUS_ERRORS + _OPENAI_STATUS_ERRORS
# 所有可重试的 LLM 调用异常（连接失败 + 非 200 响应）
_LLM_RETRYABLE_ERRORS = _LLM_CONNECT_ERRORS + _LLM_HTTP_ERRORS

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import Runnable
from langchain.chat_models import init_chat_model

# 尝试使用新的 LangGraph API
try:
    from langgraph.prebuilt import create_react_agent
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False
    create_react_agent = None

from app.agents.router import RouterAgent
from app.agents.base import _RegistryToolAdapter
from app.core.config_loader import ConfigLoader
from app.database.pool import get_connection, release_connection
from app.memory.backends.vectordb.chat_history_store import ChatHistoryStore
from app.rag import RagPipeline

logger = logging.getLogger(__name__)


class _LLMRetryExhausted(RuntimeError):
    """LLM 调用经 3 次重试全部失败后抛出，携带最后一次错误消息。"""
    def __init__(self, llm_message: str):
        super().__init__(llm_message)
        self.llm_message = llm_message


# ══════════════════════════════════════════════════════════════════
# 流式记忆上下文过滤器（参考 hermes-agent StreamingContextScrubber）
# 在流式输出中剔除模型可能回显的 <memory-context> 块
# ══════════════════════════════════════════════════════════════════

class StreamingContextScrubber:
    """
    流式输出"记忆标签过滤器"。

    【作用】
    系统提示词中注入了 <memory-context>...</memory-context> 标签包裹的历史记忆，
    这些内容是给 AI 看的背景信息，不应该被展示给用户。
    但部分模型偶尔会把这段内容原样"回显"到输出中。

    本类监控流式输出的每一个文字片段，
    一旦发现开始输出 <memory-context> 就进入"屏蔽模式"，
    直到 </memory-context> 出现后才恢复正常输出。
    保证用户永远看不到这些"幕后"内容。

    用法：
        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)    # 过滤后的安全内容
            if visible:
                emit(visible)
        trailing = scrubber.flush()           # 处理末尾残留
        if trailing:
            emit(trailing)
    """

    _OPEN_TAG  = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self._in_span: bool = False
        self._buf:     str  = ""

    def reset(self) -> None:
        self._in_span = False
        self._buf     = ""

    def feed(self, text: str) -> str:
        """返回 text 中去掉 memory-context 块后的可见部分。"""
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list = []

        while buf:
            if self._in_span:
                idx = buf.lower().find(self._CLOSE_TAG)
                if idx == -1:
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[idx + len(self._CLOSE_TAG):]
                self._in_span = False
            else:
                idx = buf.lower().find(self._OPEN_TAG)
                if idx == -1:
                    held = self._max_partial_suffix(buf, self._OPEN_TAG)
                    if held:
                        out.append(buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        out.append(buf)
                    return "".join(out)
                if idx > 0:
                    out.append(buf[:idx])
                buf = buf[idx + len(self._OPEN_TAG):]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        """流结束时刷出缓冲区；若仍在 span 内则丢弃（防泄露）。"""
        if self._in_span:
            self._buf     = ""
            self._in_span = False
            return ""
        tail      = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        for i in range(min(len(buf_lower), len(tag_lower) - 1), 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0


@dataclass
class InputMessage:
    """表示来自用户或系统的输入消息。"""
    user_id: str
    content: str
    role: str = "user"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_langchain(self) -> Any:
        if self.role == "system":
            return SystemMessage(content=self.content)
        if self.role == "assistant":
            return AIMessage(content=self.content)
        return HumanMessage(content=self.content)


@dataclass
class OutputMessage:
    """表示由 Hermes 返回的输出消息。"""
    user_id: str
    content: str
    role: str = "assistant"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_langchain(self) -> AIMessage:
        return AIMessage(content=self.content)

    @classmethod
    def from_text(cls, user_id: str, content: str) -> "OutputMessage":
        return cls(user_id=user_id, content=content)


class LangChainToolWrapper(BaseTool):
    """将动态加载的函数包装为 LangChain Tool。"""

    def __init__(self, tool_name: str, tool_func: Callable, tool_config: Dict[str, Any]):
        super().__init__()
        self.name = tool_name
        self.description = tool_config.get("description", f"Tool: {tool_name}")
        self.tool_func = tool_func
        self.tool_config = tool_config

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        return self.tool_func(*args, **kwargs)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(self.tool_func):
            return await self.tool_func(*args, **kwargs)
        return self.tool_func(*args, **kwargs)


def _validate_llm_url(url: str) -> str:
    """校验 LLM API URL 格式，必须为合法的 http/https URL。

    Args:
        url: 待校验的 URL 字符串

    Returns:
        校验通过的原始 URL

    Raises:
        ValueError: URL 格式不合法时
    """
    from urllib.parse import urlparse
    if not url or not url.strip():
        raise ValueError("LLM URL 不能为空")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"LLM URL 必须以 http:// 或 https:// 开头，当前值: {url!r}")
    if not parsed.netloc:
        raise ValueError(f"LLM URL 缺少主机地址，当前值: {url!r}")
    return url.strip()


@dataclass
class LLMInfo:
    """LLM 信息数据模型与构建器。"""
    user_id: str
    url: str
    api_key: str
    model_name: str
    model_type: str = "chat"
    temperature: float = 0.7

    def __post_init__(self) -> None:
        if self.url:
            try:
                self.url = _validate_llm_url(self.url)
            except ValueError as e:
                logger.warning("LLMInfo.url 校验失败: %s", e)

    @classmethod
    async def load(
        cls,
        user_id: str,
        db_alias: Optional[str] = None,
        table_name: str = "llms",
        fallback_user_id: str = "0",
    ) -> Optional["LLMInfo"]:
        """从数据库加载 LLM 配置。

        加载优先级：
        1. 若 user_id != '0'，先查 users.current_llm_id；
           若 current_llm_id 非 NULL，按 id 加载 llms 对应记录；
        2. 若 current_llm_id 为 NULL 或 user_id = '0'，加载系统默认
           (llms WHERE user_id='0' AND state=1 AND model_type!='embedding')。
        """
        connection = None
        try:
            connection = await get_connection("mysql", db_alias)

            if user_id != fallback_user_id:
                # 查询用户选择的模型 ID
                user_row_df = await connection.execute_raw(
                    "SELECT current_llm_id FROM users WHERE user_id = :uid LIMIT 1",
                    {"uid": user_id},
                )
                current_llm_id = None
                if user_row_df is not None and len(user_row_df) > 0:
                    val = user_row_df.iloc[0].get("current_llm_id")
                    current_llm_id = int(val) if val is not None else None

                if current_llm_id is not None:
                    df = await connection.execute_raw(
                        f"SELECT url, api_key, model_name, model_type, temperature "
                        f"FROM {table_name} WHERE id = :mid AND state = 1 "
                        "AND model_type != 'embedding' LIMIT 1",
                        {"mid": current_llm_id},
                    )
                    if df is not None and len(df) > 0:
                        row = df.iloc[0]
                        logger.debug(f"通过 current_llm_id={current_llm_id} 加载用户 {user_id} 的 LLM 配置")
                        return cls(
                            user_id=user_id,
                            url=row.get("url") or "",
                            api_key=row.get("api_key") or "",
                            model_name=row.get("model_name") or "",
                            model_type=row.get("model_type") or "chat",
                            temperature=float(row.get("temperature") or 0.7),
                        )
                    logger.info(f"current_llm_id={current_llm_id} 对应的模型不可用，回落到系统默认")

            # 系统默认模型（user_id='0'，state=1）
            default_df = await connection.execute_raw(
                f"SELECT url, api_key, model_name, model_type, temperature "
                f"FROM {table_name} WHERE user_id = :uid AND state = 1 "
                "AND model_type != 'embedding' ORDER BY id DESC LIMIT 1",
                {"uid": fallback_user_id},
            )
            if default_df is None or len(default_df) == 0:
                logger.warning("未找到任何 LLM 配置（含系统默认）")
                return None

            row = default_df.iloc[0]
            logger.debug(f"加载系统默认 LLM 配置: {row.to_dict()}")
            return cls(
                user_id=user_id,
                url=row.get("url") or "",
                api_key=row.get("api_key") or "",
                model_name=row.get("model_name") or "",
                model_type=row.get("model_type") or "chat",
                temperature=float(row.get("temperature") or 0.7),
            )
        except Exception as e:
            logger.error(f"加载 LLMInfo 失败: {type(e).__name__}: {e}", exc_info=True)
            return None
        finally:
            if connection:
                await release_connection("mysql", connection)

    def _is_ollama(self) -> bool:
        """通过 URL 判断是否为本地 Ollama 模型（支持 localhost / 127.0.0.1 / 自定义域名含 ollama）。"""
        if not self.url:
            return False
        url_lower = self.url.lower()
        return "11434" in url_lower or "ollama" in url_lower

    @property
    def provider(self) -> str:
        if self._is_ollama():
            return "openai"  # Ollama 使用 OpenAI 兼容接口
        model_lower = self.model_name.lower()
        if "gpt" in model_lower:
            return "openai"
        if "claude" in model_lower:
            return "anthropic"
        if "gemini" in model_lower:
            return "google_genai"
        if "llama" in model_lower or "vicuna" in model_lower:
            return "together"
        return "openai"

    def to_model_kwargs(self) -> Dict[str, Any]:
        # 超时优先级：环境变量 LLM_TIMEOUT > 模型类型默认值
        # max_retries=0：禁用 SDK 内部重试，统一由 hermes_engine 的重试循环管理，
        # 避免"SDK 重试 × engine 重试"的组合导致最坏情况等待时间成倍放大
        _default_timeout = 120.0 if self._is_ollama() else 120.0
        _timeout = float(os.getenv("LLM_TIMEOUT", _default_timeout))

        if self._is_ollama():
            base_url = self.url.rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            return {
                "model": self.model_name,
                "model_provider": "openai",
                "base_url": base_url + "/v1",
                "api_key": self.api_key or "ollama",
                "temperature": self.temperature,
                "timeout": _timeout,
                "max_retries": 0,
            }
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "model_provider": self.provider,
            "temperature": self.temperature,
            "api_key": self.api_key,
            "timeout": _timeout,
            "max_retries": 0,
        }
        if self.provider == "openai" and self.url:
            kwargs["base_url"] = self.url
        return kwargs

    async def build_chat_model(self) -> Optional[BaseChatModel]:
        try:
            return init_chat_model(**self.to_model_kwargs())
        except Exception as e:
            logger.error(f"构建 LLM 模型失败: {e}")
            return None


class AgentExecutorCache:
    """AgentExecutor 缓存管理类。使用 LangGraph 的新 API。"""

    def __init__(
        self,
        user_id: str,
        llm_factory: Callable[[str], Coroutine[Any, Any, Optional[BaseChatModel]]],
        tool_loader: Callable[[str], Optional[BaseTool]],
        db_alias: Optional[str] = None,
        agents_table: str = "agents",
    ):
        self.user_id = user_id
        self.llm_factory = llm_factory
        self.tool_loader = tool_loader
        self.db_alias = db_alias
        self.agents_table = agents_table
        self.cache: Dict[str, Any] = {}  # 使用通用 Any 类型而不是 AgentExecutor

    async def _fetch_agent_rows(self) -> List[Dict[str, Any]]:
        connection = None
        try:
            connection = await get_connection("mysql", self.db_alias)
            sql = (
                f"SELECT agent_name, job, desc FROM {self.agents_table} "
                "WHERE (user_id = :user_id OR public = 1) AND state = 1 "
                "ORDER BY agent_name ASC"
            )
            df = await connection.execute_raw(sql, {"user_id": self.user_id})
            if df is None or len(df) == 0:
                return []
            return [
                {"agent_name": row["agent_name"], "job": row["job"], "desc": row["desc"]}
                for _, row in df.iterrows()
            ]
        except Exception as e:
            logger.error(f"获取 AgentExecutor 配置失败: {e}")
            return []
        finally:
            if connection:
                await release_connection("mysql", connection)

    async def _build_executor(
        self,
        agent_name: str,
        job: str,
        desc: str,
        llm: BaseChatModel,
    ) -> Optional[Any]:
        """使用 LangGraph 的 create_react_agent 构建执行器"""
        try:
            tool_ids: List[str] = []
            if desc:
                try:
                    payload = json.loads(desc)
                    tool_ids = payload.get("tools", []) or []
                except Exception:
                    logger.warning(f"解析 agent desc 失败，agent_name={agent_name}")

            tools: List[BaseTool] = []
            for tool_id in tool_ids:
                tool_obj = self.tool_loader(tool_id)
                if tool_obj:
                    tools.append(tool_obj)

            # 使用 LangGraph 的 create_react_agent 而不是过时的 AgentExecutor
            if HAS_LANGGRAPH and create_react_agent:
                system_prompt = f"你是 {agent_name} 代理，负责执行: {job}。"
                executor = create_react_agent(llm, tools, prompt=system_prompt)
                return executor
            else:
                logger.warning(f"LangGraph 不可用，跳过 {agent_name} 的执行器创建")
                return None

        except Exception as e:
            logger.error(f"构建 AgentExecutor 失败: {agent_name}, {e}")
            return None

    async def load_executors(self) -> None:
        if self.cache:
            return

        agent_rows = await self._fetch_agent_rows()
        if not agent_rows:
            return

        llm = await self.llm_factory(self.user_id)
        if llm is None:
            logger.error("无法加载 AgentExecutor：LLM 构建失败")
            return

        for row in agent_rows:
            executor = await self._build_executor(row["agent_name"], row["job"], row["desc"], llm)
            if executor:
                self.cache[row["agent_name"]] = executor

    async def get(self, agent_name: str) -> Optional[Any]:
        if agent_name in self.cache:
            return self.cache[agent_name]
        await self.load_executors()
        return self.cache.get(agent_name)

    def list_executors(self) -> List[str]:
        return list(self.cache.keys())

    async def clear_cache(self) -> None:
        self.cache.clear()
        logger.info("已清空 AgentExecutor 缓存")


class HermesEngine:
    """
    Hermes 编排引擎 - 基于 LangChain 的多智能体协调引擎

    核心职责:
    1. LLM 生命周期管理 (创建、缓存、销毁)
    2. 代理生命周期管理 (初始化、执行、销毁)
    3. 任务拆解与路由 (根据意图分配给合适的子代理)
    4. LangChain Tool 编排 (动态加载、包装、执行)
    5. Agent 执行流程 (使用 LangChain AgentExecutor)
    6. 上下文传递 (在代理间流转状态)
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 Hermes 引擎

        Args:
            config: 系统配置
        """
        self.config = config
        self.config_loader = ConfigLoader("config")

        # LLM 管理
        self.llm_cache: Dict[str, BaseChatModel] = {}  # 保留字段，不再用于用户请求缓存
        self.default_llm: Optional[BaseChatModel] = None  # 默认 LLM

        # 代理管理
        self.router_agent: Optional[RouterAgent] = None
        self.agents: Dict[str, Any] = {}
        self.agent_executors: Dict[str, Any] = {}  # LangChain AgentExecutor 缓存

        # 配置管理
        self.worker_configs: Dict[str, Dict[str, Any]] = {}
        self.tool_configs: Dict[str, Dict[str, Any]] = {}
        self.agents_config: Dict[str, Any] = {}
        self.agent_config: Dict[str, Any] = {}
        self.intent_agent_mapping: Dict[str, str] = {}

        # 工具管理
        self.loaded_tool_functions: Dict[str, Any] = {}
        self.langchain_tools: Dict[str, BaseTool] = {}  # LangChain Tool 对象缓存
        self.agent_executor_caches: Dict[str, AgentExecutorCache] = {}
        self.agent_graphs: Dict[str, Any] = {}  # LangGraph 代理图缓存
        # 聊天记录存储
        self.chat_history = ChatHistoryStore()
        # 流式取消信号（每个流式会话一个 Event，key=stream_id）
        self._cancel_signals: Dict[str, asyncio.Event] = {}
        # 已取消的 turn_id 集合（阻止 _save_turn_async 写入）
        self._cancelled_turns: set = set()
        # 危险操作授权等待 Future：request_id -> Future[str]
        self._consent_futures: Dict[str, asyncio.Future] = {}
        # 记忆管理器（由 set_memory_manager() 注入）
        self.memory_manager = None
        # RAG 流水线（由 set_rag_pipeline() 注入，负责检索/索引/重向量化）
        self.rag_pipeline: Optional[RagPipeline] = None
        # 按代理名称缓存的系统 Prompt 模板（从 templates/ 目录加载）
        self._agent_system_prompts: Dict[str, str] = {}
        # Agent 事件循环（initialize() 后可用）
        self._event_loop = None

    async def initialize(self) -> None:
        """异步初始化引擎，加载配置、初始化 LLM、启动代理"""
        logger.info("开始初始化 Hermes 引擎...")

        # 加载配置
        self.agents_config = self.config_loader.load_agents_config()
        self.agent_config = self.agents_config.get("router", {})
        self.intent_agent_mapping = self.agents_config.get("intent_agent_mapping", {})

        # 从 templates/ 目录加载各代理系统 Prompt
        self._load_agent_prompts()

        # 初始化默认 LLM
        await self._initialize_default_llm()

        # 初始化各类代理
        self._initialize_router()
        self._initialize_workers()
        self._initialize_tools()

        # 初始化 Agent 事件循环
        from app.agents.loop.event_loop import AgentEventLoop
        self._event_loop = AgentEventLoop(self)

        logger.info("Hermes 引擎初始化完成 ✓")

    def _load_agent_prompts(self) -> None:
        """从 agents_config 的 prompt_template 字段加载各代理系统 Prompt 模板。

        规则：
        - 路径相对于项目根目录（即 config/ 的上级目录）
        - 模板统一存放在 config/templates/ 目录，HermesEngine 系统模板以 _system.txt 结尾
        - 文件不存在时回退到 config/templates/default_system.txt；再缺失则使用内置字符串
        - 加载结果缓存到 self._agent_system_prompts，key 为代理 name
        """
        base_dir = Path(self.config_loader.config_dir).parent  # 项目根目录

        def _read(rel_path: str) -> Optional[str]:
            p = base_dir / rel_path
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                return None

        # 内置兜底模板（与原硬编码内容一致）
        builtin_default = (
            "你是 Hermes 智能体系统中的 {agent_name} 代理。\n"
            "你能够对复杂的任务进行分解、调用工具完成任务和生成见解。\n"
            "请基于提供的信息产出清晰、有帮助的回复，并提供下一步建议。\n\n"
            "用户 ID: {user_id}\n识别的意图: {intent}\n\n{memory_section}"
        )
        default_prompt = _read("config/templates/default_system.txt") or builtin_default

        def _load_one(name: str, tpl: str) -> str:
            if tpl:
                content = _read(tpl)
                if content:
                    return content
                logger.warning(
                    "[HermesEngine] 代理 '%s' 的模板文件不存在: %s，使用默认模板", name, tpl
                )
            else:
                # 没有配置 prompt_template，在 config/templates/ 查找同名文件
                auto_path = f"config/templates/{name}_system.txt"
                content   = _read(auto_path)
                if content:
                    logger.info("[HermesEngine] 自动加载代理 '%s' 的模板: %s", name, auto_path)
                    return content
                logger.warning(
                    "[HermesEngine] 代理 '%s' 未配置 prompt_template 且未找到 %s，使用默认模板",
                    name, auto_path,
                )
            return default_prompt

        # 加载 router
        router_cfg  = self.agents_config.get("router", {})
        router_name = router_cfg.get("name", "router")
        router_tpl  = router_cfg.get("prompt_template", "")
        self._agent_system_prompts[router_name] = _load_one(router_name, router_tpl)

        # 加载所有 workers
        for worker in self.agents_config.get("workers", []) or []:
            name = worker.get("name", "")
            tpl  = worker.get("prompt_template", "")
            if not name:
                continue
            self._agent_system_prompts[name] = _load_one(name, tpl)

        self._agent_system_prompts["__default__"] = default_prompt
        logger.info(
            "已加载 %d 个代理 Prompt 模板: %s",
            len(self._agent_system_prompts),
            list(self._agent_system_prompts.keys()),
        )

    def _get_system_prompt(self, agent_name: str) -> str:
        """按代理名称返回系统 Prompt；未命中则返回默认模板。"""
        return self._agent_system_prompts.get(
            agent_name,
            self._agent_system_prompts.get("__default__", "你是一个智能助手。"),
        )

    def set_memory_manager(self, memory_manager) -> None:
        """注入 MemoryManager。"""
        self.memory_manager = memory_manager
        if self.default_llm and hasattr(memory_manager, "set_default_llm"):
            memory_manager.set_default_llm(self.default_llm)

    def set_rag_pipeline(self, rag_pipeline: "RagPipeline") -> None:
        """注入 RagPipeline（由 main.py 在 VectorStore 初始化后调用）。"""
        self.rag_pipeline = rag_pipeline
        logger.info("RagPipeline 已注入 HermesEngine")
        # 实例化并注入 MemoryArchiverAgent（系统内置，不注册到 registry）
        if hasattr(self.memory_manager, "set_archiver"):
            from app.agents.system.memory_archiver import MemoryArchiverAgent
            self.memory_manager.set_archiver(MemoryArchiverAgent())
            logger.info("✅ MemoryArchiverAgent 已注入 MemoryManager")

    @staticmethod
    def _estimate_context_length(context: Dict[str, Any]) -> int:
        """估算上下文字符总数，用于触发上下文过长的记忆压缩。

        统计 history 中所有 turn 的 user_input + assistant_response 字符数，
        加上注入系统提示词的 memory_text 字符数。
        """
        total = 0
        for turn in context.get("history", []):
            total += len(str(turn.get("user_input", "")))
            total += len(str(turn.get("assistant_response", "")))
        total += len(context.get("memory_text", ""))
        return total

    async def _initialize_default_llm(self) -> None:
        """初始化默认 LLM (使用系统配置中的默认模型)"""
        try:
            logger.info("开始加载默认 LLM 配置...")
            # 尝试从数据库加载默认 LLM 配置
            llm_info = await self._load_llm_info("0")
            if llm_info:
                logger.info(f"默认 LLM 配置已加载: model={llm_info.model_name}")
                self.default_llm = await self._build_llm_from_config(llm_info)
                if self.default_llm:
                    logger.info("✅ 默认 LLM 已成功初始化")
                else:
                    logger.warning("默认 LLM 配置已加载，但构建模型失败")
            else:
                logger.warning("未找到默认 LLM 配置，将动态加载用户 LLM")
        except Exception as e:
            logger.warning(f"初始化默认 LLM 失败: {type(e).__name__}: {e}", exc_info=True)

    def _initialize_router(self) -> None:
        """初始化路由智能体 (基于 LangChain)"""
        self.router_agent = RouterAgent(
            name=self.agent_config.get("name", "router"),
            config=self.agent_config,
            llm=self.default_llm,
            intent_agent_mapping=self.intent_agent_mapping,
        )
        self.agents[self.router_agent.name] = self.router_agent
        logger.info(f"路由智能体已初始化: {self.router_agent.name}")

    def _initialize_workers(self) -> None:
        """初始化工作智能体配置"""
        workers = self.agents_config.get("workers", []) or []
        for worker in workers:
            if not worker.get("enabled", True):
                continue
            name = worker.get("name")
            if not name:
                continue
            self.worker_configs[name] = worker
            self.agents[name] = worker
            logger.info(f"工作智能体已注册: {name}")

    def _initialize_tools(self) -> None:
        """加载工具定义配置并转换为 LangChain Tools"""
        tools = self.agents_config.get("tools", []) or []
        for tool in tools:
            tool_id = tool.get("id")
            if not tool_id:
                continue
            self.tool_configs[tool_id] = tool
            logger.info(f"工具已注册: {tool_id}")

    @lru_cache(maxsize=32)
    def _get_model_provider(self, model_name: str) -> str:
        """根据模型名称推断供应商"""
        model_lower = model_name.lower()
        if "gpt" in model_lower:
            return "openai"
        elif "claude" in model_lower:
            return "anthropic"
        elif "gemini" in model_lower:
            return "google_genai"
        elif "llama" in model_lower:
            return "together"
        else:
            return "openai"  # 默认为 OpenAI

    async def process_user_input(
        self,
        user_id: str,
        user_input: str,
        context: Dict[str, Any],
        llm: Optional[Any] = None,
        agent_name: Optional[str] = None,
    ) -> str:
        """
        处理用户输入的主流程

        Args:
            user_id: 用户 ID
            user_input: 用户输入文本
            context: 上下文信息 (历史消息、检索结果等)
            llm: 可选的 LangChain 模型实例，如果已从 Redis 加载

        Returns:
            str: 最终回复文本
        """
        if self.router_agent is None:
            raise RuntimeError("Router agent has not been initialized")

        if llm is None:
            llm = await self._build_user_llm(user_id)

        if llm is None:
            logger.error(f"无法为用户 {user_id} 获取 LLM")
            return "当前无法调用 LLM，请稍后重试。"

        from app.utils.log_setup import start_turn as _start_turn, end_turn as _end_turn
        _ns_turn_id = ""
        try:
            from app.utils.log_bus import get_bus as _get_bus
            _bus = _get_bus()
            _ctx_pre = context if isinstance(context, dict) else {}
            _bus.user_message(user_id, user_input, _ctx_pre.get("_client_type", "api"))

            turn_id = uuid.uuid4().hex
            _ns_turn_id = turn_id
            _start_turn(turn_id)

            # 使用 RagPipeline 组装上下文（检索 + 历史加载 + 相关性过滤）
            _rag_source = self.rag_pipeline
            if _rag_source is not None:
                try:
                    bundle  = await _rag_source.build_context(
                        user_id=user_id,
                        user_input=user_input,
                        base_context=context if isinstance(context, dict) else {},
                    )
                    context = bundle.to_prompt_context()
                    _bus.context_built(
                        user_id, len(bundle.history), len(bundle.memories),
                        (context if isinstance(context, dict) else {}).get("_client_type", ""),
                    )
                except Exception as e:
                    logger.warning("RAG build_context 失败，使用原始 context: %s", e)
            else:
                # 降级：均未注入时沿用旧的 ES 历史加载
                try:
                    history_size = int(os.getenv("CHAT_HISTORY_SIZE", "5"))
                    history_from_es = await self.chat_history.get_recent_messages(user_id, size=history_size)
                    if history_from_es:
                        ctx_hist = context.get("history") if isinstance(context, dict) else None
                        if isinstance(ctx_hist, list):
                            context["history"] = history_from_es + ctx_hist
                        else:
                            context["history"] = history_from_es
                except Exception as e:
                    logger.warning("降级加载 ES 历史失败: %s", e)

            # 上下文长度检查：超限则立即触发记忆压缩（后台，不阻塞本次请求）
            if self.memory_manager is not None:
                ctx_chars = self._estimate_context_length(context)
                ctx_limit = getattr(self.memory_manager, "context_length_limit", 20_000)
                if ctx_chars > ctx_limit:
                    logger.info(
                        f"上下文过长 {ctx_chars}/{ctx_limit} chars，"
                        f"立即触发记忆压缩 user={user_id}"
                    )
                    asyncio.create_task(
                        self.memory_manager.compress_immediately(user_id, "context_overflow")
                    )

            logger.info(f"开始处理用户输入 (user_id={user_id})")
            logger.debug(f"用户输入: {user_input}")
            logger.debug(f"上下文信息: {context}")

            # 加载用户画像到 context，供子 Agent 系统提示注入
            if isinstance(context, dict) and self.memory_manager is not None:
                if "_user_profile" not in context:
                    try:
                        profile_block = await self.memory_manager.build_system_prompt_block(user_id)
                        if profile_block:
                            context["_user_profile"] = profile_block
                    except Exception as _pe:
                        logger.debug("预加载用户画像失败 user=%s: %s", user_id, _pe)

            # 路由处理（传入本次请求的 LLM 用于意图识别和任务分解）
            router_result = await self.router_agent.process(user_input, context, llm=llm)
            intent       = router_result.get("intent", "general_question")
            mode         = router_result.get("mode", "single")
            pipeline     = router_result.get("pipeline", [])
            tasks        = router_result.get("tasks", [])
            target_agent = router_result.get("target_agent") or self.router_agent.name

            # 用户指定 Agent 时覆盖路由决策
            if agent_name:
                override_ag = await self._get_or_load_agent(agent_name, user_id)
                if override_ag:
                    pipeline     = [{"step": 0, "agent_name": agent_name,
                                     "task": {"task_id": "task_1", "type": intent,
                                              "description": user_input}}]
                    mode         = "single"
                    target_agent = agent_name
                    logger.info("用户指定 Agent: %s，覆盖路由决策", agent_name)
                else:
                    logger.warning("指定的 Agent '%s' 不存在，使用路由决策", agent_name)

            logger.info("路由决策: intent=%s mode=%s target=%s steps=%d",
                        intent, mode, target_agent, len(pipeline))
            logger.debug("路由结果详情: %s", router_result)
            _bus.routing(user_id, intent, mode, target_agent, len(pipeline))

            # 模型适配性检查（日志警告，不阻断）
            model_name = getattr(llm, "model_name", getattr(llm, "model", "")) or ""
            for step in pipeline:
                self.router_agent.check_model_suitability(step["agent_name"], model_name)

            # 写入 Redis dispatch 计划（turn_id 复用为 dispatch_id）
            dispatch_id = turn_id
            await self._dispatch_to_redis(user_id, dispatch_id, pipeline, mode, intent, user_input)

            # 执行 pipeline
            tool_results  = None
            tool_steps:   List[Dict[str, Any]] = []
            agent_outputs: List[Dict[str, str]] = []
            if pipeline and target_agent != self.router_agent.name:
                response, agent_outputs = await self._execute_pipeline(
                    pipeline   =pipeline,
                    mode       =mode,
                    intent     =intent,
                    user_id    =user_id,
                    llm        =llm,
                    context    =context,
                    dispatch_id=dispatch_id,
                    user_input =user_input,
                )
            else:
                # Router 自处理：直接 LLM 回复
                response = await self._generate_llm_response(
                    llm         =llm,
                    user_id     =user_id,
                    user_input  =user_input,
                    intent      =intent,
                    tasks       =tasks,
                    context     =context,
                    agent_name  =target_agent,
                    tool_results=None,
                )
                agent_outputs = [{"agent_name": target_agent or self.router_agent.name, "output": response}]

            # engine 级别记录最终回复（agent 内部单步 llm_output 已记录中间步骤）
            _bus.llm_output(user_id, target_agent, response)

            _ctx_dict     = context if isinstance(context, dict) else {}
            turn_metadata = {
                "intent":         intent,
                "target_agent":   target_agent,
                "tasks":          tasks,
                "tool_results":   tool_results,
                "tool_steps":     tool_steps,
                "mode":           mode,
                "pipeline": [
                    {
                        "step":             p["step"],
                        "agent_name":       p["agent_name"],
                        "task_description": (p.get("task") or {}).get("description", "")[:200],
                    }
                    for p in pipeline
                ],
                "agent_outputs":  agent_outputs or [],
                "client_type":    _ctx_dict.get("_client_type", ""),
                "client_version": _ctx_dict.get("_client_version", ""),
            }

            # 1. 立即写入 ES（保证可检索性）
            try:
                saved_id = await self.chat_history.save_turn(
                    user_id=user_id,
                    user_input=user_input,
                    assistant_response=response,
                    turn_id=turn_id,
                    metadata=turn_metadata,
                )
                logger.info(f"保存对话轮次到 ES: user_id={user_id} turn_id={saved_id or '(失败)'}")
            except Exception as e:
                logger.warning(f"ES save_turn 失败: {e}")

            # 2. 写入 MemoryManager L1（Redis），填充 ContextManager 所依赖的近期对话缓存
            if self.memory_manager is not None:
                try:
                    await self.memory_manager.store_turn(
                        user_id=user_id,
                        turn_id=turn_id,
                        user_input=user_input,
                        assistant_response=response,
                        metadata=turn_metadata,
                        agent_outputs=agent_outputs or None,
                    )
                    logger.debug(f"已写入 MemoryManager L1: user_id={user_id} turn_id={turn_id}")
                    # 后台向量化：通过 RagPipeline 索引本轮对话供后续检索
                    if self.rag_pipeline is not None:
                        asyncio.create_task(self.rag_pipeline.index_turn(
                            user_id=user_id, turn_id=turn_id,
                            user_input=user_input, assistant_response=response,
                            agent_outputs=agent_outputs or None,
                        ))
                    # 后台预取：为下一轮对话提前缓存相关记忆（TTL 5 min）
                    prefetch_src = self.rag_pipeline or self.memory_manager
                    prefetch_src.queue_prefetch(user_id, user_input)
                except Exception as e:
                    logger.warning(f"MemoryManager.store_turn 失败: {e}")

            logger.info(f"用户输入处理完成 (user_id={user_id})")
            logger.debug(f"生成回复: {response}")
            return response
        except _LLMRetryExhausted as e:
            logger.error("LLM 重试耗尽 user=%s: %s", user_id, e.llm_message)
            return f"LLM 访问失败，{e.llm_message}"
        except Exception as e:
            logger.error(f"处理用户输入失败: {e}")
            return "处理请求时发生错误，请稍后重试。"
        finally:
            if _ns_turn_id:
                _end_turn(_ns_turn_id)

    async def _build_user_llm(self, user_id: str) -> Optional[BaseChatModel]:
        """从 MySQL 动态加载用户 LLM 配置并构建 ChatModel 实例，不使用本地缓存。"""
        llm_info = await LLMInfo.load(user_id)
        if llm_info is None:
            return None
        return await self._build_llm_from_config(llm_info)

    async def _load_llm_info(self, user_id: str) -> Optional[LLMInfo]:
        """
        从数据库加载用户 LLM 配置信息

        Args:
            user_id: 用户 ID

        Returns:
            LLMInfo: LLM 信息对象
        """
        try:
            logger.debug(f"正在加载用户 {user_id} 的 LLM 配置...")
            llm_info = await LLMInfo.load(user_id)
            if llm_info:
                logger.debug(f"成功加载用户 {user_id} 的 LLM 配置: model={llm_info.model_name}")
            else:
                logger.debug(f"未找到用户 {user_id} 的 LLM 配置")
            return llm_info
        except Exception as e:
            logger.error(f"查询 LLM 信息失败: {type(e).__name__}: {e}", exc_info=True)
            return None

    async def _build_llm_from_config(self, llm_info: LLMInfo) -> Optional[BaseChatModel]:
        """
        根据 LLMInfo 构建 LangChain ChatModel
        """
        if not llm_info:
            return None

        try:
            is_local = llm_info._is_ollama() or 'http://localhost' in (llm_info.url or '') or '127.0.0.1' in (llm_info.url or '')
            if not llm_info.model_name or (not is_local and not llm_info.api_key):
                logger.error("LLM 配置缺少必要字段: model_name 或 api_key")
                return None

            llm = await llm_info.build_chat_model()
            if llm:
                logger.info(
                    f"LLM 已创建: {llm_info.model_name} "
                    f"(provider={llm_info.provider}, type={llm_info.model_type})"
                )
            return llm
        except Exception as e:
            logger.error(f"构建 LLM 失败: {e}")
            return None

    def clear_llm_cache(self, user_id: Optional[str] = None) -> None:
        """已废弃：LLM 不再缓存，此方法保留以兼容旧调用方。"""

    @staticmethod
    def _fmt_llm_error(exc: Exception) -> str:
        """从 LLM API 异常中提取人读错误消息（优先解析 JSON body）。"""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                data = resp.json() if callable(getattr(resp, "json", None)) else {}
                err = data.get("error", {})
                if isinstance(err, dict):
                    msg = err.get("message", "")
                    if msg:
                        return str(msg)[:300]
                body_msg = data.get("message", "")
                if body_msg:
                    return str(body_msg)[:300]
            except Exception:
                pass
            text = getattr(resp, "text", "") or ""
            return text[:300]
        return str(exc)[:300]

    async def _save_llm_failure(
        self,
        user_id: str,
        pending_input: str,
        completed_content: str,
        llm_message: str,
    ) -> None:
        """将 LLM 失败状态保存到 Redis（TTL 30 分钟），供用户发送「继续」时恢复。"""
        conn = await get_connection("redis", None)
        if conn is None:
            return
        try:
            data = {
                "pending_input":     pending_input,
                "completed_content": completed_content,
                "failed_reason":     llm_message,
                "timestamp":         datetime.now().isoformat(),
            }
            await conn.create(f"llm:failure:{user_id}", data, ttl=1800)
            logger.info("已保存 LLM 失败状态 user=%s input=%r", user_id, pending_input[:50])
        except Exception as e:
            logger.error("保存 LLM 失败状态异常: %s", e)
        finally:
            await release_connection("redis", conn)

    async def _load_llm_failure(self, user_id: str) -> Optional[dict]:
        """从 Redis 加载 LLM 失败状态，加载后立即删除（仅允许使用一次）。"""
        conn = await get_connection("redis", None)
        if conn is None:
            return None
        try:
            key = f"llm:failure:{user_id}"
            data = await conn.read(key)
            if isinstance(data, dict):
                await conn.delete(key)
                return data
            return None
        except Exception as e:
            logger.error("加载 LLM 失败状态异常: %s", e)
            return None
        finally:
            await release_connection("redis", conn)

    def request_cancel(self, stream_id: str) -> bool:
        """请求终止指定流式会话。

        每个 SSE 流在启动时生成唯一 stream_id，仅终止该 stream_id 对应的流，
        不影响同一用户其他终端正在进行的对话。

        Returns:
            bool: True 表示找到活跃流且已发送取消信号，False 表示未找到。
        """
        ev = self._cancel_signals.get(stream_id)
        if ev is not None:
            ev.set()
            logger.info("已发送取消信号 stream_id=%s", stream_id)
            return True
        return False

    def consent_respond(self, request_id: str, decision: str) -> bool:
        """处理前端用户对危险操作的授权决策，恢复被暂停的工具执行。

        Args:
            request_id: ConsentRequiredException.request_id，由 SSE 事件携带
            decision:   "allow" | "deny" | "conversation"

        Returns:
            True 表示找到等待中的 Future 并已解决，False 表示 request_id 无效或已超时
        """
        fut = self._consent_futures.get(request_id)
        if fut and not fut.done():
            fut.set_result(decision)
            logger.info("consent 已应答: request_id=%s decision=%s", request_id[:8], decision)
            return True
        logger.warning("consent_respond: 未找到等待中的请求 request_id=%s", request_id[:8])
        return False

    async def call_tool(self, tool_name: str, **kwargs) -> Any:
        """
        调用工具 (基于 LangChain Tool)

        Args:
            tool_name: 工具名称
            **kwargs: 工具参数

        Returns:
            Any: 工具执行结果
        """
        # 检查是否已缓存 LangChain Tool
        if tool_name not in self.langchain_tools:
            # 加载工具函数
            tool_func = self._load_tool_function(tool_name)
            tool_config = self.tool_configs.get(tool_name)

            if tool_func is None or tool_config is None:
                logger.warning(f"工具加载失败: {tool_name}")
                return None

            # 将函数包装为 LangChain Tool
            langchain_tool = LangChainToolWrapper(tool_name, tool_func, tool_config)
            self.langchain_tools[tool_name] = langchain_tool

        try:
            tool = self.langchain_tools[tool_name]
            # 如果参数为字典，使用 invoke；如果有单个参数，转换为字典
            if not kwargs:
                return await tool._arun()
            return await tool._arun(**kwargs)
        except Exception as e:
            logger.error(f"调用工具 {tool_name} 失败: {e}")
            return None

    def _load_tool_function(self, tool_name: str) -> Optional[Any]:
        """加载工具函数并缓存"""
        if tool_name in self.loaded_tool_functions:
            return self.loaded_tool_functions[tool_name]

        tool_config = self.tool_configs.get(tool_name)
        if not tool_config:
            return None

        tool_path = tool_config.get("path")
        if not tool_path or "." not in tool_path:
            return None

        module_name, func_name = tool_path.rsplit(".", 1)
        try:
            module = importlib.import_module(module_name)
            tool_func = getattr(module, func_name, None)
            if tool_func:
                self.loaded_tool_functions[tool_name] = tool_func
            return tool_func
        except Exception as e:
            logger.error(f"加载工具模块失败: {tool_path}, {e}")
            return None

    def _registry_tools_for_agent(
        self, user_id: str, agent_name: str
    ) -> List["BaseTool"]:
        """从 registry 收集当前 agent 可用的工具并转换为 LangChain Tool。

        收集规则：
          - visibility=public  且 exec_location=server：所有 agent 均可用的通用工具
          - visibility=exclusive 且 owner_agent==agent_name：该 agent 的专属工具
          - exec_location=client 的工具需客户端执行，跳过（LangGraph 不支持代理执行）
        """
        from app.tools.registry import registry as _tool_registry
        from app.tools.base import EXEC_SERVER

        available = _tool_registry.list_available_for(user_id=user_id, agent_name=agent_name)
        adapters: List["BaseTool"] = []
        for t in available:
            if t.exec_location != EXEC_SERVER:
                continue
            adapters.append(_RegistryToolAdapter(t, user_id=user_id, agent_name=agent_name))
        return adapters

    def _get_agent_executor_cache(self, user_id: str) -> AgentExecutorCache:
        if user_id not in self.agent_executor_caches:
            self.agent_executor_caches[user_id] = AgentExecutorCache(
                user_id=user_id,
                llm_factory=self._build_user_llm,
                tool_loader=self._load_tool_function,
                db_alias=None,
            )
        return self.agent_executor_caches[user_id]

    async def _execute_worker(
        self,
        worker_name: str,
        tasks: List[Dict[str, Any]],
        user_id: str,
        context: Dict[str, Any],
    ) -> str:
        """执行 Worker 智能体任务 (使用 LangChain Tool 调用)"""
        worker_config = self.worker_configs.get(worker_name)
        if worker_config is None:
            logger.warning(f"未找到工作智能体配置: {worker_name}")
            return f"未找到智能体 {worker_name}，请稍后重试。"

        results: List[str] = []
        tool_ids = worker_config.get("tools", []) or []

        for task in tasks:
            task_desc = task.get("description", "")
            results.append(f"任务: {task_desc}")

            for tool_id in tool_ids:
                tool_result = await self.call_tool(
                    tool_id,
                    user_id=user_id,
                    task=task,
                    context=context,
                )
                if tool_result is not None:
                    results.append(f"{tool_id} 结果: {tool_result}")

        if not results:
            return f"已将任务分配给 {worker_name}，正在处理。"

        return "\n".join(results)

    @staticmethod
    def _extract_tool_steps(messages: list) -> List[Dict[str, Any]]:
        """从 LangGraph 消息链中提取每次工具调用的名称、参数和输出。

        LangGraph react agent 的消息顺序：
          HumanMessage → AIMessage(tool_calls) → ToolMessage(result) → ... → AIMessage(final)
        ToolMessage 通过 tool_call_id 与对应的 AIMessage tool_call 关联。
        """
        tool_steps: List[Dict[str, Any]] = []
        pending: Dict[str, Dict[str, Any]] = {}  # tool_call_id -> step

        for msg in messages:
            # AIMessage 带有 tool_calls 时表示本轮要调用工具
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        call_id = tc.get("id", "")
                        name = tc.get("name", "")
                        args = tc.get("args", {})
                    else:
                        call_id = getattr(tc, "id", "")
                        name = getattr(tc, "name", "")
                        args = getattr(tc, "args", {})
                    step: Dict[str, Any] = {
                        "tool_name": name,
                        "tool_args": args,
                        "tool_output": None,
                    }
                    tool_steps.append(step)
                    if call_id:
                        pending[call_id] = step

            # ToolMessage 携带工具执行结果，通过 tool_call_id 回填到对应 step
            elif hasattr(msg, "tool_call_id"):
                call_id = getattr(msg, "tool_call_id", "")
                content = getattr(msg, "content", str(msg))
                if call_id and call_id in pending:
                    pending[call_id]["tool_output"] = content
                else:
                    tool_steps.append({
                        "tool_name": "unknown",
                        "tool_args": {},
                        "tool_output": content,
                    })

        return tool_steps

    async def _execute_worker_with_tools(
        self,
        worker_name: str,
        tasks: List[Dict[str, Any]],
        user_id: str,
        context: Dict[str, Any],
        llm: BaseChatModel,
        extra_tools: Optional[List[str]] = None,
    ) -> tuple:
        """使用 LangGraph 执行 Worker 智能体任务。

        Args:
            extra_tools: 事件循环动态注入的额外工具 ID 列表（已在 langchain_tools 缓存中）

        Returns:
            (result_str, tool_steps): 执行结果字符串 + 工具调用步骤列表
        """
        worker_config = self.worker_configs.get(worker_name) or {}
        if not worker_config and not extra_tools:
            logger.warning(f"未找到工作智能体配置: {worker_name}")
            return f"未找到智能体 {worker_name}，请稍后重试。", []

        # 缓存 key 含 user_id：不同用户可能有不同的 private/exclusive 工具集
        executor_key = f"{worker_name}::{user_id}"

        # 有 extra_tools 时强制清除旧图缓存，确保以完整工具集重建
        if extra_tools:
            self.agent_graphs.pop(executor_key, None)

        if executor_key not in self.agent_graphs and HAS_LANGGRAPH:
            # ── 1. YAML 配置工具 + 动态注入工具 ──────────────────────────────
            tool_ids: List[str] = list(worker_config.get("tools", []) or [])
            for t in (extra_tools or []):
                if t not in tool_ids:
                    tool_ids.append(t)

            langchain_tools: List[BaseTool] = []
            yaml_tool_names: set = set()
            for tool_id in tool_ids:
                if tool_id not in self.langchain_tools:
                    tool_func   = self._load_tool_function(tool_id)
                    tool_config = self.tool_configs.get(tool_id)
                    if tool_func and tool_config:
                        self.langchain_tools[tool_id] = LangChainToolWrapper(
                            tool_id, tool_func, tool_config
                        )
                if tool_id in self.langchain_tools:
                    langchain_tools.append(self.langchain_tools[tool_id])
                    yaml_tool_names.add(tool_id)

            # ── 2. registry 工具：公共工具 + 该 agent 专属工具 ───────────────
            try:
                registry_lc_tools = self._registry_tools_for_agent(user_id, worker_name)
                for rt in registry_lc_tools:
                    if rt.name not in yaml_tool_names:   # 避免与 YAML 工具重名
                        langchain_tools.append(rt)
                if registry_lc_tools:
                    logger.debug(
                        "[Engine] %s 注入 registry 工具: %s",
                        worker_name,
                        [rt.name for rt in registry_lc_tools],
                    )
            except Exception as e:
                logger.warning("[Engine] 加载 registry 工具失败 agent=%s: %s", worker_name, e)

            if langchain_tools:
                try:
                    # 优先使用 registry agent 的完整背景作为系统提示，并注入 client_env + user_profile
                    from app.agents.registry import registry as _ag_registry
                    _ag = _ag_registry.get(worker_name)
                    if _ag and _ag.background:
                        system_prompt = _ag._build_system_prompt(context=context)
                    else:
                        agent_role    = worker_config.get("role", worker_name)
                        system_prompt = f"你是 {agent_role} 代理，负责执行分配的任务。"
                        # 注入 client_env + user_profile（无 BaseAgent 实例时手动拼接）
                        from app.utils.client_env import format_env_for_prompt
                        _ctx = context if isinstance(context, dict) else {}
                        _env_block = format_env_for_prompt(
                            _ctx.get("_client_type"), _ctx.get("_client_version")
                        )
                        _profile_block = _ctx.get("_user_profile", "")
                        if _env_block:
                            system_prompt += "\n\n" + _env_block
                        if _profile_block:
                            system_prompt += "\n\n" + _profile_block
                    graph = create_react_agent(llm, langchain_tools, prompt=system_prompt)
                    self.agent_graphs[executor_key] = graph
                    logger.info(
                        "[Engine] 为 %s 构建 LangGraph（工具总数=%d：yaml=%d registry=%d）",
                        worker_name, len(langchain_tools),
                        len(yaml_tool_names), len(langchain_tools) - len(yaml_tool_names),
                    )
                except Exception as e:
                    logger.warning(f"创建 LangGraph 失败: {e}，使用简单执行模式")
                    result = await self._execute_worker(worker_name, tasks, user_id, context)
                    return result, []

        # 使用图执行任务
        try:
            graph = self.agent_graphs.get(executor_key)
            if graph is None:
                result = await self._execute_worker(worker_name, tasks, user_id, context)
                return result, []

            task_desc = ", ".join([t.get("description", "") for t in tasks])

            # 剥离 event loop 注入的工具请求说明（LangGraph 直接调用工具，无需此 schema）
            _TRS_MARKER = "\n\n如果你需要一个当前不存在的专用工具"
            if _TRS_MARKER in task_desc:
                task_desc = task_desc[:task_desc.index(_TRS_MARKER)]

            logger.debug("LangGraph调用: worker=%s task_desc=%r", worker_name, task_desc[:120])
            _invoke_msg = {"messages": [HumanMessage(content=f"请执行以下任务: {task_desc}")]}
            _last_ag_err = ""
            for _attempt in range(3):
                try:
                    raw = await graph.ainvoke(_invoke_msg)
                    _last_ag_err = ""
                    break
                except _LLM_RETRYABLE_ERRORS as _e:
                    _last_ag_err = self._fmt_llm_error(_e)
                    logger.warning(
                        "Agent LLM 调用失败 (第 %d/3 次) agent=%s: %s",
                        _attempt + 1, worker_name, _last_ag_err,
                    )
                    if _attempt < 2:
                        await asyncio.sleep(2.0 * (_attempt + 1))
            if _last_ag_err:
                raise _LLMRetryExhausted(_last_ag_err)

            messages   = raw.get("messages", []) if isinstance(raw, dict) else []
            tool_steps = self._extract_tool_steps(messages)

            # 从消息链中提取最终 AI 回复（最后一条无 tool_calls 的 AIMessage）
            result_str = ""
            if messages:
                for m in reversed(messages):
                    content = getattr(m, "content", None)
                    if content and not getattr(m, "tool_call_id", None) and not getattr(m, "tool_calls", None):
                        result_str = content if isinstance(content, str) else str(content)
                        break
            if not result_str:
                result_str = str(raw.get("output", raw)) if isinstance(raw, dict) else str(raw)

            logger.debug(
                "LangGraph完成: worker=%s tool_steps=%d result_preview=%r",
                worker_name, len(tool_steps), result_str[:150],
            )
            for i, step in enumerate(tool_steps):
                logger.debug(
                    "  工具步骤[%d]: name=%s args=%s output_preview=%r",
                    i, step.get("tool_name"), step.get("tool_args"),
                    str(step.get("tool_output", ""))[:100],
                )

            return result_str, tool_steps

        except _LLMRetryExhausted:
            raise  # 重试耗尽，终止整条流
        except Exception as e:
            logger.error(f"LangGraph 执行失败: {e}", exc_info=True)
            result = await self._execute_worker(worker_name, tasks, user_id, context)
            return result, []

    async def _generate_llm_response(
        self,
        llm: BaseChatModel,
        user_id: str,
        user_input: str,
        intent: str,
        tasks: List[Dict[str, Any]],
        context: Dict[str, Any],
        agent_name: str,
        tool_results: Any = None,
    ) -> str:
        """
        使用 LangChain ChatModel 生成最终回复 (基于 LCEL)

        使用 LangChain Expression Language (LCEL) 构建高效的处理链

        Args:
            llm: LangChain 聊天模型
            user_id: 用户 ID
            user_input: 用户输入
            intent: 识别的意图
            tasks: 任务列表
            context: 上下文
            agent_name: 代理名称
            tool_results: 工具执行结果

        Returns:
            str: 生成的回复
        """
        if llm is None:
            return "当前无法调用 LLM，请稍后重试。"

        try:
            from app.utils.client_env import format_env_for_prompt as _fmt_env
            # 用户画像注入：优先从 context["_user_profile"] 取（process_user_input 已预加载），
            # 降级时再从 Redis 加载。
            base_sys_prompt = self._get_system_prompt(agent_name)
            _ctx_dict_gen = context if isinstance(context, dict) else {}
            profile_block = _ctx_dict_gen.get("_user_profile", "")
            if not profile_block and self.memory_manager is not None:
                try:
                    profile_block = await self.memory_manager.build_system_prompt_block(user_id)
                except Exception as _pe:
                    logger.debug(f"加载用户画像失败 user={user_id}: {_pe}")
            if profile_block:
                base_sys_prompt = base_sys_prompt + "\n\n" + profile_block
            # 客户端环境注入
            env_block = _fmt_env(
                _ctx_dict_gen.get("_client_type"), _ctx_dict_gen.get("_client_version")
            )
            if env_block:
                base_sys_prompt = base_sys_prompt + "\n\n" + env_block

            # 使用 ChatPromptTemplate 构建提示 (LCEL 方式)
            system_prompt = ChatPromptTemplate.from_messages([
                ("system", base_sys_prompt),
                ("human", """{user_input}
                    当前任务:
                    {tasks_summary}

                    工具执行结果:
                    {tool_results_summary}

                    背景信息:
                    {context_summary}"""),
            ])

            # 准备上下文变量
            tasks_summary        = "\n".join([f"- {t.get('description', '')}" for t in tasks]) if tasks else "- 无"
            tool_results_summary = str(tool_results) if tool_results else "- 无"

            # 剥离 memories / memory_text 后再序列化，避免重复/冗余
            ctx_for_summary = {k: v for k, v in (context or {}).items()
                               if k not in ("memories", "memory_text", "history")}
            context_summary = json.dumps(ctx_for_summary, ensure_ascii=False, indent=2) if ctx_for_summary else "- 无"

            # memory_text 由 ContextManager 生成；为空时不展示该段
            memory_text    = (context or {}).get("memory_text", "") if isinstance(context, dict) else ""
            memory_section = memory_text if memory_text else ""

            # 使用 LCEL 构建链: PromptTemplate | LLM | OutputParser
            chain: Runnable = system_prompt | llm | StrOutputParser()

            logger.debug(
                "LLM请求: user=%s agent=%s intent=%s | "
                "memory_len=%d tasks=%d has_tool_results=%s",
                user_id, agent_name, intent,
                len(memory_section), len(tasks), bool(tool_results),
            )
            if memory_section:
                logger.debug("注入记忆:\n%s", memory_section)
            logger.debug("任务摘要: %s", tasks_summary)
            if tool_results:
                logger.debug("工具结果: %s", tool_results_summary)

            # 调用链（含 3 次重试，针对 LLM 返回非 200 或连接失败的场景）
            _payload = {
                "agent_name":           agent_name,
                "user_id":              user_id,
                "intent":               intent,
                "user_input":           user_input,
                "tasks_summary":        tasks_summary,
                "tool_results_summary": tool_results_summary,
                "context_summary":      context_summary,
                "memory_section":       memory_section,
            }
            _last_err = ""
            for _attempt in range(3):
                try:
                    response = await chain.ainvoke(_payload)
                    _last_err = ""
                    break
                except _LLM_RETRYABLE_ERRORS as _e:
                    _last_err = self._fmt_llm_error(_e)
                    logger.warning(
                        "LLM 调用失败 (第 %d/3 次) user=%s: %s", _attempt + 1, user_id, _last_err
                    )
                    if _attempt < 2:
                        await asyncio.sleep(2.0 * (_attempt + 1))
            if _last_err:
                raise _LLMRetryExhausted(_last_err)

            logger.info(f"LLM 已生成回复 (user_id={user_id}, agent={agent_name}, intent={intent})")
            logger.debug("LLM输出 user=%s len=%d:\n%s", user_id, len(response), response)
            return response

        except _LLMRetryExhausted:
            raise  # 传播到调用方，由上层决定如何处理
        except Exception as e:
            logger.error(f"LLM 生成回复失败: {e}")
            return "LLM 生成失败，请稍后重试。"

    async def _generate_llm_response_stream(
        self,
        llm: BaseChatModel,
        user_id: str,
        user_input: str,
        intent: str,
        tasks: List[Dict[str, Any]],
        context: Dict[str, Any],
        agent_name: str,
    ):
        """流式生成 LLM 回复，yield 文本块。"""
        if llm is None:
            yield "当前无法调用 LLM，请稍后重试。"
            return
        try:
            from app.utils.client_env import format_env_for_prompt as _fmt_env_s
            # 用户画像注入：优先从 context["_user_profile"] 取，降级时再从 Redis 加载。
            base_sys_prompt = self._get_system_prompt(agent_name)
            _ctx_dict_str = context if isinstance(context, dict) else {}
            profile_block = _ctx_dict_str.get("_user_profile", "")
            if not profile_block and self.memory_manager is not None:
                try:
                    profile_block = await self.memory_manager.build_system_prompt_block(user_id)
                except Exception as _pe:
                    logger.debug(f"[stream] 加载用户画像失败 user={user_id}: {_pe}")
            if profile_block:
                base_sys_prompt = base_sys_prompt + "\n\n" + profile_block
            # 客户端环境注入
            env_block_s = _fmt_env_s(
                _ctx_dict_str.get("_client_type"), _ctx_dict_str.get("_client_version")
            )
            if env_block_s:
                base_sys_prompt = base_sys_prompt + "\n\n" + env_block_s

            system_prompt = ChatPromptTemplate.from_messages([
                ("system", base_sys_prompt),
                ("human", """{user_input}

当前任务:
{tasks_summary}

背景信息:
{context_summary}"""),
            ])
            tasks_summary = "\n".join([f"- {t.get('description', '')}" for t in tasks]) if tasks else "- 无"
            ctx_for_summary = {k: v for k, v in (context or {}).items()
                               if k not in ("memories", "memory_text", "history")}
            context_summary = json.dumps(ctx_for_summary, ensure_ascii=False, indent=2) if ctx_for_summary else "- 无"
            memory_text = (context or {}).get("memory_text", "") if isinstance(context, dict) else ""
            memory_section = memory_text if memory_text else ""

            chain: Runnable = system_prompt | llm | StrOutputParser()
            _stream_payload = {
                "agent_name":      agent_name,
                "user_id":         user_id,
                "intent":          intent,
                "user_input":      user_input,
                "tasks_summary":   tasks_summary,
                "context_summary": context_summary,
                "memory_section":  memory_section,
            }
            # 3 次重试（非 200 / 连接失败场景下，error 发生在流开始前，重试安全）
            _last_err = ""
            for _attempt in range(3):
                try:
                    async for chunk in chain.astream(_stream_payload):
                        yield chunk
                    _last_err = ""
                    break
                except _LLM_RETRYABLE_ERRORS as _e:
                    _last_err = self._fmt_llm_error(_e)
                    logger.warning(
                        "流式 LLM 失败 (第 %d/3 次) user=%s: %s", _attempt + 1, user_id, _last_err
                    )
                    if _attempt < 2:
                        await asyncio.sleep(2.0 * (_attempt + 1))
            if _last_err:
                raise _LLMRetryExhausted(_last_err)
        except _LLMRetryExhausted:
            raise  # 传播到 process_user_input_stream._run() 的消费方
        except Exception as e:
            logger.error(f"流式 LLM 生成失败: {e}")
            yield "LLM 生成失败，请稍后重试。"

    async def process_user_input_stream(
        self,
        user_id: str,
        user_input: str,
        context: Dict[str, Any],
        llm=None,
        agent_name: Optional[str] = None,
    ):
        """流式处理用户输入，yield SSE 格式事件字符串。

        新增事件类型（在原有 routing/token/done/error 基础上）：
          planning     — 路由完成，返回完整 pipeline 计划
          agent_start  — 某 Agent 开始执行
          step_start   — Agent 内部 L2 子步骤开始
          step_done    — Agent 内部 L2 子步骤完成
          tool_call    — 工具调用开始
          tool_result  — 工具调用成功
          tool_error   — 工具调用失败
          agent_done   — Agent 执行完成
          cancelled    — 对话已被前端终止
        """
        import json as _json
        from app.utils import progress_bus as _pb

        if self.router_agent is None:
            yield 'event: error\ndata: {"message": "Router not initialized"}\n\n'
            return

        if llm is None:
            llm = await self._build_user_llm(user_id)
        if llm is None:
            yield 'event: error\ndata: {"message": "LLM unavailable"}\n\n'
            return

        # ── 取消信号（每个流唯一 stream_id，互相隔离） ─────────────────────────
        stream_id = uuid.uuid4().hex
        cancel_ev = asyncio.Event()
        self._cancel_signals[stream_id] = cancel_ev
        yield f'event: stream_start\ndata: {{"stream_id": "{stream_id}"}}\n\n'

        # ── 进度队列：所有进度事件（含 token）均经此队列流出 ─────────────────────
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        _pb.set_queue(q)   # ContextVar 在当前 coroutine 及其 child-task 中均可见

        turn_id = uuid.uuid4().hex

        # ── 后台处理任务：执行完整 pipeline 并将所有事件放入队列 ─────────────────
        async def _run() -> None:
            nonlocal user_input, context
            import time as _time
            from app.utils.log_setup import start_turn as _start_turn, end_turn as _end_turn
            _start_turn(turn_id)
            _turn_start = _time.monotonic()
            try:
                # ── 检测「继续」命令，恢复上次 LLM 失败的中断任务 ───────────────
                if user_input.strip() in ("继续", "continue"):
                    _failure = await self._load_llm_failure(user_id)
                    if _failure:
                        _orig_input = _failure.get("pending_input", "")
                        _completed  = _failure.get("completed_content", "")
                        if _orig_input:
                            logger.info(
                                "检测到「继续」命令，恢复中断任务 user=%s input=%r",
                                user_id, _orig_input[:60],
                            )
                            user_input = _orig_input
                            if _completed and isinstance(context, dict):
                                context = dict(context)
                                context["_resume_hint"] = (
                                    f"上次因 LLM 故障中断，已完成部分：\n{_completed[:500]}\n\n"
                                    "请继续完成剩余工作。"
                                )

                from app.utils.log_bus import get_bus as _get_bus
                _bus = _get_bus()
                _ctx_pre = context if isinstance(context, dict) else {}
                _bus.user_message(user_id, user_input, _ctx_pre.get("_client_type", "api"))

                # ── RAG 上下文组装 ──────────────────────────────────────────────
                ctx = context
                _rag_source = self.rag_pipeline
                if _rag_source is not None:
                    try:
                        bundle = await _rag_source.build_context(
                            user_id=user_id, user_input=user_input,
                            base_context=ctx if isinstance(ctx, dict) else {},
                        )
                        ctx = bundle.to_prompt_context()
                        _bus.context_built(
                            user_id, len(bundle.history), len(bundle.memories),
                            (ctx if isinstance(ctx, dict) else {}).get("_client_type", ""),
                        )
                    except Exception as e:
                        logger.warning("RAG build_context 失败 [stream]: %s", e)

                # 上下文长度检查
                if self.memory_manager is not None:
                    ctx_chars = self._estimate_context_length(ctx)
                    ctx_limit = getattr(self.memory_manager, "context_length_limit", 20_000)
                    if ctx_chars > ctx_limit:
                        asyncio.create_task(
                            self.memory_manager.compress_immediately(user_id, "context_overflow")
                        )

                # 加载用户画像
                if isinstance(ctx, dict) and self.memory_manager is not None:
                    if "_user_profile" not in ctx:
                        try:
                            pb = await self.memory_manager.build_system_prompt_block(user_id)
                            if pb:
                                ctx["_user_profile"] = pb
                        except Exception as _pe:
                            logger.debug("[stream] 预加载用户画像失败 user=%s: %s", user_id, _pe)

                # ── 路由 ────────────────────────────────────────────────────────
                if cancel_ev.is_set():
                    q.put_nowait({"event": "cancelled", "data": {"turn_id": turn_id}})
                    return

                router_result = await self.router_agent.process(user_input, ctx, llm=llm)
                intent        = router_result.get("intent", "general_question")
                mode          = router_result.get("mode", "single")
                pipeline      = router_result.get("pipeline", [])
                tasks         = router_result.get("tasks", [])
                target_agent  = router_result.get("target_agent") or self.router_agent.name

                if agent_name:
                    override_ag = await self._get_or_load_agent(agent_name, user_id)
                    if override_ag:
                        pipeline     = [{"step": 0, "agent_name": agent_name,
                                         "task": {"task_id": "task_1", "type": intent,
                                                  "description": user_input}}]
                        mode         = "single"
                        target_agent = agent_name

                _bus.routing(user_id, intent, mode, target_agent, len(pipeline))

                # planning 事件：完整 pipeline 计划
                q.put_nowait({"event": "planning", "data": {
                    "intent":  intent,
                    "mode":    mode,
                    "agent":   target_agent,
                    "pipeline": [
                        {
                            "step":        p["step"],
                            "agent_name":  p["agent_name"],
                            "description": (p.get("task") or {}).get("description", "")[:100],
                        }
                        for p in pipeline
                    ],
                }})

                # ── Pipeline 执行 ────────────────────────────────────────────────
                if cancel_ev.is_set():
                    self._cancelled_turns.add(turn_id)
                    q.put_nowait({"event": "cancelled", "data": {"turn_id": turn_id}})
                    return

                dispatch_id = turn_id
                await self._dispatch_to_redis(user_id, dispatch_id, pipeline, mode, intent, user_input)

                # ── 危险操作授权 Hook ────────────────────────────────────────────
                from app.tools.permission import (
                    set_consent_hook, set_consent_turn_id, consent_manager,
                    _CONSENT_HOOK, _CONSENT_TURN_ID,
                )
                from app.tools.base import ConsentRequiredException, CONSENT_SESSION

                # 每轮请求独立的串行化锁：保证并行 agent 或多工具调用时
                # 同一时刻只有一个授权弹窗处于等待状态，避免授权冲突。
                _consent_lock = asyncio.Lock()

                async def _consent_hook(exc: ConsentRequiredException) -> str:
                    """暂停执行，向前端推送授权请求，等待用户决策。

                    通过 _consent_lock 串行化并发授权请求：
                    - 并行执行的多个 agent 同时触发危险操作时，依次弹窗而非同时弹出
                    - 获取锁后先复查授权状态，若已由同一 turn 的其他工具调用授权则跳过弹窗
                    - 工具执行的超时计时在授权等待期间不计：授权完成后工具才真正开始执行
                    """
                    async with _consent_lock:
                        # 获取锁后复查：并发的其他工具调用可能已通过 conversation 级别授权
                        already = await consent_manager.check_consented(
                            exc.tool_name, exc.operation, exc.user_id,
                            exc.session_id, exc.project_id,
                        )
                        if already:
                            logger.debug(
                                "consent 已由并发请求授权，跳过弹窗: tool=%s op=%s",
                                exc.tool_name, exc.operation,
                            )
                            return "allow"

                        loop = asyncio.get_event_loop()
                        fut: asyncio.Future = loop.create_future()
                        self._consent_futures[exc.request_id] = fut
                        q.put_nowait({"event": "consent_required", "data": exc.to_dict()})
                        try:
                            decision = await asyncio.wait_for(
                                asyncio.shield(fut), timeout=300
                            )
                        except asyncio.TimeoutError:
                            logger.warning("consent 等待超时 request_id=%s", exc.request_id)
                            decision = "deny"
                        finally:
                            self._consent_futures.pop(exc.request_id, None)

                        if decision == "session":
                            # 会话级别授权：一般危险操作本会话内不再询问
                            # 极危险操作（CRITICAL_OPS）由 check_consented 跳过 session 缓存，仍会弹窗
                            await consent_manager.grant_consent(
                                exc.tool_name, exc.operation, exc.user_id,
                                CONSENT_SESSION, session_id=exc.session_id,
                            )
                        elif decision == "conversation":
                            # 全量放行本轮所有危险操作，后续弹窗不再出现
                            consent_manager.grant_conversation_all(turn_id)
                        # "allow": 一次性允许，工具直接执行，无需持久化
                        # "deny":  _wrapped_execute 返回拒绝结果，无需任何授权

                        logger.info(
                            "consent 决策: tool=%s op=%s decision=%s",
                            exc.tool_name, exc.operation, decision,
                        )
                        _bus.consent_decision(exc.user_id, exc.tool_name, exc.operation, decision)
                        return decision

                _hook_token  = set_consent_hook(_consent_hook)
                _turn_token  = set_consent_turn_id(turn_id)

                pipeline_response = ""
                agent_outputs: List[Dict[str, str]] = []
                _pipeline_llm_err: Optional[_LLMRetryExhausted] = None
                try:
                    if pipeline and target_agent != self.router_agent.name:
                        pipeline_response, agent_outputs = await self._execute_pipeline(
                            pipeline=pipeline, mode=mode, intent=intent,
                            user_id=user_id, llm=llm, context=ctx,
                            dispatch_id=dispatch_id, user_input=user_input,
                        )
                except _LLMRetryExhausted as _le:
                    _pipeline_llm_err = _le
                finally:
                    _CONSENT_HOOK.reset(_hook_token)
                    _CONSENT_TURN_ID.reset(_turn_token)

                if _pipeline_llm_err is not None:
                    asyncio.create_task(self._save_llm_failure(
                        user_id, user_input, pipeline_response, _pipeline_llm_err.llm_message
                    ))
                    q.put_nowait({"event": "llm_failure", "data": {
                        "message": f"LLM 访问失败，{_pipeline_llm_err.llm_message}",
                        "hint":    "输入「继续」可恢复未完成的任务",
                    }})
                    return

                # ── 最终 LLM 流式输出 ─────────────────────────────────────────────
                if cancel_ev.is_set():
                    self._cancelled_turns.add(turn_id)
                    q.put_nowait({"event": "cancelled", "data": {"turn_id": turn_id}})
                    return

                full_response_parts: List[str] = []
                scrubber = StreamingContextScrubber()

                ctx_for_stream = dict(ctx) if isinstance(ctx, dict) else {}
                if pipeline_response:
                    # 有 pipeline 结果：流式综合为最终回复
                    ctx_for_stream["_pipeline_result"] = pipeline_response
                elif agent_outputs:
                    # Agent 执行了但未产生有效输出 → 告知 LLM 如实报告失败
                    failed_agents = ", ".join(ao.get("agent_name", "?") for ao in agent_outputs)
                    ctx_for_stream["_pipeline_result"] = (
                        f"[执行结果] Agent（{failed_agents}）执行完毕，但未产生有效输出。"
                        "请如实告知用户任务未能完成，简要说明可能原因，不要虚构完成情况。"
                    )
                # else: 无 pipeline（Router 自处理）：不注入 _pipeline_result，直接回复

                stream_gen = self._generate_llm_response_stream(
                    llm=llm, user_id=user_id, user_input=user_input,
                    intent=intent, tasks=tasks, context=ctx_for_stream,
                    agent_name=target_agent,
                )

                try:
                    async for chunk in stream_gen:
                        if cancel_ev.is_set():
                            break
                        visible = scrubber.feed(chunk)
                        if visible:
                            full_response_parts.append(visible)
                            q.put_nowait({"event": "token", "data": {"text": visible}})
                except _LLMRetryExhausted as _le:
                    asyncio.create_task(self._save_llm_failure(
                        user_id, user_input,
                        pipeline_response or "".join(full_response_parts),
                        _le.llm_message,
                    ))
                    q.put_nowait({"event": "llm_failure", "data": {
                        "message": f"LLM 访问失败，{_le.llm_message}",
                        "hint":    "输入「继续」可恢复未完成的任务",
                    }})
                    return

                trailing = scrubber.flush()
                if trailing and not cancel_ev.is_set():
                    full_response_parts.append(trailing)
                    q.put_nowait({"event": "token", "data": {"text": trailing}})

                if cancel_ev.is_set():
                    self._cancelled_turns.add(turn_id)
                    q.put_nowait({"event": "cancelled", "data": {"turn_id": turn_id}})
                    return

                full_response = "".join(full_response_parts) or pipeline_response
                _bus.llm_output(user_id, target_agent, full_response)

                # ── 对话轮次摘要日志 ──────────────────────────────────────────────
                _agents_used = [p["agent_name"] for p in pipeline] if pipeline else [target_agent]
                _tools_called: List[str] = []
                for _ao in (agent_outputs or []):
                    for _step in (_ao.get("steps") or []):
                        _tn = _step.get("tool_name") or _step.get("tool")
                        if _tn and _tn not in _tools_called:
                            _tools_called.append(_tn)
                _sctx = ctx if isinstance(ctx, dict) else {}
                _bus.conversation_turn(
                    user_id=user_id,
                    turn_id=turn_id,
                    user_message=user_input,
                    intent=intent,
                    mode=mode,
                    agents_used=_agents_used,
                    tools_called=_tools_called,
                    response_len=len(full_response),
                    elapsed_ms=(_time.monotonic() - _turn_start) * 1000,
                    client_type=_sctx.get("_client_type", "api"),
                )

                # 异步保存（_save_turn_async 会检查 _cancelled_turns 再决定是否写入）
                asyncio.create_task(self._save_turn_async(
                    user_id=user_id, user_input=user_input,
                    response=full_response, turn_id=turn_id,
                    intent=intent, target_agent=target_agent,
                    tasks=tasks, mode=mode, pipeline=pipeline,
                    agent_outputs=agent_outputs,
                    client_type=_sctx.get("_client_type", ""),
                    client_version=_sctx.get("_client_version", ""),
                ))

                q.put_nowait({"event": "done", "data": {"turn_id": turn_id}})

            except Exception as e:
                logger.error("流式处理失败 user=%s: %s", user_id, e, exc_info=True)
                q.put_nowait({"event": "error", "data": {"message": str(e)}})
            finally:
                _end_turn(turn_id)
                self._cancel_signals.pop(stream_id, None)
                q.put_nowait(None)  # 哨兵：通知生成器退出

        # 后台任务继承当前 context（含 ContextVar 进度队列）
        asyncio.create_task(_run())

        # ── 消费进度队列，按序 yield SSE 事件 ─────────────────────────────────
        while True:
            item = await q.get()
            if item is None:
                break
            yield (
                f'event: {item["event"]}\n'
                f'data: {_json.dumps(item["data"], ensure_ascii=False)}\n\n'
            )

    async def _save_turn_async(
        self,
        user_id: str, user_input: str, response: str,
        turn_id: str, intent: str, target_agent: str,
        tasks: list, mode: str, pipeline: list,
        agent_outputs: Optional[list] = None,
        client_type: str = "", client_version: str = "",
    ) -> None:
        """后台保存对话轮次到 ES 和 MemoryManager。取消的轮次直接跳过。"""
        if turn_id in self._cancelled_turns:
            self._cancelled_turns.discard(turn_id)
            logger.info("对话已取消，跳过存档 user=%s turn_id=%s", user_id, turn_id)
            return
        turn_metadata = {
            "intent": intent, "target_agent": target_agent,
            "tasks": tasks, "mode": mode,
            "pipeline": [
                {
                    "step":             p["step"],
                    "agent_name":       p["agent_name"],
                    "task_description": (p.get("task") or {}).get("description", "")[:200],
                }
                for p in pipeline
            ],
            "agent_outputs":  agent_outputs or [],
            "client_type":    client_type,
            "client_version": client_version,
        }
        try:
            await self.chat_history.save_turn(
                user_id=user_id, user_input=user_input,
                assistant_response=response, turn_id=turn_id, metadata=turn_metadata,
            )
        except Exception as e:
            logger.warning(f"流式存档 ES 失败: {e}")
        if self.memory_manager is not None:
            try:
                await self.memory_manager.store_turn(
                    user_id=user_id, turn_id=turn_id,
                    user_input=user_input, assistant_response=response,
                    metadata=turn_metadata,
                )
                # 后台向量化：通过 RagPipeline 索引本轮对话供后续检索
                if self.rag_pipeline is not None:
                    asyncio.create_task(self.rag_pipeline.index_turn(
                        user_id=user_id, turn_id=turn_id,
                        user_input=user_input, assistant_response=response,
                    ))
                # 后台预取：为下一轮对话提前缓存相关记忆（TTL 5 min）
                prefetch_src = self.rag_pipeline or self.memory_manager
                prefetch_src.queue_prefetch(user_id, user_input)
            except Exception as e:
                logger.warning(f"流式存档 MemoryManager 失败: {e}")

    def _build_router_response(self, intent: str, tasks: List[Dict[str, Any]]) -> str:
        """构建路由器默认回复"""
        if not tasks:
            return "已接收请求，正在处理。"

        task_descriptions = [task.get("description", "") for task in tasks]
        return (
            f"已识别意图: {intent}。"
            f" 当前任务: {task_descriptions}."
            "如需更多操作，请补充说明。"
        )

    # ── Agent 框架 Pipeline 方法 ────────────────────────────────────────────

    async def _get_or_load_agent(self, agent_name: str, user_id: str):
        """从 registry 查找 agent；不存在时从 DB 加载用户可见的 agent 并注册。"""
        from app.agents.registry import registry
        from app.agents.base import BaseAgent as _BaseAgent

        ag = registry.get(agent_name)
        if ag:
            return ag

        conn = await get_connection("mysql", None)
        try:
            df = await conn.execute_raw(
                "SELECT id, agent_name, job, `desc`, `public`, user_id "
                "FROM agents WHERE agent_name = :name AND state = 1 "
                "AND (`public` = 1 OR user_id = :uid)",
                {"name": agent_name, "uid": user_id},
            )
            if df is None or len(df) == 0:
                return None
            row      = df.iloc[0]
            desc_data = json.loads(row["desc"]) if row.get("desc") else {}
            ag = _BaseAgent(
                name      =str(row["agent_name"]),
                role      =str(row.get("job", "")),
                background=str(desc_data.get("background", "")),
                tools     =list(desc_data.get("tools", [])),
                is_public =bool(row.get("public", 0)),
                source    ="db",
                user_id   =str(row.get("user_id", "0")),
                db_id     =int(row["id"]),
            )
            registry.register(ag)
            return ag
        except Exception as e:
            logger.warning("_get_or_load_agent 失败 name=%s: %s", agent_name, e)
            return None
        finally:
            await release_connection("mysql", conn)

    async def _dispatch_to_redis(
        self,
        user_id: str,
        dispatch_id: str,
        pipeline: List[Dict[str, Any]],
        mode: str,
        intent: str,
        user_input: str,
    ) -> None:
        """将分发计划写入 Redis，TTL 24h。"""
        conn = None
        try:
            conn = await get_connection("redis", None)
            payload = {
                "dispatch_id": dispatch_id,
                "user_id":     user_id,
                "intent":      intent,
                "mode":        mode,
                "user_input":  user_input[:200],
                "pipeline": [
                    {"step": p["step"], "agent_name": p["agent_name"], "status": "pending"}
                    for p in pipeline
                ],
                "created_at": datetime.now().isoformat(),
            }
            key = f"dispatch:{user_id}:{dispatch_id}"
            await conn.create(key, payload, ttl=86400)
        except Exception as e:
            logger.warning("写入 Redis dispatch 失败: %s", e)
        finally:
            if conn:
                await release_connection("redis", conn)

    async def _update_dispatch_step(
        self,
        user_id: str,
        dispatch_id: str,
        step: int,
        status: str,
        result: str,
        overall_done: bool,
    ) -> None:
        """更新 Redis 中某个分发步骤的状态。"""
        conn = None
        try:
            conn = await get_connection("redis", None)
            key = f"dispatch:{user_id}:{dispatch_id}"
            raw = await conn.read(key)
            if not raw:
                return
            data = json.loads(raw) if isinstance(raw, str) else raw
            for p in data.get("pipeline", []):
                if p.get("step") == step:
                    p["status"]         = status
                    p["result_preview"] = result[:200]
                    p["updated_at"]     = datetime.now().isoformat()
                    break
            if overall_done:
                data["status"]  = "done"
                data["done_at"] = datetime.now().isoformat()
            await conn.create(key, data, ttl=86400)
        except Exception as e:
            logger.warning("更新 Redis dispatch 步骤失败: %s", e)
        finally:
            if conn:
                await release_connection("redis", conn)

    async def _execute_agent_instance(
        self,
        agent_name: str,
        task: Dict[str, Any],
        context: Dict[str, Any],
        llm: Any,
        user_id: str,
    ) -> str:
        """执行单个 Agent，通过事件循环支持工具请求与动态注入。

        流程：
          1. 调用 AgentEventLoop.run()（内含工具请求检测、构建、注入、重试逻辑）
          2. 将循环日志追加到 context["_loop_logs"] 供上层记录
          3. 返回最终结果文本
        """
        if self._event_loop is not None:
            session_id = uuid.uuid4().hex[:8]
            result, log_entries = await self._event_loop.run(
                user_id    =user_id,
                agent_name =agent_name,
                task       =task,
                context    =context,
                llm        =llm,
                session_id =session_id,
            )
            # 将循环日志写入 context，以便调用方按需处理
            if isinstance(context, dict):
                existing = context.get("_loop_logs", [])
                context["_loop_logs"] = existing + [e.to_dict() for e in log_entries]
            return result

        # 兜底（event_loop 未初始化，如单元测试场景）
        ag = await self._get_or_load_agent(agent_name, user_id)
        if ag is not None:
            exec_result = await ag.execute(task, context, llm)
            return exec_result.get("result", "")
        result_str, _ = await self._execute_worker_with_tools(
            agent_name, [task], user_id, context, llm
        )
        return result_str

    async def _run_agent_tracked(
        self,
        agent_name: str,
        task: Dict[str, Any],
        context: Dict[str, Any],
        llm: Any,
        user_id: str,
    ) -> Tuple[str, Any]:
        """Execute agent and collect tool/step events via ContextVar collector.

        Returns (result_str, AgentExecCollector) so callers can attach execution
        details to agent_outputs without changing _execute_agent_instance's signature.
        """
        from app.core.exec_collector import AgentExecCollector, set_collector, reset_collector
        collector = AgentExecCollector()
        token = set_collector(collector)
        try:
            result = await self._execute_agent_instance(agent_name, task, context, llm, user_id)
        finally:
            reset_collector(token)
        return result, collector

    async def _execute_parallel(
        self,
        pipeline: List[Dict[str, Any]],
        intent: str,
        user_id: str,
        llm: Any,
        context: Dict[str, Any],
        dispatch_id: str,
    ) -> Tuple[str, List[Dict[str, str]]]:
        """并行执行所有 Agent，合并结果并校验完整性。

        返回: (merged_response, agent_outputs)
          agent_outputs 格式供向量切片使用：[{"agent_name": ..., "output": ...}]
        """
        from app.utils import progress_bus as _pb

        for step in pipeline:
            _pb.push("agent_start", {"step": step["step"], "agent_name": step["agent_name"]})

        async def _tracked(s):
            try:
                r, col = await self._run_agent_tracked(s["agent_name"], s["task"], context, llm, user_id)
                return s, r, col
            except Exception as exc:
                from app.core.exec_collector import AgentExecCollector
                return s, f"执行出错: {exc}", AgentExecCollector()

        raw_results = await asyncio.gather(*[_tracked(s) for s in pipeline], return_exceptions=True)

        result_dicts: List[Dict[str, Any]] = []
        agent_outputs: List[Dict[str, Any]] = []
        for item in raw_results:
            if isinstance(item, Exception):
                continue
            step, r, collector = item
            r_str = str(r)
            result_dicts.append({"agent": step["agent_name"], "result": r_str})
            agent_outputs.append({
                "agent_name": step["agent_name"],
                "task_description": step.get("task", {}).get("description", "")[:300],
                "output": r_str,
                **collector.to_dict(),
            })
            _pb.push("agent_done", {
                "step":           step["step"],
                "agent_name":     step["agent_name"],
                "result_preview": r_str[:150],
            })
            is_last = (step["step"] == pipeline[-1]["step"])
            await self._update_dispatch_step(
                user_id, dispatch_id, step["step"], "done", r_str, is_last
            )
            if self.memory_manager is not None:
                asyncio.create_task(self.memory_manager.on_delegation(
                    user_id, step["agent_name"],
                    step["task"].get("description", ""), r_str,
                ))

        await self.router_agent.validate_parallel_completeness(intent, result_dicts, llm=llm)

        merged = "\n\n".join(
            f"【{r['agent']}】\n{r['result']}"
            for r in result_dicts
        )
        return merged, agent_outputs

    async def _execute_serial(
        self,
        pipeline: List[Dict[str, Any]],
        user_id: str,
        llm: Any,
        context: Dict[str, Any],
        dispatch_id: str,
    ) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, str]]]:
        """串行执行 Agent，每步校验结果后再继续。

        返回: (last_result, agent_outputs, break_info)
          break_info 非 None 表示流水线提前中断，包含 issue/suggestion，
          供调用方直接触发重规划而无需再调用 judge_overall_result。
        """
        from app.utils import progress_bus as _pb

        accumulated   = dict(context)
        last_result   = ""
        agent_outputs: List[Dict[str, Any]] = []
        break_info: Optional[Dict[str, str]] = None

        for i, step in enumerate(pipeline):
            _pb.push("agent_start", {"step": step["step"], "agent_name": step["agent_name"]})
            result, collector = await self._run_agent_tracked(
                step["agent_name"], step["task"], accumulated, llm, user_id
            )
            is_last = (i == len(pipeline) - 1)
            _pb.push("agent_done", {
                "step":           step["step"],
                "agent_name":     step["agent_name"],
                "result_preview": result[:150],
            })
            await self._update_dispatch_step(
                user_id, dispatch_id, step["step"], "done", result, is_last
            )
            agent_outputs.append({
                "agent_name": step["agent_name"],
                "task_description": step.get("task", {}).get("description", "")[:300],
                "output": result,
                **collector.to_dict(),
            })
            if self.memory_manager is not None:
                asyncio.create_task(self.memory_manager.on_delegation(
                    user_id, step["agent_name"],
                    step["task"].get("description", ""), result,
                ))
            accumulated["prev_result"] = result
            last_result = result

            if not is_last:
                validation = await self.router_agent.validate_step_result(
                    agent_name=step["agent_name"],
                    task_desc =step["task"].get("description", ""),
                    result    =result,
                    llm=llm,
                )
                if not validation.get("can_proceed", True):
                    logger.warning(
                        "串行流水线在步骤 %d 中断: agent=%s issue=%s",
                        i, step["agent_name"], validation.get("issue", ""),
                    )
                    break_info = {
                        "issue":      validation.get("issue", "串行流水线提前中断"),
                        "suggestion": validation.get("suggestion", ""),
                    }
                    break

        return last_result, agent_outputs, break_info

    async def _execute_pipeline(
        self,
        pipeline: List[Dict[str, Any]],
        mode: str,
        intent: str,
        user_id: str,
        llm: Any,
        context: Dict[str, Any],
        dispatch_id: str,
        user_input: str,
    ) -> Tuple[str, List[Dict[str, str]]]:
        """根据 mode 执行 pipeline，外层 while 循环由 LLM 判断整体结果后退出。

        流程：
          1. for 遍历当前 pipeline 中每个 Agent 任务并执行
          2. 所有 Agent 完成后，LLM 判断结果是否满足用户原始请求
          3. 满足 → 退出循环；不满足 → router 重新规划 pipeline → 推送 router_replan 事件 → 再次执行
          4. 最多 MAX_ROUTER_ITERATIONS 轮
        """
        from app.utils import progress_bus as _pb

        MAX_ROUTER_ITERATIONS = 3
        result       = ""
        agent_outputs: List[Dict[str, str]] = []
        serial_break: Optional[Dict[str, str]] = None

        if not pipeline:
            result = await self._generate_llm_response(
                llm=llm, user_id=user_id, user_input=user_input,
                intent=intent, tasks=[], context=context,
                agent_name=self.router_agent.name, tool_results=None,
            )
            return result, []

        # Make original user request available to every agent via context
        if isinstance(context, dict):
            context.setdefault("_user_input", user_input)

        # Safeguard: if a task description doesn't include the user's specifics
        # (e.g. URL, filename), append the original request as context so agents
        # aren't left guessing.
        for step in pipeline:
            task = step.get("task", {})
            desc = task.get("description", "")
            if user_input and user_input not in desc:
                task["description"] = f"{desc}\n\n[用户原始请求：{user_input}]"

        for router_iteration in range(MAX_ROUTER_ITERATIONS):
            # ── 单轮执行 ──────────────────────────────────────────────
            if mode == "parallel":
                result, agent_outputs = await self._execute_parallel(
                    pipeline, intent, user_id, llm, context, dispatch_id
                )
            elif mode == "serial":
                result, agent_outputs, serial_break = await self._execute_serial(
                    pipeline, user_id, llm, context, dispatch_id
                )
            else:
                step = pipeline[0]
                _pb.push("agent_start", {"step": step["step"], "agent_name": step["agent_name"]})
                result, collector = await self._run_agent_tracked(
                    step["agent_name"], step["task"], context, llm, user_id
                )
                _pb.push("agent_done", {
                    "step":           step["step"],
                    "agent_name":     step["agent_name"],
                    "result_preview": result[:150],
                })
                await self._update_dispatch_step(user_id, dispatch_id, step["step"], "done", result, True)
                agent_outputs = [{
                    "agent_name": step["agent_name"],
                    "task_description": step.get("task", {}).get("description", "")[:300],
                    "output": result,
                    **collector.to_dict(),
                }]

            # 最后一轮不再判断，直接返回
            if router_iteration >= MAX_ROUTER_ITERATIONS - 1:
                break

            # ── 路由级 LLM 判断 ──────────────────────────────────────
            pipeline_results = [
                {"agent": ao["agent_name"], "result": ao["output"]}
                for ao in agent_outputs
            ]
            # 串行流水线提前中断时，验证器已给出明确失败原因，
            # 直接复用该结果触发重规划，无需再调用 judge_overall_result
            # （避免 LLM 把残缺输出误判为 satisfied=True）
            if mode == "serial" and serial_break:
                judgment = {"satisfied": False, **serial_break}
                serial_break = None
            else:
                judgment = await self.router_agent.judge_overall_result(
                    user_input, pipeline_results, llm
                )
            if judgment.get("satisfied", True):
                logger.debug(
                    "[Pipeline] 第 %d 轮结果满足用户期望，退出循环",
                    router_iteration + 1,
                )
                break

            # ── 重新规划 ─────────────────────────────────────────────
            new_pipeline = await self.router_agent.replan_agents(
                user_input,
                judgment["issue"],
                judgment["suggestion"],
                pipeline_results,
                llm,
            )
            if not new_pipeline:
                logger.warning("[Pipeline] 重新规划返回空 pipeline，使用上一轮结果")
                break

            pipeline = new_pipeline
            _pb.push("router_replan", {
                "reason":     judgment["issue"],
                "iteration":  router_iteration + 1,
                "new_agents": [p["agent_name"] for p in new_pipeline],
            })
            # 推送新一轮 planning 事件，前端可刷新 agent 列表
            _pb.push("planning", {
                "intent":   intent,
                "mode":     mode,
                "replan":   True,
                "iteration": router_iteration + 1,
                "pipeline": [
                    {
                        "step":        p["step"],
                        "agent_name":  p["agent_name"],
                        "description": (p.get("task") or {}).get("description", "")[:100],
                    }
                    for p in new_pipeline
                ],
            })
            logger.info(
                "[Pipeline] 第 %d 轮结果不满足，原因：%s，重新规划 %d 个 Agent",
                router_iteration + 1, judgment["issue"], len(new_pipeline),
            )

        return result, agent_outputs

    async def shutdown(self) -> None:
        """
        关闭引擎，清理资源 (基于 LangChain)

        清理:
        - 代理缓存
        - LLM 缓存
        - Tool 缓存
        - LangGraph 代理图缓存
        """
        try:
            # 清理 LangChain Tools
            self.langchain_tools.clear()
            logger.info("已清理 LangChain Tool 缓存")

            # 清理 LLM 缓存
            self.llm_cache.clear()
            logger.info("已清理 LLM 缓存")

            # 清理 LangGraph 代理图
            self.agent_graphs.clear()
            logger.info("已清理 LangGraph 代理图")

            # 清理 AgentExecutor 缓存
            self.agent_executor_caches.clear()
            logger.info("已清理 AgentExecutor 缓存")

            # 清理其他缓存
            self.agents.clear()
            self.worker_configs.clear()
            self.tool_configs.clear()
            self.loaded_tool_functions.clear()
            self.intent_agent_mapping.clear()

            logger.info("Hermes 引擎已关闭并释放所有资源 ✓")
        except Exception as e:
            logger.error(f"引擎关闭过程中出错: {e}")
