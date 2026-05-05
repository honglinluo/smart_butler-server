"""内容扫描器 — 文件类型识别、代码块提取、图片安全检测。

主要职责：
  1. 识别上传文件的真实类型（magic bytes 优先，后缀兜底）
  2. 从长文本中提取 Markdown 代码围栏和启发式代码片段
  3. 检测图片是否为"polyglot"（同时是合法代码的双重文件）
  4. 对文本内容做恶意模式扫描（高危 shell、系统命令等）
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 文件类型分类 ──────────────────────────────────────────────────────────────

# 允许上传的扩展名 → 分类
_EXT_MAP = {
    # 代码
    ".py":    "code",  ".js":  "code", ".ts":  "code",
    ".sh":    "code",  ".bash":"code", ".rb":  "code",
    ".go":    "code",  ".rs":  "code", ".cpp": "code",
    ".c":     "code",  ".h":   "code", ".java":"code",
    ".php":   "code",  ".sql": "code", ".r":   "code",
    # 文本/数据
    ".txt":   "text",  ".md":  "text", ".rst": "text",
    ".csv":   "data",  ".json":"data", ".yaml":"data",
    ".xml":   "data",  ".toml":"data", ".ini": "data",
    # 图片
    ".png":   "image", ".jpg": "image", ".jpeg":"image",
    ".gif":   "image", ".webp":"image", ".bmp": "image",
    ".svg":   "image",
    # 文档
    ".pdf":   "doc",   ".docx":"doc",  ".xlsx":"doc",
}

# 文件 magic bytes（前 4 字节）→ (真实类型, 描述)
_MAGIC_BYTES: List[Tuple[bytes, str, str]] = [
    (b"\x89PNG",           "image", "PNG"),
    (b"\xff\xd8\xff",      "image", "JPEG"),
    (b"GIF8",              "image", "GIF"),
    (b"RIFF",              "image", "WebP/WAV"),
    (b"%PDF",              "doc",   "PDF"),
    (b"PK\x03\x04",        "doc",   "ZIP/DOCX/XLSX"),
    (b"\x7fELF",           "binary","ELF 可执行文件"),
    (b"MZ",                "binary","PE 可执行文件"),
    (b"\xca\xfe\xba\xbe",  "binary","Java .class"),
]

# 禁止上传的文件类型
_BLOCKED_TYPES = {"binary"}

# 最大文件大小
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# ── 代码块提取 ────────────────────────────────────────────────────────────────

# Markdown 代码围栏：```python\n...\n``` 或 ~~~...~~~
_FENCE_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)\n(?P<code>.*?)```|~~~(?P<lang2>[a-zA-Z0-9_+-]*)\n(?P<code2>.*?)~~~",
    re.DOTALL,
)

# 启发式 Python 代码特征（连续 3 行满足其中 2 条即认为是代码）
_PY_HINTS = [
    re.compile(r"^(import |from .+ import )", re.MULTILINE),
    re.compile(r"^(def |class |async def )", re.MULTILINE),
    re.compile(r"^\s{4}[a-zA-Z_]", re.MULTILINE),  # 4 空格缩进
    re.compile(r"^\s*(if|for|while|try|with|return|yield)\b", re.MULTILINE),
    re.compile(r"#.*$", re.MULTILINE),   # 注释
]

# 高危 Shell 模式（在文本中检测）
_SHELL_DANGER = re.compile(
    r"\b(rm\s+-[rf]+|chmod\s+777|curl\s+.+\|\s*bash|wget\s+.+\|\s*sh"
    r"|mkfs|dd\s+if=|nc\s+-l|/etc/passwd|/etc/shadow)\b",
    re.IGNORECASE,
)


@dataclass
class CodeBlock:
    """从文本中提取出的单个代码片段。"""
    language:   str
    code:       str
    source:     str  = "fence"   # "fence" | "heuristic"
    line_start: int  = 0


@dataclass
class ScanResult:
    """文件或文本的扫描结果。"""
    file_type:       str            = "unknown"   # code/text/data/image/doc/binary
    real_type:       str            = "unknown"   # magic bytes 识别的真实类型
    extension:       str            = ""
    file_size:       int            = 0
    blocked:         bool           = False
    blocked_reason:  Optional[str]  = None
    code_blocks:     List[CodeBlock] = field(default_factory=list)
    danger_patterns: List[str]      = field(default_factory=list)
    is_text_readable: bool          = True


class ContentScanner:
    """内容扫描器（无状态，静态方法集合）。"""

    # ── 文件扫描 ──────────────────────────────────────────────────────────────

    @staticmethod
    def scan_file(path: Path, content: Optional[bytes] = None) -> ScanResult:
        """扫描已落盘或内存中的文件，返回类型识别 + 安全评估结果。"""
        result = ScanResult(extension=path.suffix.lower())

        # 1. 大小检查
        if content is not None:
            result.file_size = len(content)
        elif path.exists():
            result.file_size = path.stat().st_size

        if result.file_size > MAX_FILE_SIZE:
            result.blocked       = True
            result.blocked_reason = f"文件大小 {result.file_size // 1024}KB 超过 {MAX_FILE_SIZE // 1024 // 1024}MB 上限"
            return result

        # 2. Magic bytes 识别真实类型
        raw = content if content is not None else (
            path.read_bytes()[:16] if path.exists() else b""
        )
        result.real_type = ContentScanner._magic_type(raw[:16])
        result.file_type = _EXT_MAP.get(result.extension, result.real_type)

        # 3. 拦截二进制可执行文件
        if result.real_type in _BLOCKED_TYPES:
            result.blocked       = True
            result.blocked_reason = f"禁止上传可执行文件（{result.real_type}）"
            return result

        # 4. 图片特殊处理：检测 polyglot（同时是合法 Python/Shell）
        if result.real_type == "image":
            ContentScanner._check_image_polyglot(raw, result)
            return result

        # 5. 文本类：提取代码块 + 危险模式扫描
        text = ""
        try:
            text_bytes = content if content is not None else (
                path.read_bytes() if path.exists() else b""
            )
            text = text_bytes.decode("utf-8", errors="replace")
            result.is_text_readable = True
        except Exception:
            result.is_text_readable = False

        if text:
            result.code_blocks    = ContentScanner.extract_code_blocks(text)
            result.danger_patterns = ContentScanner.find_danger_patterns(text)

        return result

    @staticmethod
    def _magic_type(header: bytes) -> str:
        for magic, ftype, _ in _MAGIC_BYTES:
            if header.startswith(magic):
                return ftype
        return "text"

    @staticmethod
    def _check_image_polyglot(raw: bytes, result: ScanResult) -> None:
        """检测图片末尾是否附加了可执行代码（Polyglot 攻击）。"""
        try:
            tail = raw[-512:].decode("utf-8", errors="ignore")
            py_count = sum(1 for p in _PY_HINTS if p.search(tail))
            if py_count >= 2:
                result.blocked       = True
                result.blocked_reason = "图片疑似包含嵌入代码（Polyglot 检测）"
        except Exception:
            pass

    # ── 文本代码提取 ──────────────────────────────────────────────────────────

    @staticmethod
    def extract_code_blocks(text: str) -> List[CodeBlock]:
        """从 Markdown 文本中提取所有代码块（围栏式 + 启发式）。"""
        blocks: List[CodeBlock] = []

        # 1. Markdown 围栏代码块
        for m in _FENCE_RE.finditer(text):
            lang = (m.group("lang") or m.group("lang2") or "text").strip().lower()
            code = (m.group("code") or m.group("code2") or "").strip()
            if code:
                line_start = text[: m.start()].count("\n") + 1
                blocks.append(CodeBlock(language=lang, code=code,
                                        source="fence", line_start=line_start))

        # 2. 如果没有找到围栏块，尝试启发式识别整段文本
        if not blocks:
            heuristic = ContentScanner._heuristic_code(text)
            if heuristic:
                blocks.append(heuristic)

        return blocks

    @staticmethod
    def _heuristic_code(text: str) -> Optional[CodeBlock]:
        """对无围栏标记的文本做启发式 Python 代码识别。"""
        matched = sum(1 for p in _PY_HINTS if p.search(text))
        if matched >= 2:
            lang = "python"
            # 尝试判断是否更像 Shell
            if re.search(r"^#!/", text, re.MULTILINE):
                lang = "shell"
            return CodeBlock(language=lang, code=text.strip(), source="heuristic")
        return None

    # ── 危险模式扫描 ──────────────────────────────────────────────────────────

    @staticmethod
    def find_danger_patterns(text: str) -> List[str]:
        """在文本中扫描高危 Shell 命令模式，返回匹配列表。"""
        found = []
        for m in _SHELL_DANGER.finditer(text):
            found.append(m.group(0).strip())
        return found

    # ── 便捷方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def classify_language(code: str, hint: str = "") -> str:
        """根据代码内容猜测语言，hint 为文件扩展名或用户指定语言。"""
        h = hint.lower().lstrip(".")
        if h in ("py", "python"):
            return "python"
        if h in ("js", "javascript"):
            return "javascript"
        if h in ("sh", "bash", "shell"):
            return "shell"
        if h:
            return h

        # 启发式
        if re.search(r"^(import |from .+ import |def |class |async def )", code, re.MULTILINE):
            return "python"
        if re.search(r"^(function |const |let |var |=>)", code, re.MULTILINE):
            return "javascript"
        if re.search(r"^(#!/|echo |grep |sed |awk )", code, re.MULTILINE):
            return "shell"
        return "text"


# 全局单例
scanner = ContentScanner()
