"""
【模块说明】沙箱模块（Sandbox）— 安全执行代码与处理用户上传文件

这个包包含三个核心组件：
  - executor.py   沙箱执行器：在隔离环境中运行 Python/Shell 代码
  - scanner.py    内容扫描器：检测文件类型、提取代码块、拦截危险内容
  - file_handler.py 文件处理器：用户上传文件的扫描→验证→保存完整流水线

对外暴露的主要对象：
  sandbox       — 全局沙箱执行器单例
  scanner       — 全局内容扫描器单例
  file_handler  — 全局文件处理器单例

沙箱模块 — 安全执行与文件处理。
"""

from app.sandbox.executor import SandboxExecutor, SandboxResult, sandbox
from app.sandbox.scanner import ContentScanner, CodeBlock, ScanResult, scanner
from app.sandbox.file_handler import FileHandler, ProcessedFile, file_handler

__all__ = [
    "SandboxExecutor", "SandboxResult", "sandbox",
    "ContentScanner", "CodeBlock", "ScanResult", "scanner",
    "FileHandler", "ProcessedFile", "file_handler",
]
