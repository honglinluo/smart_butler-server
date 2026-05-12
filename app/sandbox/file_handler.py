"""
【模块说明】文件处理器（File Handler）— 用户上传文件的"验收流水线"

当用户向 AI 发送一个文件（代码、图片、文档等），系统不会直接保存，
而是先走一遍完整的安全检查流程，确认安全后才存到服务器上。

【处理步骤（三步流水线）】
  1. 扫描（Scan）
     用内容扫描器检查文件类型、提取代码块、扫描恶意模式

  2. 沙箱验证（Execute/Validate）
     如果是代码文件，在沙箱中执行（或做语法检查），看看有无危险行为

  3. 持久化（Save）
     安全验证通过后，按文件类型分类存入用户专属目录

【用户文件目录结构】
  {上传根目录}/{用户ID}/uploads/
    ├── code/    代码文件（.py/.js/.sh 等）
    ├── images/  图片文件
    ├── files/   文档和数据文件（.pdf/.csv 等）
    └── text/    长文本内容

沙箱文件处理器 — 上传内容的完整处理流水线。

流水线：扫描 → 沙箱执行/校验 → 安全则持久化到用户目录

目录结构：
  {UPLOAD_ROOT}/{user_id}/uploads/
    ├── code/    Python / JS / Shell 等代码文件
    ├── images/  通过校验的图片
    ├── files/   文本、数据、文档等
    └── text/    长文本片段（含代码块检测）
"""

import hashlib
import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.core.paths import PROJECT_ROOT
from app.core.file_storage import UPLOAD_ROOT, SUBDIR_UPLOADS
from app.sandbox.executor import SandboxExecutor, SandboxResult, sandbox
from app.sandbox.scanner import CodeBlock, ContentScanner, ScanResult, scanner

logger = logging.getLogger(__name__)

# 最大并发处理文件数
MAX_FILES_PER_REQUEST = 5


@dataclass
class ProcessedFile:
    """单个上传内容的处理结果。"""
    original_name:  str
    content_type:   str                  # code / image / text / data / doc
    scan:           ScanResult           = field(default_factory=ScanResult)
    sandbox_results: List[SandboxResult] = field(default_factory=list)
    saved_path:     Optional[str]        = None   # 相对于 PROJECT_ROOT
    safe:           bool                 = False
    error:          Optional[str]        = None

    def to_dict(self) -> dict:
        return {
            "original_name":   self.original_name,
            "content_type":    self.content_type,
            "safe":            self.safe,
            "saved_path":      self.saved_path,
            "sandbox_results": [r.to_dict() for r in self.sandbox_results],
            "code_blocks_found": len(self.scan.code_blocks),
            "danger_patterns": self.scan.danger_patterns,
            "blocked":         self.scan.blocked,
            "blocked_reason":  self.scan.blocked_reason,
            "error":           self.error,
        }


class FileHandler:
    """上传文件的处理器，封装扫描 + 执行 + 存储完整流程。"""

    def __init__(self, executor: SandboxExecutor = sandbox):
        self._exec = executor

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def process_upload(
        self,
        filename: str,
        content:  bytes,
        user_id:  str,
    ) -> ProcessedFile:
        """处理单个上传文件。返回 ProcessedFile，safe=True 且 saved_path 非空表示已落盘。"""
        result = ProcessedFile(
            original_name=filename,
            content_type="unknown",
        )

        # 1. 扫描（类型识别 + 危险模式）
        tmp_path   = Path(f"/tmp/upload_{uuid.uuid4().hex}_{filename}")
        try:
            tmp_path.write_bytes(content)
            result.scan = scanner.scan_file(tmp_path, content=content)
        finally:
            tmp_path.unlink(missing_ok=True)

        result.content_type = result.scan.file_type

        if result.scan.blocked:
            result.error = result.scan.blocked_reason
            return result

        if result.scan.danger_patterns:
            result.error = f"检测到高危命令模式: {result.scan.danger_patterns[:3]}"
            return result

        # 2. 沙箱执行 / 校验
        text = content.decode("utf-8", errors="replace") if result.scan.is_text_readable else ""

        if result.content_type == "code":
            lang = scanner.classify_language(text, hint=Path(filename).suffix)
            sr   = await self._exec.run(text, language=lang)
            result.sandbox_results.append(sr)
            if not sr.safe_to_save:
                result.error = sr.blocked_reason or sr.stderr[:200] or "沙箱执行失败"
                return result

        elif result.content_type == "image":
            # 图片无法执行，校验通过即可保存
            pass

        elif result.content_type in ("text", "data"):
            # 文本内检测到代码块则逐块在沙箱中运行
            for block in result.scan.code_blocks:
                sr = await self._exec.run(block.code, language=block.language)
                result.sandbox_results.append(sr)
                if sr.blocked:
                    result.error = sr.blocked_reason
                    return result

        # 3. 全部通过 → 落盘
        save_dir = self._user_dir(user_id, result.content_type)
        save_dir.mkdir(parents=True, exist_ok=True)

        safe_name   = self._safe_filename(filename)
        dest        = save_dir / safe_name
        # 同名文件加 hash 后缀避免覆盖
        if dest.exists():
            h        = hashlib.md5(content).hexdigest()[:6]
            stem, ext = Path(safe_name).stem, Path(safe_name).suffix
            dest     = save_dir / f"{stem}_{h}{ext}"

        dest.write_bytes(content)
        result.saved_path = str(dest.relative_to(PROJECT_ROOT))
        result.safe       = True
        logger.info("文件已保存: %s → %s", filename, result.saved_path)
        return result

    async def process_text(
        self,
        text:    str,
        user_id: str,
        label:   str = "paste",
    ) -> ProcessedFile:
        """处理用户粘贴的长文本：检测代码块并在沙箱中执行，最终保存原始文本。"""
        filename = f"{label}.txt"
        result   = ProcessedFile(original_name=filename, content_type="text")

        # 扫描：提取代码块
        result.scan = ScanResult(
            file_type        = "text",
            real_type        = "text",
            code_blocks      = ContentScanner.extract_code_blocks(text),
            danger_patterns  = ContentScanner.find_danger_patterns(text),
            is_text_readable = True,
        )

        if result.scan.danger_patterns:
            result.error = f"检测到高危命令: {result.scan.danger_patterns[:3]}"
            return result

        # 对每个代码块运行沙箱
        for block in result.scan.code_blocks:
            sr = await self._exec.run(block.code, language=block.language)
            result.sandbox_results.append(sr)
            if sr.blocked:
                result.error = sr.blocked_reason
                return result

        # 落盘（保存原始长文本）
        save_dir = self._user_dir(user_id, "text")
        save_dir.mkdir(parents=True, exist_ok=True)

        safe_name = self._safe_filename(filename)
        dest      = save_dir / safe_name
        if dest.exists():
            import hashlib
            h = hashlib.md5(text.encode()).hexdigest()[:6]
            dest = save_dir / f"{Path(safe_name).stem}_{h}.txt"

        dest.write_text(text, encoding="utf-8")
        result.saved_path = str(dest.relative_to(PROJECT_ROOT))
        result.safe       = True
        return result

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _user_dir(user_id: str, content_type: str) -> Path:
        subdir_map = {
            "code":  "code",
            "image": "images",
            "text":  "files",
            "data":  "files",
            "doc":   "files",
        }
        subdir = subdir_map.get(content_type, "files")
        return UPLOAD_ROOT / user_id / SUBDIR_UPLOADS / subdir

    @staticmethod
    def _safe_filename(name: str) -> str:
        """过滤文件名中的危险字符，只保留字母、数字、下划线、连字符、点。"""
        import re
        safe = re.sub(r"[^\w.\-]", "_", Path(name).name)
        return safe[:128]  # 截断超长名称


# 全局单例
file_handler = FileHandler()
