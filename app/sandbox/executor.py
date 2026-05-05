"""沙箱执行器 — 在隔离环境中安全运行用户代码。

实现策略：
  - 每次执行分配独立临时目录，执行后自动清理
  - subprocess 运行，通过 asyncio.wait_for 强制超时
  - Linux 平台使用 resource 模块限制 CPU 时间和内存
  - 剥离网络代理环境变量，阻断出向请求识别
  - 执行前静态扫描阻断已知高危调用模式

支持语言：
  python   → python3 执行，捕获 stdout / stderr
  shell    → bash -n 仅语法检查（不实际运行）
  node/js  → node --check 仅语法检查
  其余     → 静态文本扫描，不实际执行

资源限制（可在 system_config.yaml sandbox 节配置）：
  timeout_sec  : 10   单次执行超时（秒）
  max_output   : 65536 stdout/stderr 最大字节数
  max_file_size: 10MB  单文件上限
"""

import asyncio
import logging
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 默认资源限制 ──────────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT  = 10        # 秒
_MAX_OUTPUT_BYTES = 64 * 1024 # 64 KB

# ── 高危调用模式（在执行前静态拦截）─────────────────────────────────────────
_BLOCKED_PATTERNS: List[str] = [
    "os.system",
    "os.popen",
    "subprocess.call",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_output",
    "__import__('os')",
    "__import__('subprocess')",
    "importlib.import_module",
    "eval(",
    "exec(",
    "compile(",
    "open(",          # 文件写操作会在 dangerous_ops 层拦截，此处仅警告
    "socket.connect",
    "urllib.request",
    "requests.get",
    "requests.post",
    "httpx.get",
    "httpx.post",
    "aiohttp",
]

# ── 不允许执行（仅语法检查）的语言 ───────────────────────────────────────────
_SYNTAX_ONLY_LANGS = {"shell", "bash", "sh", "javascript", "js", "typescript", "ts"}

# 代理相关环境变量（执行时剥离，避免沙箱内代码通过代理访问外网）
_PROXY_ENV_KEYS = {
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
}


@dataclass
class SandboxResult:
    """单次沙箱执行结果。"""
    success:          bool
    stdout:           str  = ""
    stderr:           str  = ""
    exit_code:        int  = 0
    execution_ms:     int  = 0
    language:         str  = ""
    blocked:          bool = False
    blocked_reason:   Optional[str] = None
    syntax_only:      bool = False    # True 表示只做了语法检查，未实际运行

    @property
    def safe_to_save(self) -> bool:
        """执行无异常且未被拦截，可以持久化到用户目录。"""
        return self.success and not self.blocked

    def to_dict(self) -> Dict:
        return {
            "success":        self.success,
            "stdout":         self.stdout,
            "stderr":         self.stderr,
            "exit_code":      self.exit_code,
            "execution_ms":   self.execution_ms,
            "language":       self.language,
            "blocked":        self.blocked,
            "blocked_reason": self.blocked_reason,
            "syntax_only":    self.syntax_only,
            "safe_to_save":   self.safe_to_save,
        }


class SandboxExecutor:
    """沙箱执行器（进程级单例，无状态，线程安全）。"""

    def __init__(
        self,
        timeout_sec:    int = _DEFAULT_TIMEOUT,
        max_output:     int = _MAX_OUTPUT_BYTES,
    ):
        self.timeout_sec = timeout_sec
        self.max_output  = max_output

    # ── 公共入口 ──────────────────────────────────────────────────────────────

    async def run(
        self,
        code:     str,
        language: str = "python",
        filename: Optional[str] = None,
    ) -> SandboxResult:
        """执行代码字符串，自动分派到对应语言处理器。"""
        lang = language.lower().strip(".")

        # 1. 静态拦截
        block = self._static_scan(code)
        if block:
            return SandboxResult(
                success=False, blocked=True, blocked_reason=block, language=lang
            )

        # 2. 语法检查类语言
        if lang in _SYNTAX_ONLY_LANGS:
            return await self._syntax_check(code, lang)

        # 3. Python 真实执行
        if lang in ("python", "python3", "py"):
            return await self._run_python(code)

        # 4. 其余语言：仅静态扫描，不执行
        return SandboxResult(
            success=True,
            language=lang,
            syntax_only=True,
            stdout=f"[沙箱] 语言 '{lang}' 暂不支持实际执行，已完成静态扫描。",
        )

    async def run_file(self, file_path: Path) -> SandboxResult:
        """对已落盘的文件执行沙箱检查。自动根据扩展名选择语言。"""
        suffix  = file_path.suffix.lower().lstrip(".")
        content = ""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return SandboxResult(
                success=False,
                stderr=f"读取文件失败: {e}",
                language=suffix,
            )
        return await self.run(content, language=suffix or "text")

    # ── Python 执行 ───────────────────────────────────────────────────────────

    async def _run_python(self, code: str) -> SandboxResult:
        tmp_dir = Path(tempfile.mkdtemp(prefix="sandbox_"))
        script  = tmp_dir / "script.py"
        try:
            script.write_text(code, encoding="utf-8")
            script.chmod(0o400)  # 只读，防止脚本自我修改

            cmd = [sys.executable, str(script)]
            env = self._clean_env()

            import time
            t0 = time.monotonic()
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=str(tmp_dir),
                        preexec_fn=self._set_resource_limits if sys.platform != "win32" else None,
                    ),
                    timeout=self.timeout_sec + 1,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_sec
                )
                elapsed_ms = int((time.monotonic() - t0) * 1000)

                stdout = stdout_bytes.decode("utf-8", errors="replace")[: self.max_output]
                stderr = stderr_bytes.decode("utf-8", errors="replace")[: self.max_output]

                return SandboxResult(
                    success      = proc.returncode == 0,
                    stdout       = stdout,
                    stderr       = stderr,
                    exit_code    = proc.returncode,
                    execution_ms = elapsed_ms,
                    language     = "python",
                )

            except asyncio.TimeoutError:
                return SandboxResult(
                    success       = False,
                    stderr        = f"执行超时（>{self.timeout_sec}s）",
                    exit_code     = -1,
                    language      = "python",
                    blocked       = True,
                    blocked_reason= "timeout",
                )

        except Exception as e:
            logger.error("沙箱执行异常: %s", e)
            return SandboxResult(
                success=False, stderr=str(e), exit_code=-1, language="python"
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 语法检查（shell / js / ts）───────────────────────────────────────────

    async def _syntax_check(self, code: str, lang: str) -> SandboxResult:
        tmp_dir = Path(tempfile.mkdtemp(prefix="sandbox_"))
        ext_map = {
            "shell": "sh", "bash": "sh", "sh": "sh",
            "javascript": "js", "js": "js",
            "typescript": "ts", "ts": "ts",
        }
        ext    = ext_map.get(lang, lang)
        script = tmp_dir / f"check.{ext}"
        try:
            script.write_text(code, encoding="utf-8")

            if ext == "sh":
                cmd = ["bash", "-n", str(script)]
            elif ext == "js":
                cmd = ["node", "--check", str(script)]
            else:
                # ts 等先尝试 node --check（ts-node 可选）
                cmd = ["node", "--check", str(script)]

            # 检查命令是否可用
            import shutil as sh
            if not sh.which(cmd[0]):
                return SandboxResult(
                    success=True, language=lang, syntax_only=True,
                    stdout=f"[沙箱] {cmd[0]} 未安装，跳过语法检查；已完成静态扫描。",
                )

            import time
            t0   = time.monotonic()
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(tmp_dir),
                ),
                timeout=10,
            )
            out, err = await proc.communicate()
            elapsed  = int((time.monotonic() - t0) * 1000)

            return SandboxResult(
                success      = proc.returncode == 0,
                stdout       = out.decode("utf-8", errors="replace")[:self.max_output],
                stderr       = err.decode("utf-8", errors="replace")[:self.max_output],
                exit_code    = proc.returncode,
                execution_ms = elapsed,
                language     = lang,
                syntax_only  = True,
            )

        except asyncio.TimeoutError:
            return SandboxResult(
                success=False, blocked=True, blocked_reason="timeout",
                language=lang, syntax_only=True,
            )
        except Exception as e:
            return SandboxResult(
                success=False, stderr=str(e), language=lang, syntax_only=True
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _static_scan(code: str) -> Optional[str]:
        """静态扫描高危调用模式，返回拦截原因或 None（通过）。"""
        for pattern in _BLOCKED_PATTERNS:
            if pattern in code:
                return f"代码包含高危调用: {pattern!r}"
        return None

    @staticmethod
    def _clean_env() -> Dict[str, str]:
        """返回剥离代理和敏感变量后的干净环境，防止沙箱代码借用宿主网络。"""
        env = {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_KEYS}
        # 额外剥除 API Key 类变量
        env = {k: v for k, v in env.items()
               if not any(s in k.upper() for s in ("API_KEY", "SECRET", "PASSWORD", "TOKEN"))}
        return env

    @staticmethod
    def _set_resource_limits() -> None:
        """在子进程 preexec_fn 中设置 CPU / 内存 / 文件大小限制（Linux/macOS）。"""
        try:
            import resource
            # CPU 时间：最多 12 秒（比 timeout 略宽，确保进程正常退出）
            resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
            # 虚拟内存：256 MB
            mem = 256 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            # 单文件写入上限：16 MB
            resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
            # 最多打开 32 个文件描述符
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        except Exception:
            pass  # Windows 或权限不足时静默跳过


# 全局单例
sandbox = SandboxExecutor()
