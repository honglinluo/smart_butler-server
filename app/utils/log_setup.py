"""
【模块说明】三级日志文件配置（LogSetup）— 把系统运行过程记录到不同级别的日志文件

服务器运行中会产生大量日志，把所有日志混在一起很难排查问题。
这个模块把日志按内容和用途分成三个独立的日志文件，方便快速找到需要的信息。

【三级日志文件】
  Tier 1 — system.log（系统运行日志）
    记录：数据库连接状态、服务启动/关闭、向量化任务等基础设施事件
    滚动：文件超过 10 MB 自动备份，最多保留 5 个备份文件

  Tier 2 — conv/turn_{轮次ID}.log（单轮对话全程日志）
    记录：每轮对话的完整过程（用户输入→上下文→路由→AI调用→工具执行）
    特点：每轮对话一个独立文件，并发对话互不干扰（通过 ContextVar 隔离）

  Tier 3 — scheduler/scheduler.log（定时任务日志）
    记录：定时任务触发和执行情况（系统任务 + 用户自定义任务）
    滚动：按天滚动，每天一个文件

三级日志文件配置

Tier 1 — system.log  (RotatingFileHandler, 10 MB × 5 backup)
  记录服务运行状态：数据库连接、模型服务、向量化、启动/关闭等基础设施事件。
  接收所有日志命名空间（app.scheduler.* 除外，由 Tier 3 单独处理）。

Tier 2 — conv/turn_{turn_id}.log  (FileHandler, per-turn)
  记录单轮对话全过程：用户输入 → 上下文组装 → 路由 → LLM 调用 → 工具执行。
  由 start_turn() / end_turn() 控制，通过 ContextVar 绑定到当前 asyncio 任务。
  并发对话互不干扰：每个 asyncio Task 独立持有自己的 ContextVar 快照。

Tier 3 — scheduler/scheduler.log  (TimedRotatingFileHandler, 按日滚动)
  记录定时任务执行状态（系统内置任务 + 用户自定义任务）。
  对应 app.scheduler.* 命名空间；同时继续向 root 传播（保留终端 + system.log 输出）。

调用方
------
  # main.py / init_log_bus 初始化后调用一次
  setup_logging(log_cfg)

  # hermes_engine.py 每轮对话
  start_turn(turn_id)
  try:
      ...
  finally:
      end_turn(turn_id)
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Optional

# ── 目录常量 ─────────────────────────────────────────────────────────────────
LOG_BASE      = Path("/tmp/smart_butler")
LOG_CONV_DIR  = LOG_BASE / "conv"
LOG_SCHED_DIR = LOG_BASE / "scheduler"

# ── 公用格式 ─────────────────────────────────────────────────────────────────
_PLAIN_FMT = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── ContextVar：当前 asyncio Task 的 turn_id（空字符串 = 非对话上下文）──────
_TURN_CTX: ContextVar[str] = ContextVar("hermes_turn_id", default="")

# ── 全局 turn → FileHandler 映射（线程安全）─────────────────────────────────
_turn_handlers: Dict[str, logging.FileHandler] = {}
_turn_lock = threading.Lock()


# 不应写入对话轮次日志的基础设施命名空间（属于 Tier 1 系统日志）
_TURN_EXCLUDE_PREFIXES: tuple = (
    "app.database",
    "app.scheduler",
    "elasticsearch",
    "elastic_transport",
    "redis",
    "asyncio",
    "uvicorn",
    "fastapi",
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "hpack",
    "h2",
)


# ── Tier 2：对话轮次路由器 ────────────────────────────────────────────────────

class _TurnRouter(logging.Handler):
    """将当前 asyncio Task 上下文的对话相关日志写入对应的 turn 文件。

    通过 ContextVar 读取当前 turn_id，无需注入任何额外参数。
    不同并发 Task 各自持有独立的 ContextVar 快照，天然隔离。
    基础设施命名空间（数据库、调度器等）被排除，只写入 Tier 1 系统日志。
    """

    def emit(self, record: logging.LogRecord) -> None:
        # 过滤基础设施命名空间，不写入对话日志
        if record.name.startswith(_TURN_EXCLUDE_PREFIXES):
            return
        tid = _TURN_CTX.get("")
        if not tid:
            return
        with _turn_lock:
            h = _turn_handlers.get(tid)
        if h is None:
            return
        try:
            h.emit(record)
        except Exception:
            self.handleError(record)


# ── Tier 1：排除调度器命名空间 ────────────────────────────────────────────────

class _ExcludeSchedulerFilter(logging.Filter):
    """过滤掉 app.scheduler.* 的记录，由 Tier 3 专门处理。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("app.scheduler")


# ── 初始化（幂等）────────────────────────────────────────────────────────────

_setup_done = False


def setup_logging(log_cfg: Optional[dict] = None) -> None:
    """初始化三级日志文件处理器，在 init_log_bus() 内部调用，整个进程只需执行一次。

    Args:
        log_cfg: system_config.yaml logging 节，目前仅读取 ``level`` 字段。
    """
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    log_cfg = log_cfg or {}

    # 确保目录存在
    for d in (LOG_BASE, LOG_CONV_DIR, LOG_SCHED_DIR):
        d.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()

    # ── Tier 1: system.log ──────────────────────────────────────────────────
    sys_h = logging.handlers.RotatingFileHandler(
        LOG_BASE / "system.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    sys_h.setLevel(logging.DEBUG)
    sys_h.setFormatter(_PLAIN_FMT)
    sys_h.addFilter(_ExcludeSchedulerFilter())
    root.addHandler(sys_h)

    # ── Tier 2: 对话轮次路由器 ───────────────────────────────────────────────
    turn_r = _TurnRouter()
    turn_r.setLevel(logging.DEBUG)
    turn_r.setFormatter(_PLAIN_FMT)
    root.addHandler(turn_r)

    # ── Tier 3: scheduler/scheduler.log ─────────────────────────────────────
    # propagate=True（默认）：同时输出到 root（终端 + system.log），不重复配置 StreamHandler
    sched_logger = logging.getLogger("app.scheduler")
    if not any(isinstance(h, logging.handlers.TimedRotatingFileHandler)
               for h in sched_logger.handlers):
        sched_h = logging.handlers.TimedRotatingFileHandler(
            LOG_SCHED_DIR / "scheduler.log",
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        sched_h.suffix = "%Y-%m-%d"
        sched_h.setLevel(logging.DEBUG)
        sched_h.setFormatter(_PLAIN_FMT)
        sched_logger.addHandler(sched_h)

    logging.getLogger(__name__).info(
        "日志文件已就绪: system=%s conv=%s scheduler=%s",
        LOG_BASE / "system.log",
        LOG_CONV_DIR,
        LOG_SCHED_DIR / "scheduler.log",
    )


# ── 对话轮次生命周期 ──────────────────────────────────────────────────────────

def start_turn(turn_id: str) -> None:
    """开启对话轮次日志记录。

    在当前 asyncio Task 的 ContextVar 中写入 turn_id，并打开对应的 conv 文件。
    之后本 Task 及其直接 await 的协程产生的所有日志记录均会写入该文件。

    Args:
        turn_id: 对话轮次 ID（通常是 uuid hex 字符串）。
    """
    if not turn_id:
        return
    path = LOG_CONV_DIR / f"turn_{turn_id}.log"
    h = logging.FileHandler(path, mode="a", encoding="utf-8")
    h.setLevel(logging.DEBUG)
    h.setFormatter(_PLAIN_FMT)
    with _turn_lock:
        _turn_handlers[turn_id] = h
    # 在当前 asyncio Task 的 context 副本中设置 turn_id
    _TURN_CTX.set(turn_id)


def end_turn(turn_id: str) -> None:
    """结束对话轮次日志记录，刷新并关闭对应的文件句柄。

    应在 finally 块中调用，保证 start_turn 之后无论正常还是异常都能执行。
    """
    if not turn_id:
        return
    with _turn_lock:
        h = _turn_handlers.pop(turn_id, None)
    if h is not None:
        try:
            h.flush()
            h.close()
        except Exception:
            pass
