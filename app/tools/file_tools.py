"""Static file tools — read and write local files.

Registered in agents_config.yaml and used by general_assistant via LangGraph ReAct.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB safety cap


def tool_file_reader(file_path: str) -> str:
    """读取本地文件内容（支持 .md .txt .py .json .yaml 等文本文件）。

    Args:
        file_path: 文件的绝对路径。

    Returns:
        文件的完整文本内容；出错时返回以 [错误] 开头的说明字符串。
    """
    try:
        p = Path(file_path)
        if not p.exists():
            return f"[错误] 文件不存在: {file_path}"
        if not p.is_file():
            return f"[错误] 路径不是文件: {file_path}"
        size = p.stat().st_size
        if size > _MAX_READ_BYTES:
            return f"[错误] 文件过大 ({size // 1024} KB > 2 MB 上限): {file_path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        logger.info("file_reader: 成功读取 %s (%d 字节)", file_path, len(content))
        return content
    except Exception as e:
        logger.warning("file_reader 失败 path=%s: %s", file_path, e)
        return f"[错误] 读取文件失败: {e}"


def tool_file_writer(file_path: str, content: str) -> str:
    """将内容写入本地文件（自动创建父目录）。

    Args:
        file_path: 目标文件的绝对路径。
        content:   要写入的文本内容。

    Returns:
        成功提示字符串；出错时返回以 [错误] 开头的说明字符串。
    """
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info("file_writer: 成功写入 %s (%d 字节)", file_path, len(content))
        return f"[成功] 已将 {len(content)} 字节写入 {file_path}"
    except Exception as e:
        logger.warning("file_writer 失败 path=%s: %s", file_path, e)
        return f"[错误] 写入文件失败: {e}"
