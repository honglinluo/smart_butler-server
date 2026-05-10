"""Hermes 流水线日志总线

结构化记录 Agent 处理全流程：用户消息 → 上下文组装 → 路由 → LLM 输入/输出 → 工具调用。

特性
----
- 彩色终端输出：每类事件使用固定颜色，方便肉眼扫描
- JSON 格式（可选）：启用后写入文件，供 ELK / Loki 采集
- 零依赖注入：通过 get_bus() 获取单例，无需到处传参

用法
----
    # 任意模块中
    from app.utils.log_bus import get_bus
    bus = get_bus()
    bus.user_message(user_id="u1", message="你好", client_type="lark")

    # main.py 启动时初始化（传入 system_config.yaml 的 logging 节）
    from app.utils.log_bus import init_log_bus
    init_log_bus({"level": "DEBUG", "json_file": "logs/hermes.json"})

事件颜色对照（终端）
--------------------
  Bright Cyan   — 用户消息      (user)
  Blue          — 上下文 / RAG  (context)
  Magenta       — 路由决策      (routing)
  Yellow        — LLM 输入      (llm_in)
  Bright Green  — LLM 输出      (llm_out)
  White         — 工具调用      (tool)
  Green         — 工具成功      (tool_ok)
  Bright Red    — 工具失败      (tool_err)
  Dark Gray     — 系统/其他     (system)
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

# ── ANSI 颜色 ─────────────────────────────────────────────────────────────────
_RESET = "\033[0m"

_EVENT_COLORS: Dict[str, str] = {
    "user":     "\033[96m",   # Bright Cyan
    "context":  "\033[34m",   # Blue
    "routing":  "\033[35m",   # Magenta
    "llm_in":   "\033[33m",   # Yellow
    "llm_out":  "\033[92m",   # Bright Green
    "tool":     "\033[37m",   # White
    "tool_ok":  "\033[32m",   # Green
    "tool_err": "\033[91m",   # Bright Red
    "system":   "\033[90m",   # Dark Gray
}


# ── 彩色终端格式化器 ───────────────────────────────────────────────────────────

class _ColorFormatter(logging.Formatter):
    """根据 LogRecord 的 event_type 字段为终端输出着色。"""

    def format(self, record: logging.LogRecord) -> str:
        evt   = getattr(record, "event_type", "system")
        color = _EVENT_COLORS.get(evt, "")
        text  = super().format(record)
        if color and os.getenv("NO_COLOR") is None:
            return f"{color}{text}{_RESET}"
        return text


# ── Hermes 流水线日志总线 ──────────────────────────────────────────────────────

class HermesLogger:
    """Agent 流水线各阶段的结构化日志总线。

    所有方法均在 ``hermes.bus`` logger 命名空间下发射日志，
    不影响其他模块的标准 logging 配置。
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("hermes.bus")

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _emit(
        self,
        level:      int,
        event_type: str,
        message:    str,
        extra:      Optional[Dict[str, Any]] = None,
    ) -> None:
        fields = {"event_type": event_type, **(extra or {})}
        self._logger.log(level, message, extra=fields, stacklevel=2)

    @staticmethod
    def _clip(text: str, max_len: int = 200) -> str:
        s = str(text)
        if len(s) <= max_len:
            return s
        return s[:max_len] + f" …(+{len(s) - max_len})"

    # ── 事件方法 ──────────────────────────────────────────────────────────────

    def user_message(
        self,
        user_id:     str,
        message:     str,
        client_type: str = "api",
    ) -> None:
        """用户消息进入系统。"""
        self._emit(
            logging.INFO, "user",
            f"[用户消息] user={user_id} client={client_type} | {self._clip(message, 120)}",
            {"user_id": user_id, "client_type": client_type, "content": message[:500]},
        )

    def context_built(
        self,
        user_id:       str,
        history_count: int,
        memory_count:  int,
        client_type:   str = "",
    ) -> None:
        """RAG 上下文组装完成。"""
        self._emit(
            logging.DEBUG, "context",
            f"[上下文] user={user_id} history={history_count} "
            f"memories={memory_count} client={client_type}",
            {
                "user_id":       user_id,
                "history_count": history_count,
                "memory_count":  memory_count,
                "client_type":   client_type,
            },
        )

    def routing(
        self,
        user_id:        str,
        intent:         str,
        mode:           str,
        target_agent:   str,
        pipeline_steps: int = 0,
    ) -> None:
        """路由决策结果。"""
        self._emit(
            logging.INFO, "routing",
            f"[路由] user={user_id} intent={intent} mode={mode} "
            f"→ {target_agent} (steps={pipeline_steps})",
            {
                "user_id":        user_id,
                "intent":         intent,
                "mode":           mode,
                "target_agent":   target_agent,
                "pipeline_steps": pipeline_steps,
            },
        )

    def llm_input(
        self,
        user_id:       str,
        agent_name:    str,
        human_content: str,
        system_prompt: str = "",
        tools:         Optional[List[str]] = None,
    ) -> None:
        """发送给 LLM 的内容（system prompt + human message）。"""
        self._emit(
            logging.DEBUG, "llm_in",
            f"[→LLM] user={user_id} agent={agent_name} "
            f"sys={len(system_prompt)}chars tools={tools or []} | "
            f"{self._clip(human_content, 200)}",
            {
                "user_id":       user_id,
                "agent_name":    agent_name,
                "system_prompt": system_prompt[:300],
                "human_content": human_content[:500],
                "tools":         tools or [],
            },
        )

    def llm_output(
        self,
        user_id:    str,
        agent_name: str,
        response:   str,
        elapsed_ms: Optional[float] = None,
    ) -> None:
        """LLM 返回内容。"""
        timing = f" ({elapsed_ms:.0f}ms)" if elapsed_ms is not None else ""
        self._emit(
            logging.INFO, "llm_out",
            f"[←LLM]{timing} user={user_id} agent={agent_name} | "
            f"{self._clip(response, 150)}",
            {
                "user_id":    user_id,
                "agent_name": agent_name,
                "response":   response[:500],
                **({"elapsed_ms": round(elapsed_ms, 1)} if elapsed_ms is not None else {}),
            },
        )

    def tool_call(
        self,
        user_id:    str,
        agent_name: str,
        tool_name:  str,
        args:       Any,
    ) -> None:
        """工具调用开始。"""
        self._emit(
            logging.INFO, "tool",
            f"[工具↑] user={user_id} agent={agent_name} "
            f"tool={tool_name} args={self._clip(str(args), 120)}",
            {
                "user_id":    user_id,
                "agent_name": agent_name,
                "tool_name":  tool_name,
                "tool_args":  str(args)[:300],
            },
        )
        from app.utils.progress_bus import push as _pb
        _pb("tool_call", {"agent_name": agent_name, "tool_name": tool_name, "args": str(args)[:200]})

    def tool_result(
        self,
        user_id:    str,
        agent_name: str,
        tool_name:  str,
        result:     str,
        elapsed_ms: Optional[float] = None,
    ) -> None:
        """工具调用成功返回。"""
        timing = f" ({elapsed_ms:.0f}ms)" if elapsed_ms is not None else ""
        self._emit(
            logging.INFO, "tool_ok",
            f"[工具↓]{timing} user={user_id} agent={agent_name} "
            f"tool={tool_name} → {self._clip(result, 120)}",
            {
                "user_id":    user_id,
                "agent_name": agent_name,
                "tool_name":  tool_name,
                "result":     result[:300],
                **({"elapsed_ms": round(elapsed_ms, 1)} if elapsed_ms is not None else {}),
            },
        )
        from app.utils.progress_bus import push as _pb
        _pb("tool_result", {
            "agent_name": agent_name,
            "tool_name":  tool_name,
            "result":     result[:300],
            **({"elapsed_ms": round(elapsed_ms, 1)} if elapsed_ms is not None else {}),
        })

    def tool_error(
        self,
        user_id:    str,
        agent_name: str,
        tool_name:  str,
        error:      str,
    ) -> None:
        """工具调用失败（错误信息已作为 ToolMessage 反馈给 LLM）。"""
        self._emit(
            logging.ERROR, "tool_err",
            f"[工具✗] user={user_id} agent={agent_name} "
            f"tool={tool_name} error={self._clip(error, 150)}",
            {
                "user_id":    user_id,
                "agent_name": agent_name,
                "tool_name":  tool_name,
                "error":      error[:300],
            },
        )
        from app.utils.progress_bus import push as _pb
        _pb("tool_error", {"agent_name": agent_name, "tool_name": tool_name, "error": error[:300]})


# ── 模块级单例 ────────────────────────────────────────────────────────────────

_bus: Optional[HermesLogger] = None


def get_bus() -> HermesLogger:
    """返回全局 HermesLogger 单例（未初始化时自动创建）。"""
    global _bus
    if _bus is None:
        _bus = HermesLogger()
    return _bus


def init_log_bus(log_cfg: Dict[str, Any]) -> HermesLogger:
    """初始化日志总线及整个应用的 logging 体系（在 main.py 中调用一次）。

    配置两条 handler：
      1. 彩色 StreamHandler — 终端实时输出，颜色由 event_type 决定
      2. JSON FileHandler（可选）— 写入 log_cfg["json_file"] 路径，供 ELK/Loki 采集

    Args:
        log_cfg: system_config.yaml logging 节的 dict，支持字段：
            level      — 日志级别，默认 "INFO"
            json_file  — JSON 日志文件路径（空/缺失则不启用 JSON handler）
    """
    global _bus

    level_name = str(log_cfg.get("level", "INFO")).upper()
    level      = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # ── 彩色终端 handler ──────────────────────────────────────────────────────
    stream_h = logging.StreamHandler()
    stream_h.setLevel(level)
    stream_h.setFormatter(
        _ColorFormatter(
            fmt="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(stream_h)

    # ── JSON 文件 handler（可选）──────────────────────────────────────────────
    json_file = log_cfg.get("json_file", "")
    if json_file:
        try:
            from pythonjsonlogger import jsonlogger
            parent = os.path.dirname(json_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            file_h = logging.FileHandler(json_file, encoding="utf-8")
            file_h.setLevel(level)
            file_h.setFormatter(
                jsonlogger.JsonFormatter(
                    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            root.addHandler(file_h)
            logging.getLogger("hermes.bus").info(
                "JSON 日志已启用: %s", json_file,
                extra={"event_type": "system"},
            )
        except Exception as e:
            logging.getLogger("hermes.bus").warning(
                "JSON 日志初始化失败: %s", e,
                extra={"event_type": "system"},
            )

    _bus = HermesLogger()
    return _bus
