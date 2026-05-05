"""内置工具包 — 导入时自动注册到全局 registry。"""

from app.tools.builtin.file_reader import FileReaderTool
from app.tools.builtin.file_writer import FileWriterTool

__all__ = ["FileReaderTool", "FileWriterTool"]
