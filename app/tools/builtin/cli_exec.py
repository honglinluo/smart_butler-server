"""
【模块说明】命令行执行工具（CliExecTool）— 让 AI 能执行系统命令（高危，需授权）

这是一个强大但危险的工具，允许 AI 在服务器上执行任意系统命令。
由于执行系统命令可能影响服务器安全，每次调用都必须经过用户明确授权。

【主要特性】
  跨平台支持  — 自动识别 Windows / Linux / macOS，选择正确的 Shell
  编码处理    — 自动探测 UTF-8 / GBK，正确显示中文命令输出
  完整结果    — 返回 exit_code（退出码）/ stdout（标准输出）/ stderr（错误输出）/ 执行时长
  结果断言    — 可选 expected 参数，检查输出是否包含期望内容

【Shell 选择策略】
  Windows — 优先 PowerShell，备选 cmd.exe
  Linux   — 优先 bash，备选 sh
  macOS   — 优先 zsh，备选 bash → sh

【安全机制】
  dangerous_ops=["cli"]，每次调用前都触发授权检查弹窗

CLI 命令执行工具 — 自动识别操作系统，选择正确的 Shell 执行命令。

特性：
  自动 OS 识别  — Windows / Linux / macOS，选择对应 Shell
  Shell 优先级  — Windows: PowerShell → cmd.exe
                  Linux:   bash → sh
                  macOS:   zsh → bash → sh
  输出编码处理  — 自动探测 UTF-8 / GBK / 系统编码，支持 chardet 增强
  完整结果返回  — exit_code / stdout / stderr / 执行时长 / 超时标志
  结果断言      — 可选 expected 参数；若指定则检查输出中是否包含期望内容，
                  匹配时 status=pass，否则 status=fail；未指定时 status 始终为 pass
  安全机制      — dangerous_ops=["cli"]，每次调用触发用户授权核查
"""

from __future__ import annotations

import asyncio
import locale
import logging
import os
import platform
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from app.tools.base import BaseTool, EXEC_SERVER, VIS_PUBLIC
from app.tools.decorators import tool

logger = logging.getLogger(__name__)

# ── 输出截断阈值 ──────────────────────────────────────────────────────────────
_DEFAULT_MAX_OUTPUT = 50_000   # 每条流（stdout/stderr）最多保留字符数
_TAIL_KEEP          = 2_000    # 截断时保留末尾行数对应字符数（避免丢失关键错误信息）

# ══════════════════════════════════════════════════════════════════════════════
# OS 与 Shell 检测
# ══════════════════════════════════════════════════════════════════════════════

class _OsInfo:
    """缓存当前环境的 OS / Shell 信息（进程级单例）。"""
    _cache: Optional["_OsInfo"] = None

    def __init__(self) -> None:
        self.system   = platform.system()          # 'Windows' / 'Linux' / 'Darwin'
        self.release  = platform.release()
        self.machine  = platform.machine()
        self.python   = sys.version.split()[0]
        self.encoding = locale.getpreferredencoding(False) or "utf-8"

        # Shell 候选列表（按优先级）
        if self.system == "Windows":
            self.shell_candidates = self._win_shells()
        elif self.system == "Darwin":
            self.shell_candidates = self._mac_shells()
        else:
            self.shell_candidates = self._linux_shells()

        # 选定的默认 Shell
        self.default_shell, self.shell_args = self.shell_candidates[0]

    # ── Windows ──────────────────────────────────────────────────────────────
    @staticmethod
    def _win_shells() -> List[Tuple[str, List[str]]]:
        candidates = []
        # PowerShell 7+（pwsh）
        if shutil.which("pwsh"):
            candidates.append(("pwsh", ["-NoProfile", "-NonInteractive", "-Command"]))
        # Windows PowerShell 5.x
        if shutil.which("powershell"):
            candidates.append(("powershell", ["-NoProfile", "-NonInteractive", "-Command"]))
        # cmd.exe（兜底）
        cmd = os.environ.get("ComSpec", "cmd.exe")
        candidates.append((cmd, ["/c"]))
        return candidates

    # ── macOS ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _mac_shells() -> List[Tuple[str, List[str]]]:
        candidates = []
        for sh in ("/bin/zsh", "/bin/bash", "/usr/local/bin/bash", "/bin/sh"):
            if os.path.isfile(sh):
                candidates.append((sh, ["-c"]))
        return candidates or [("/bin/sh", ["-c"])]

    # ── Linux ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _linux_shells() -> List[Tuple[str, List[str]]]:
        candidates = []
        for sh in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash", "/bin/sh"):
            if os.path.isfile(sh):
                candidates.append((sh, ["-c"]))
        return candidates or [("/bin/sh", ["-c"])]

    @classmethod
    def get(cls) -> "_OsInfo":
        if cls._cache is None:
            cls._cache = cls()
        return cls._cache

    def resolve_shell(self, shell_type: str) -> Tuple[str, List[str]]:
        """根据 shell_type 参数返回 (shell_path, args)。"""
        st = shell_type.lower().strip()
        mapping = {
            "powershell": ("pwsh" if shutil.which("pwsh") else "powershell",
                           ["-NoProfile", "-NonInteractive", "-Command"]),
            "pwsh":       ("pwsh", ["-NoProfile", "-NonInteractive", "-Command"]),
            "cmd":        (os.environ.get("ComSpec", "cmd.exe"), ["/c"]),
            "bash":       (shutil.which("bash") or "/bin/bash", ["-c"]),
            "sh":         (shutil.which("sh") or "/bin/sh", ["-c"]),
            "zsh":        (shutil.which("zsh") or "/bin/zsh", ["-c"]),
            "fish":       (shutil.which("fish") or "/usr/bin/fish", ["-c"]),
        }
        if st in mapping:
            path, args = mapping[st]
            if path and (shutil.which(path) or os.path.isfile(path)):
                return path, args
        return self.default_shell, self.shell_args

    def to_dict(self) -> Dict[str, str]:
        return {
            "system":   self.system,
            "release":  self.release,
            "machine":  self.machine,
            "shell":    self.default_shell,
            "encoding": self.encoding,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 输出解码
# ══════════════════════════════════════════════════════════════════════════════

def _decode(raw: bytes, preferred: str = "utf-8") -> str:
    """多级尝试解码字节流：preferred → 系统编码 → chardet → replace 兜底。"""
    if not raw:
        return ""

    for enc in (preferred, locale.getpreferredencoding(False), "utf-8"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass

    # chardet 自动检测（可选依赖）
    try:
        import chardet  # type: ignore
        det = chardet.detect(raw)
        enc = det.get("encoding") or "utf-8"
        return raw.decode(enc, errors="replace")
    except ImportError:
        pass

    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, max_chars: int) -> Tuple[str, bool]:
    """截断过长输出，保留头部 + 末尾，中间插入省略提示。"""
    if len(text) <= max_chars:
        return text, False

    head = max_chars - _TAIL_KEEP
    if head < 0:
        head = max_chars // 2
        tail = max_chars - head
    else:
        tail = _TAIL_KEEP

    omit_chars = len(text) - head - tail
    result = (
        text[:head]
        + f"\n\n... [已省略 {omit_chars:,} 字符] ...\n\n"
        + text[-tail:]
    )
    return result, True


# ══════════════════════════════════════════════════════════════════════════════
# 核心执行函数
# ══════════════════════════════════════════════════════════════════════════════

async def _run_command(
    command:    str,
    shell_type: str,
    cwd:        Optional[str],
    env_extra:  Dict[str, str],
    timeout:    int,
    stdin_data: Optional[str],
    encoding:   str,
    max_output: int,
) -> Dict[str, Any]:
    os_info = _OsInfo.get()
    shell, shell_args = os_info.resolve_shell(shell_type)

    # 构造完整进程参数
    argv = [shell, *shell_args, command]

    # 合并环境变量
    env = {**os.environ, **env_extra} if env_extra else None

    # 验证工作目录
    effective_cwd: Optional[str] = None
    if cwd:
        if os.path.isdir(cwd):
            effective_cwd = cwd
        else:
            return {
                "success":    False,
                "exit_code":  -1,
                "stdout":     "",
                "stderr":     f"工作目录不存在或不是目录: {cwd}",
                "command":    command,
                "shell":      shell,
                "os_name":    os_info.system,
                "duration_ms": 0,
                "timed_out":  False,
                "truncated":  {},
            }

    stdin_bytes = stdin_data.encode(encoding) if stdin_data else None
    t0 = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_bytes else asyncio.subprocess.DEVNULL,
            cwd=effective_cwd,
            env=env,
        )

        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(stdin_bytes),
                timeout=timeout,
            )
            timed_out = False
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                raw_out, raw_err = await proc.communicate()
            except Exception:
                raw_out, raw_err = b"", b""
            timed_out = True

    except FileNotFoundError:
        return {
            "success":    False,
            "exit_code":  -1,
            "stdout":     "",
            "stderr":     f"Shell 未找到: {shell}，请确认已安装",
            "command":    command,
            "shell":      shell,
            "os_name":    os_info.system,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "timed_out":  False,
            "truncated":  {},
        }
    except Exception as exc:
        return {
            "success":    False,
            "exit_code":  -1,
            "stdout":     "",
            "stderr":     f"进程启动失败: {exc}",
            "command":    command,
            "shell":      shell,
            "os_name":    os_info.system,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "timed_out":  False,
            "truncated":  {},
        }

    duration_ms = int((time.monotonic() - t0) * 1000)
    exit_code   = proc.returncode if not timed_out else -1

    stdout_raw = _decode(raw_out, encoding)
    stderr_raw = _decode(raw_err, encoding)

    stdout, out_trunc = _truncate(stdout_raw, max_output)
    stderr, err_trunc = _truncate(stderr_raw, max_output)

    truncated: Dict[str, Any] = {}
    if out_trunc:
        truncated["stdout"] = {"original_chars": len(stdout_raw), "kept_chars": max_output}
    if err_trunc:
        truncated["stderr"] = {"original_chars": len(stderr_raw), "kept_chars": max_output}

    return {
        "success":    exit_code == 0 and not timed_out,
        "exit_code":  exit_code,
        "stdout":     stdout,
        "stderr":     stderr,
        "command":    command,
        "shell":      shell,
        "shell_args": shell_args,
        "os_name":    os_info.system,
        "duration_ms": duration_ms,
        "timed_out":  timed_out,
        "truncated":  truncated,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 工具类
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="cli_exec",
    description=(
        "在服务端执行 CLI 命令，自动识别操作系统并选择合适的 Shell。\n"
        "Windows 优先使用 PowerShell（pwsh → powershell → cmd），"
        "Linux 使用 bash → sh，macOS 使用 zsh → bash → sh。\n"
        "返回完整执行结果：result（stdout）/ log（stderr + 执行信息）/ status（pass/fail）"
        "/ exit_code / 执行时长 / 超时标志。\n"
        "可通过 expected 参数对输出内容进行断言：输出中包含期望字符串则 status=pass，"
        "否则 status=fail；不传 expected 时 status 始终为 pass。\n"
        "⚠ 此工具执行系统命令，需要用户授权（dangerous_ops=cli）。"
    ),
    exec_location=EXEC_SERVER,
    visibility=VIS_PUBLIC,
    dangerous_ops=["cli"],
    parameters={
        "command": {
            "type":        "string",
            "description": "要执行的 CLI 命令（支持管道、重定向、多行命令）",
            "required":    True,
        },
        "expected": {
            "type":        "string",
            "description": (
                "期望结果（可选）。若指定，工具将断言 stdout 中包含此字符串（子串匹配）："
                "包含则 status=pass，否则 status=fail。不传或为空时跳过断言，status 始终为 pass。\n"
                "注意：此字段仅检查 stdout，不检查 stderr；命令本身成功与否由 success/exit_code 判断。\n"
                "包安装验证建议：不要依赖 pip 输出，改用 `python -c \"import <pkg>; print('ok')\"` 单独确认。"
            ),
            "required":    False,
            "default":     None,
        },
        "cwd": {
            "type":        "string",
            "description": "工作目录（绝对路径），默认使用服务进程当前目录",
            "required":    False,
        },
        "env": {
            "type":        "object",
            "description": "额外注入的环境变量键值对，与系统环境变量合并",
            "required":    False,
        },
        "timeout": {
            "type":        "integer",
            "description": "执行超时秒数，超时后强制终止进程，默认 60",
            "default":     60,
        },
        "shell_type": {
            "type":        "string",
            "description": (
                "指定 Shell 类型（auto/powershell/pwsh/cmd/bash/sh/zsh/fish），"
                "默认 auto（按 OS 自动选择）"
            ),
            "default":     "auto",
        },
        "stdin": {
            "type":        "string",
            "description": "传给命令的标准输入内容（可选）",
            "required":    False,
        },
        "encoding": {
            "type":        "string",
            "description": "输出解码编码，默认 utf-8；Windows 中文环境可设为 gbk",
            "default":     "utf-8",
        },
        "max_output": {
            "type":        "integer",
            "description": "stdout / stderr 各自最大保留字符数，超出部分省略，默认 50000",
            "default":     50_000,
        },
    },
)
class CliExecTool(BaseTool):
    async def execute(self, params: dict, context: dict) -> dict:
        command    = params.get("command", "").strip()
        expected   = params.get("expected") or None
        cwd        = params.get("cwd") or None
        env_extra  = params.get("env") or {}
        timeout    = max(1, int(params.get("timeout", 60)))
        shell_type = (params.get("shell_type") or "auto").strip()
        stdin_data = params.get("stdin") or None
        encoding   = (params.get("encoding") or "utf-8").strip()
        max_output = max(100, int(params.get("max_output", _DEFAULT_MAX_OUTPUT)))

        if not command:
            return {
                "status":    "pass",
                "result":    "",
                "log":       "command 参数不能为空",
                "success":   False,
                "exit_code": -1,
                "stdout":    "",
                "stderr":    "command 参数不能为空",
                "command":   "",
                "shell":     "",
                "os_name":   _OsInfo.get().system,
                "duration_ms": 0,
                "timed_out": False,
                "truncated": {},
            }

        if not isinstance(env_extra, dict):
            env_extra = {}

        # shell_type="auto" 时使用 OS 默认 Shell
        if shell_type == "auto":
            shell_type = _OsInfo.get().default_shell

        raw = await _run_command(
            command    = command,
            shell_type = shell_type,
            cwd        = cwd,
            env_extra  = env_extra,
            timeout    = timeout,
            stdin_data = stdin_data,
            encoding   = encoding,
            max_output = max_output,
        )

        # 追加超时提示到 stderr
        if raw.get("timed_out"):
            raw["stderr"] = (
                f"[命令执行超时（{timeout}s），进程已被强制终止]\n" + raw["stderr"]
            ).strip()

        # ── result：命令标准输出 ───────────────────────────────────────────────
        result_text: str = raw["stdout"]

        # ── log：执行元信息 + stderr ──────────────────────────────────────────
        log_parts = [
            f"[执行信息] OS={raw['os_name']}  Shell={raw['shell']}"
            f"  exit_code={raw['exit_code']}  耗时={raw['duration_ms']}ms",
        ]
        if raw["stderr"]:
            log_parts.append(f"[stderr]\n{raw['stderr']}")
        log_text = "\n".join(log_parts)

        # ── status：断言 ──────────────────────────────────────────────────────
        if expected is not None and str(expected).strip():
            needle = str(expected).strip()
            matched = needle in result_text
            status  = "pass" if matched else "fail"
            verdict = "匹配 ✓" if matched else "不匹配 ✗"
            log_text += f"\n[断言] 期望包含: {needle!r} → {verdict}"
        else:
            status = "pass"

        # 记录日志（不含完整输出，避免日志膨胀）
        logger.info(
            "[cli_exec] os=%s shell=%s exit=%s dur=%dms status=%s cmd=%.100s",
            raw["os_name"], raw["shell"], raw["exit_code"],
            raw["duration_ms"], status, command,
        )

        return {
            "status":      status,
            "result":      result_text,
            "log":         log_text,
            # 保留原始字段方便调试
            "success":     raw["success"],
            "exit_code":   raw["exit_code"],
            "stdout":      raw["stdout"],
            "stderr":      raw["stderr"],
            "command":     raw["command"],
            "shell":       raw["shell"],
            "shell_args":  raw.get("shell_args", []),
            "os_name":     raw["os_name"],
            "duration_ms": raw["duration_ms"],
            "timed_out":   raw["timed_out"],
            "truncated":   raw["truncated"],
        }
