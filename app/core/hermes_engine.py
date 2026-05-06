"""Hermes 框架核心 - 基于 LangChain 的多智能体编排引擎"""

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
except ImportError:
    _HTTPX_CONNECT_ERRORS = (OSError,)

try:
    from openai import APIConnectionError as _OAIConnectionError, APITimeoutError as _OAITimeoutError
    _OPENAI_CONNECT_ERRORS = (_OAIConnectionError, _OAITimeoutError)
except ImportError:
    _OPENAI_CONNECT_ERRORS = ()

# 所有"LLM 端点无法连接"的异常类型，统一用于 except 子句
_LLM_CONNECT_ERRORS = _HTTPX_CONNECT_ERRORS + _OPENAI_CONNECT_ERRORS

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
from app.core.config_loader import ConfigLoader
from app.database.pool import get_connection, release_connection
from app.core.chat_history_store import ChatHistoryStore
from app.core.context_manager import ContextManager
from app.rag import RagPipeline

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 流式记忆上下文过滤器（参考 hermes-agent StreamingContextScrubber）
# 在流式输出中剔除模型可能回显的 <memory-context> 块
# ══════════════════════════════════════════════════════════════════

class StreamingContextScrubber:
    """逐 chunk 剔除 <memory-context>…</memory-context> 块。

    背景：系统提示中注入了 <memory-context> 标签围栏的历史记忆，
    部分模型偶尔会将其原样回显给用户。本类通过状态机在流式输出中
    过滤掉这类块，确保用户不会看到"幕后"记忆内容。

    用法::
        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()
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


class RegistryToolAdapter(BaseTool):
    """将 app.tools.base.BaseTool（registry 工具）适配为 LangChain Tool。

    LangGraph 调用工具时以 kwargs 形式传入参数；适配层将其封装为
    (params, context) 后调用 BaseTool.execute()，结果序列化为字符串返回。
    """

    def __init__(self, registry_tool: Any, user_id: str, agent_name: str = ""):
        super().__init__()
        self.name        = registry_tool.name
        self.description = registry_tool.description or f"Tool: {registry_tool.name}"
        self._registry_tool = registry_tool
        self._user_id    = user_id
        self._agent_name = agent_name

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self._arun(*args, **kwargs))

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        import json as _json
        # LangGraph 以 kwargs 传入；无 schema 时 LangChain 可能以单字符串传入
        if args and isinstance(args[0], str) and not kwargs:
            try:
                params = _json.loads(args[0])
            except Exception:
                params = {"input": args[0]}
        else:
            params = dict(kwargs)

        context = {"user_id": self._user_id, "agent_name": self._agent_name}
        try:
            result = await self._registry_tool.execute(params, context)
            if isinstance(result, dict):
                return _json.dumps(result, ensure_ascii=False)
            return str(result)
        except Exception as e:
            logger.warning("[RegistryToolAdapter] %s 执行失败 user=%s: %s",
                           self.name, self._user_id, e)
            return f"工具 {self.name} 执行失败: {e}"


@dataclass
class LLMInfo:
    """LLM 信息数据模型与构建器。"""
    user_id: str
    url: str
    api_key: str
    model_name: str
    model_type: str = "chat"
    temperature: float = 0.7

    @classmethod
    async def load(
        cls,
        user_id: str,
        db_alias: Optional[str] = None,
        table_name: str = "llms",
        fallback_user_id: str = "0",
    ) -> Optional["LLMInfo"]:
        connection = None
        try:
            connection = await get_connection("mysql", db_alias)
            if not connection:
                logger.error("无法获取数据库连接")
                return None

            sql = (
                f"SELECT url, api_key, model_name, model_type, temperature "
                f"FROM {table_name} WHERE user_id = :user_id AND state = 1 "
                f"AND model_type != 'embedding' "
                "ORDER BY id DESC LIMIT 1"
            )
            logger.debug(f"执行SQL查询: {sql}, user_id={user_id}")
            df = await connection.execute_raw(sql, {"user_id": user_id})
            if df is None or len(df) == 0:
                if user_id != fallback_user_id:
                    logger.info(f"未找到用户 {user_id} 的 LLM 配置，加载默认用户 {fallback_user_id}")
                    df = await connection.execute_raw(sql, {"user_id": fallback_user_id})

            if df is None or len(df) == 0:
                logger.warning("未找到任何 LLM 配置")
                return None

            # DataFrame的第一行
            row = df.iloc[0]
            logger.debug(f"查询到LLM配置: {row.to_dict()}")
            
            return cls(
                user_id=user_id,
                url=row.get("url") or row.get(0) or "",
                api_key=row.get("api_key") or row.get(1) or "",
                model_name=row.get("model_name") or row.get(2) or "",
                model_type=row.get("model_type") or row.get(3) or "chat",
                temperature=float(row.get("temperature") or row.get(4) or 0.7) if row.get("temperature") is not None else 0.7,
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
                "timeout": 120.0,   # Ollama 本地模型冷启动较慢
                "max_retries": 1,
            }
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "model_provider": self.provider,
            "temperature": self.temperature,
            "api_key": self.api_key,
            "timeout": 60.0,
            "max_retries": 1,
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
            if not connection:
                logger.error("无法获取数据库连接")
                return []

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
                executor = create_react_agent(llm, tools, state_modifier=system_prompt)
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
        self.llm_cache: Dict[str, BaseChatModel] = {}  # 用户 LLM 缓存
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
        # 上下文管理器（兼容保留；主链路已由 RagPipeline 取代）
        self.context_manager: Optional[ContextManager] = None
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
            "你是一个强大的多代理协作平台，能够处理复杂的任务、分解问题、调用工具和生成见解。\n"
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
        """注入 MemoryManager 并创建 ContextManager（兼容保留）。"""
        self.memory_manager = memory_manager
        self.context_manager = ContextManager(memory_manager, self.config)
        if self.default_llm and hasattr(memory_manager, "set_default_llm"):
            memory_manager.set_default_llm(self.default_llm)

    def set_rag_pipeline(self, rag_pipeline: "RagPipeline") -> None:
        """注入 RagPipeline（由 main.py 在 VectorStore 初始化后调用）。

        注入后 process_user_input / process_user_input_stream 均走 RagPipeline 路径。
        同时将 rag_pipeline 注入 ContextManager，使兜底路径也能受益。
        """
        self.rag_pipeline = rag_pipeline
        if self.context_manager is not None:
            self.context_manager.set_rag_pipeline(rag_pipeline)
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
            llm = await self._get_user_llm(user_id)

        if llm is None:
            logger.error(f"无法为用户 {user_id} 获取 LLM")
            return "当前无法调用 LLM，请稍后重试。"

        try:
            turn_id = uuid.uuid4().hex

            # 使用 RagPipeline 组装上下文（检索 + 历史加载 + 相关性过滤）
            _rag_source = self.rag_pipeline or self.context_manager
            if _rag_source is not None:
                try:
                    bundle  = await _rag_source.build_context(
                        user_id=user_id,
                        user_input=user_input,
                        base_context=context if isinstance(context, dict) else {},
                    )
                    context = bundle.to_prompt_context()
                    logger.info(
                        "上下文组装完成 user=%s history=%d memories=%d",
                        user_id, len(bundle.history), len(bundle.memories),
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
            _ctx_dict     = context if isinstance(context, dict) else {}
            turn_metadata = {
                "intent":         intent,
                "target_agent":   target_agent,
                "tasks":          tasks,
                "tool_results":   tool_results,
                "tool_steps":     tool_steps,
                "mode":           mode,
                "pipeline":       [{"step": p["step"], "agent_name": p["agent_name"]} for p in pipeline],
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
        except Exception as e:
            logger.error(f"处理用户输入失败: {e}")
            return "处理请求时发生错误，请稍后重试。"

    async def _get_user_llm(self, user_id: str) -> Optional[BaseChatModel]:
        """
        获取用户 LLM - 支持缓存和动态加载
        
        Args:
            user_id: 用户 ID
            
        Returns:
            BaseChatModel: LangChain 聊天模型，或 None
        """
        # 检查缓存
        if user_id in self.llm_cache:
            return self.llm_cache[user_id]
        
        # 从数据库加载 LLM 信息
        llm_info = await self._load_llm_info(user_id)
        if not llm_info:
            logger.warning(f"未找到用户 {user_id} 的 LLM 配置，尝试使用默认模型")
            if self.default_llm:
                self.llm_cache[user_id] = self.default_llm
                return self.default_llm
            
            # 加载默认用户的 LLM
            llm_info = await self._load_llm_info("0")
            if not llm_info:
                logger.error("未找到任何 LLM 模型配置")
                return None

        # 根据配置构建 LLM
        llm = await self._build_llm_from_config(llm_info)
        if llm:
            self.llm_cache[user_id] = llm
        
        return llm

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
        """
        清空 LLM 缓存
        
        Args:
            user_id: 指定用户 ID，如果为 None 则清空所有缓存
        """
        if user_id:
            if user_id in self.llm_cache:
                del self.llm_cache[user_id]
                logger.info(f"已清空用户 {user_id} 的 LLM 缓存")
        else:
            self.llm_cache.clear()
            logger.info("已清空所有 LLM 缓存")

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
            adapters.append(RegistryToolAdapter(t, user_id=user_id, agent_name=agent_name))
        return adapters

    def _get_agent_executor_cache(self, user_id: str) -> AgentExecutorCache:
        if user_id not in self.agent_executor_caches:
            self.agent_executor_caches[user_id] = AgentExecutorCache(
                user_id=user_id,
                llm_factory=self._get_user_llm,
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
                        from app.core.client_env import format_env_for_prompt
                        _ctx = context if isinstance(context, dict) else {}
                        _env_block = format_env_for_prompt(
                            _ctx.get("_client_type"), _ctx.get("_client_version")
                        )
                        _profile_block = _ctx.get("_user_profile", "")
                        if _env_block:
                            system_prompt += "\n\n" + _env_block
                        if _profile_block:
                            system_prompt += "\n\n" + _profile_block
                    graph = create_react_agent(llm, langchain_tools, state_modifier=system_prompt)
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
            raw = await graph.ainvoke(
                {"messages": [HumanMessage(content=f"请执行以下任务: {task_desc}")]}
            )

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
            from app.core.client_env import format_env_for_prompt as _fmt_env
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

            # 调用链 (使用异步)
            response = await chain.ainvoke({
                "agent_name":           agent_name,
                "user_id":              user_id,
                "intent":               intent,
                "user_input":           user_input,
                "tasks_summary":        tasks_summary,
                "tool_results_summary": tool_results_summary,
                "context_summary":      context_summary,
                "memory_section":       memory_section,
            })
            
            logger.info(f"LLM 已生成回复 (user_id={user_id}, agent={agent_name}, intent={intent})")
            logger.debug("LLM输出 user=%s len=%d:\n%s", user_id, len(response), response)
            return response
            
        except _LLM_CONNECT_ERRORS as e:
            logger.warning("LLM 端点连接失败 user=%s: %s", user_id, e)
            return "LLM 服务端点无法连接，请检查服务是否启动。"
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
            from app.core.client_env import format_env_for_prompt as _fmt_env_s
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
            async for chunk in chain.astream({
                "agent_name":    agent_name,
                "user_id":       user_id,
                "intent":        intent,
                "user_input":    user_input,
                "tasks_summary": tasks_summary,
                "context_summary": context_summary,
                "memory_section": memory_section,
            }):
                yield chunk
        except _LLM_CONNECT_ERRORS as e:
            logger.warning("流式 LLM 端点连接失败 user=%s: %s", user_id, e)
            yield "LLM 服务端点无法连接，请检查服务是否启动。"
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
        """
        流式处理用户输入，yield SSE 格式事件字符串。

        event: routing   — 路由决策完成
        event: token     — LLM token 块
        event: done      — 全部完成
        event: error     — 发生错误
        """
        import json as _json

        if self.router_agent is None:
            yield 'event: error\ndata: {"message": "Router not initialized"}\n\n'
            return

        if llm is None:
            llm = await self._get_user_llm(user_id)
        if llm is None:
            yield 'event: error\ndata: {"message": "LLM unavailable"}\n\n'
            return

        try:
            turn_id = uuid.uuid4().hex

            _rag_source = self.rag_pipeline or self.context_manager
            if _rag_source is not None:
                try:
                    bundle  = await _rag_source.build_context(
                        user_id=user_id, user_input=user_input,
                        base_context=context if isinstance(context, dict) else {},
                    )
                    context = bundle.to_prompt_context()
                    logger.info(
                        "上下文组装完成 user=%s history=%d memories=%d [stream]",
                        user_id, len(bundle.history), len(bundle.memories),
                    )
                except Exception as e:
                    logger.warning("RAG build_context 失败，使用原始 context [stream]: %s", e)

            # 上下文长度检查（流式版本）
            if self.memory_manager is not None:
                ctx_chars = self._estimate_context_length(context)
                ctx_limit = getattr(self.memory_manager, "context_length_limit", 20_000)
                if ctx_chars > ctx_limit:
                    logger.info(
                        f"[stream] 上下文过长 {ctx_chars}/{ctx_limit} chars，"
                        f"立即触发记忆压缩 user={user_id}"
                    )
                    asyncio.create_task(
                        self.memory_manager.compress_immediately(user_id, "context_overflow")
                    )

            # 加载用户画像到 context，供子 Agent 系统提示注入
            if isinstance(context, dict) and self.memory_manager is not None:
                if "_user_profile" not in context:
                    try:
                        profile_block = await self.memory_manager.build_system_prompt_block(user_id)
                        if profile_block:
                            context["_user_profile"] = profile_block
                    except Exception as _pe:
                        logger.debug("[stream] 预加载用户画像失败 user=%s: %s", user_id, _pe)

            router_result = await self.router_agent.process(user_input, context, llm=llm)
            intent       = router_result.get("intent", "general_question")
            mode         = router_result.get("mode", "single")
            pipeline     = router_result.get("pipeline", [])
            tasks        = router_result.get("tasks", [])
            target_agent = router_result.get("target_agent") or self.router_agent.name

            if agent_name:
                override_ag = await self._get_or_load_agent(agent_name, user_id)
                if override_ag:
                    pipeline     = [{"step": 0, "agent_name": agent_name,
                                     "task": {"task_id": "task_1", "type": intent,
                                              "description": user_input}}]
                    mode         = "single"
                    target_agent = agent_name

            yield (
                f'event: routing\n'
                f'data: {_json.dumps({"intent": intent, "mode": mode, "agent": target_agent}, ensure_ascii=False)}\n\n'
            )

            full_response_parts: List[str] = []
            scrubber   = StreamingContextScrubber()  # 剔除 LLM 可能回显的 <memory-context> 块
            stream_gen = self._generate_llm_response_stream(
                llm=llm, user_id=user_id, user_input=user_input,
                intent=intent, tasks=tasks, context=context, agent_name=target_agent,
            )
            async for chunk in stream_gen:
                visible = scrubber.feed(chunk)
                if visible:
                    full_response_parts.append(visible)
                    yield f'event: token\ndata: {_json.dumps({"text": visible}, ensure_ascii=False)}\n\n'
            # 刷出缓冲区尾部
            trailing = scrubber.flush()
            if trailing:
                full_response_parts.append(trailing)
                yield f'event: token\ndata: {_json.dumps({"text": trailing}, ensure_ascii=False)}\n\n'

            full_response = "".join(full_response_parts)

            _sctx = context if isinstance(context, dict) else {}
            # 异步保存到存储层（不阻塞流）
            asyncio.create_task(self._save_turn_async(
                user_id=user_id, user_input=user_input,
                response=full_response, turn_id=turn_id,
                intent=intent, target_agent=target_agent,
                tasks=tasks, mode=mode, pipeline=pipeline,
                client_type=_sctx.get("_client_type", ""),
                client_version=_sctx.get("_client_version", ""),
            ))

            yield f'event: done\ndata: {_json.dumps({"turn_id": turn_id}, ensure_ascii=False)}\n\n'

        except Exception as e:
            logger.error(f"流式处理失败 user={user_id}: {e}", exc_info=True)
            yield f'event: error\ndata: {_json.dumps({"message": str(e)}, ensure_ascii=False)}\n\n'

    async def _save_turn_async(
        self,
        user_id: str, user_input: str, response: str,
        turn_id: str, intent: str, target_agent: str,
        tasks: list, mode: str, pipeline: list,
        client_type: str = "", client_version: str = "",
    ) -> None:
        """后台保存对话轮次到 ES 和 MemoryManager。"""
        turn_metadata = {
            "intent": intent, "target_agent": target_agent,
            "tasks": tasks, "mode": mode,
            "pipeline": [{"step": p["step"], "agent_name": p["agent_name"]} for p in pipeline],
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
        if not conn:
            return None
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
            if not conn:
                return
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
            if not conn:
                return
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
        coros = [
            self._execute_agent_instance(step["agent_name"], step["task"], context, llm, user_id)
            for step in pipeline
        ]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        result_dicts: List[Dict[str, Any]] = []
        for step, r in zip(pipeline, raw_results):
            if isinstance(r, Exception):
                r = f"执行出错: {r}"
            result_dicts.append({"agent": step["agent_name"], "result": str(r)})
            is_last = (step["step"] == pipeline[-1]["step"])
            await self._update_dispatch_step(
                user_id, dispatch_id, step["step"], "done", str(r), is_last
            )
            if self.memory_manager is not None:
                asyncio.create_task(self.memory_manager.on_delegation(
                    user_id, step["agent_name"],
                    step["task"].get("description", ""), str(r),
                ))

        await self.router_agent.validate_parallel_completeness(intent, result_dicts, llm=llm)

        agent_outputs = [
            {"agent_name": r["agent"], "output": r["result"]}
            for r in result_dicts
        ]
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
    ) -> Tuple[str, List[Dict[str, str]]]:
        """串行执行 Agent，每步校验结果后再继续。

        返回: (last_result, agent_outputs)
          agent_outputs 包含每一步的输出，供向量切片各步独立索引（req 2）。
        """
        accumulated   = dict(context)
        last_result   = ""
        agent_outputs: List[Dict[str, str]] = []

        for i, step in enumerate(pipeline):
            result  = await self._execute_agent_instance(
                step["agent_name"], step["task"], accumulated, llm, user_id
            )
            is_last = (i == len(pipeline) - 1)
            await self._update_dispatch_step(
                user_id, dispatch_id, step["step"], "done", result, is_last
            )
            agent_outputs.append({"agent_name": step["agent_name"], "output": result})
            if self.memory_manager is not None:
                asyncio.create_task(self.memory_manager.on_delegation(
                    user_id, step["agent_name"],
                    step["task"].get("description", ""), result,
                ))
            accumulated["prev_result"] = result
            last_result = result

            if not is_last:
                next_task  = pipeline[i + 1]["task"]
                validation = await self.router_agent.validate_step_result(
                    agent_name    =step["agent_name"],
                    task_desc     =step["task"].get("description", ""),
                    result        =result,
                    next_task_desc=next_task.get("description", ""),
                    llm=llm,
                )
                if not validation.get("can_proceed", True):
                    logger.warning(
                        "串行流水线在步骤 %d 中断: agent=%s issue=%s",
                        i, step["agent_name"], validation.get("issue", ""),
                    )
                    break

        return last_result, agent_outputs

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
        """根据 mode 执行 pipeline，返回 (最终回复文本, per-agent 输出列表)。"""
        if not pipeline:
            result = await self._generate_llm_response(
                llm=llm, user_id=user_id, user_input=user_input,
                intent=intent, tasks=[], context=context,
                agent_name=self.router_agent.name, tool_results=None,
            )
            return result, []
        if mode == "parallel":
            return await self._execute_parallel(pipeline, intent, user_id, llm, context, dispatch_id)
        if mode == "serial":
            return await self._execute_serial(pipeline, user_id, llm, context, dispatch_id)
        # single
        step   = pipeline[0]
        result = await self._execute_agent_instance(
            step["agent_name"], step["task"], context, llm, user_id
        )
        await self._update_dispatch_step(user_id, dispatch_id, step["step"], "done", result, True)
        return result, [{"agent_name": step["agent_name"], "output": result}]

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
