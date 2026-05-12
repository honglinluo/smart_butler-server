"""
【模块说明】内容获取器（ContentFetcher）— 自动从用户消息中识别并读取文件和网页

当用户发来"帮我分析这个文件 /home/user/data.csv"或"帮我总结这个网页 https://..."时，
AI 需要先去把文件内容或网页内容实际读取出来，才能进行分析。
这个模块负责自动识别并获取这些内容。

【三个主要功能】
  fetch_context_for_input()  — 扫描用户输入，找出文件路径/URL，获取内容注入上下文
  detect_output_request()    — 识别用户是否要求"把结果保存为文件"
  write_output_file()        — 把 AI 生成的内容写入指定文件

【安全限制】
  - 单个文件最大读取 512 KB
  - 网页抓取有超时限制，防止等待太久

Content Fetcher — 主动探测用户输入中的文件路径和网址并获取内容

入口函数:
  fetch_context_for_input(text)  → (context_str, input_file_paths)
  detect_output_request(text, input_paths) → (needed: bool, output_path: Path | None)
  write_output_file(path, content)         → result_str
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 限制 ──────────────────────────────────────────────────────────────────────
_MAX_FILE_BYTES   = 512 * 1024   # 单文件读取上限 512 KB
_MAX_TOTAL_CHARS  = 40_000       # 总注入字符上限
_MAX_DIR_LIST     = 200          # 目录最多列出的文件数
_MAX_DIR_READ     = 15           # 目录自动读取的文本文件数
_URL_FETCH_CHARS  = 8_000        # URL 内容保留字符数

# 识别为文本文件的后缀集合
_TEXT_SUFFIXES = {
    '.md', '.txt', '.py', '.js', '.ts', '.jsx', '.tsx',
    '.json', '.yaml', '.yml', '.toml', '.cfg', '.ini', '.env',
    '.csv', '.log', '.rst', '.html', '.sh', '.bash', '.zsh',
    '.sql', '.xml', '.css', '.go', '.java', '.c', '.cpp', '.h',
    '.rs', '.rb', '.php', '.swift', '.kt', '.r', '.lua', '.pl',
}

# 输出文件请求关键词
_OUTPUT_PATTERNS = [
    r'输出[一个]*.*?(?:新的\s*)?(?:md|txt|文件|文档)',
    r'生成[一个]*.*?(?:新的\s*)?(?:文件|文档)',
    r'保存.*?(?:为|到|成).*?(?:文件|文档)',
    r'写(?:入|出|到).*?(?:文件|文档)',
    r'创建[一个]*.*?(?:新的\s*)?(?:文件|文档)',
    r'新建.*?(?:文件|文档)',
]

# 明确指定输出路径的模式
_EXPLICIT_OUTPUT_RE = re.compile(
    r'(?:输出(?:到)?|保存(?:到|为)|写(?:入|到)|生成到|创建)\s*[：:]*\s*'
    r'(/[^\s，。！？\n"\'<>]+\.(?:md|txt|pdf|html|rst|csv|json))',
    re.IGNORECASE,
)

# 已知文件扩展名列表（用于路径检测）
_KNOWN_EXTS = (
    'md', 'txt', 'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'yaml', 'yml',
    'toml', 'cfg', 'ini', 'env', 'csv', 'log', 'html', 'rst', 'sh',
    'bash', 'sql', 'xml', 'css', 'go', 'java', 'c', 'cpp', 'h', 'rs',
    'rb', 'php', 'swift', 'kt', 'pdf', 'gz', 'zip', 'tar',
)
_EXT_RE = re.compile(
    r'\.(?:' + '|'.join(_KNOWN_EXTS) + r')(?=[^a-zA-Z0-9]|$)',
    re.IGNORECASE,
)

# URL 检测
_URL_RE = re.compile(r'https?://[^\s，。！？\n"\'<>（）【】》《]+', re.UNICODE)


# ── 路径检测 ───────────────────────────────────────────────────────────────────

def find_file_paths(text: str) -> List[Path]:
    """从文本中检测所有有效的本地文件路径（支持路径含空格、中文、emoji）。"""
    results: List[Path] = []
    seen: set = set()

    # 所有以 / 或 ~/ 起始的位置
    for m in re.finditer(r'(?<![a-zA-Z0-9_])(~?/)', text):
        start = m.start()
        remaining = text[start:]

        # 在剩余文本中找已知扩展名
        for ext_m in _EXT_RE.finditer(remaining):
            raw = remaining[:ext_m.end()]
            # 去掉尾部标点
            raw = raw.rstrip('.,，。！？；:：）】"\'》')
            # 展开 ~/
            if raw.startswith('~/'):
                raw = os.path.expanduser(raw)
            norm = str(Path(raw))
            if norm in seen:
                break
            p = Path(raw)
            if p.is_file():
                results.append(p)
                seen.add(norm)
            break  # 每个起始位置只取第一个匹配的扩展名

    return results


def find_dir_paths(text: str, exclude_files: Optional[List[Path]] = None) -> List[Path]:
    """从文本中检测有效的本地目录路径。"""
    exclude_norms = {str(p) for p in (exclude_files or [])}
    results: List[Path] = []
    seen: set = set()

    for m in re.finditer(r'(?<![a-zA-Z0-9_])(~?/[^\s，。！？\n"\'<>（）【】]+)', text):
        raw = m.group(1).rstrip('.,，。！？；:：）】"\'/').rstrip()
        if raw.startswith('~/'):
            raw = os.path.expanduser(raw)
        norm = str(Path(raw))
        if norm in seen or norm in exclude_norms:
            continue
        p = Path(raw)
        if p.is_dir():
            results.append(p)
            seen.add(norm)

    return results


def find_urls(text: str) -> List[str]:
    """从文本中提取 URL 列表。"""
    urls = []
    seen: set = set()
    for url in _URL_RE.findall(text):
        url = url.rstrip('.,，。！？；:：）】"\'>』')
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


# ── 内容读取 ───────────────────────────────────────────────────────────────────

def read_file(path: Path) -> str:
    """读取文本文件内容（含大小限制）。"""
    try:
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            return f"[文件过大 {size // 1024} KB，超过 512 KB 上限，已跳过]"
        return path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        logger.warning("read_file 失败 path=%s: %s", path, e)
        return f"[读取失败: {e}]"


def list_dir(path: Path) -> Tuple[str, str]:
    """遍历目录：返回 (文件列表字符串, 自动读取的文本内容字符串)。"""
    all_files: List[Path] = []
    try:
        for entry in sorted(path.rglob('*')):
            if entry.is_file():
                all_files.append(entry)
    except Exception as e:
        return f"[目录遍历失败: {e}]", ""

    # 文件列表
    lines = [str(f.relative_to(path)) for f in all_files[:_MAX_DIR_LIST]]
    if len(all_files) > _MAX_DIR_LIST:
        lines.append(f"... (共 {len(all_files)} 个文件，仅展示前 {_MAX_DIR_LIST} 个)")
    listing = "\n".join(lines)

    # 自动读取小文本文件
    read_parts: List[str] = []
    for f in all_files:
        if f.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            size = f.stat().st_size
            if size > _MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding='utf-8', errors='replace')
            read_parts.append(f"--- {f.relative_to(path)} ---\n{text}")
        except Exception:
            continue
        if len(read_parts) >= _MAX_DIR_READ:
            read_parts.append(f"[已达自动读取上限 {_MAX_DIR_READ} 个文件]")
            break

    return listing, "\n\n".join(read_parts)


async def fetch_url(url: str) -> str:
    """在沙箱 HTTP 客户端中获取网页内容并提取纯文本。"""
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HermesBot/1.0)"},
        ) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return f"[HTTP {r.status_code}，获取失败]"
            ct = r.headers.get('content-type', '').lower()
            if 'html' in ct or not ct:
                return _html_to_text(r.text)
            elif 'json' in ct:
                return r.text[:_URL_FETCH_CHARS]
            elif 'text' in ct:
                return r.text[:_URL_FETCH_CHARS]
            else:
                return f"[不支持的内容类型: {ct}]"
    except Exception as e:
        logger.warning("fetch_url 失败 url=%s: %s", url, e)
        return f"[网页获取失败: {e}]"


def _html_to_text(html: str) -> str:
    """将 HTML 转换为纯文本（去除脚本、样式、标签）。"""
    html = re.sub(
        r'<(?:script|style)[^>]*>.*?</(?:script|style)>',
        '', html, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text[:_URL_FETCH_CHARS]


# ── 主入口 ─────────────────────────────────────────────────────────────────────

async def fetch_context_for_input(text: str) -> Tuple[str, List[Path]]:
    """
    从用户输入中检测并获取所有相关内容。

    Returns:
        (context_str, input_file_paths)
        context_str       : 注入到 LLM 提示中的内容块（空字符串表示无内容）
        input_file_paths  : 检测到的文件路径列表（用于推断输出路径）
    """
    parts: List[str] = []
    total_chars = 0

    # ── 检测并获取 URL 内容 ────────────────────────────────────────
    for url in find_urls(text):
        if total_chars >= _MAX_TOTAL_CHARS:
            break
        logger.info("content_fetcher: 获取 URL %s", url)
        content = await fetch_url(url)
        if content:
            section = f"【网页内容: {url}】\n{content}"
            parts.append(section)
            total_chars += len(section)

    # ── 检测并读取文件 ─────────────────────────────────────────────
    file_paths = find_file_paths(text)
    for path in file_paths:
        if total_chars >= _MAX_TOTAL_CHARS:
            break
        logger.info("content_fetcher: 读取文件 %s", path)
        content = read_file(path)
        section = f"【文件内容: {path}】\n{content}"
        parts.append(section)
        total_chars += len(section)

    # ── 检测并列举目录 ─────────────────────────────────────────────
    dir_paths = find_dir_paths(text, exclude_files=file_paths)
    for path in dir_paths:
        if total_chars >= _MAX_TOTAL_CHARS:
            break
        logger.info("content_fetcher: 遍历目录 %s", path)
        listing, file_contents = list_dir(path)
        section = f"【目录结构: {path}】\n{listing}"
        parts.append(section)
        total_chars += len(section)
        if file_contents and total_chars < _MAX_TOTAL_CHARS:
            fc_section = f"【目录中的文件内容: {path}】\n{file_contents}"
            parts.append(fc_section)
            total_chars += len(fc_section)

    context_str = "\n\n".join(parts)
    if context_str and total_chars >= _MAX_TOTAL_CHARS:
        context_str += f"\n\n[注意] 内容已达 {_MAX_TOTAL_CHARS // 1000}K 字符上限，部分内容已截断。"

    return context_str, file_paths


# ── 输出文件处理 ───────────────────────────────────────────────────────────────

def detect_output_request(
    text: str,
    input_paths: Optional[List[Path]] = None,
) -> Tuple[bool, Optional[Path]]:
    """
    检测任务描述中是否需要将结果写入文件，并推断输出路径。

    Returns:
        (needed, output_path)
        needed      : True 表示需要输出文件
        output_path : 推断的输出路径（None 表示未检测到需求）
    """
    needed = any(re.search(p, text) for p in _OUTPUT_PATTERNS)
    if not needed:
        return False, None

    # 优先使用用户明确指定的输出路径
    m = _EXPLICIT_OUTPUT_RE.search(text)
    if m:
        return True, Path(m.group(1))

    # 从输入文件路径派生
    if input_paths:
        inp = input_paths[0]
        if inp.is_file():
            # 将文件名中的特殊字符替换为下划线（仅保留中文、英文、数字）
            safe_stem = re.sub(r'[^\w一-鿿\-]', '_', inp.stem).strip('_')[:60]
            suffix = inp.suffix if inp.suffix in ('.md', '.txt', '.rst') else '.md'
            output = inp.parent / f"{safe_stem}_摘要{suffix}"
            return True, output

    # 默认：当前目录
    name = f"output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    return True, Path.cwd() / name


def write_output_file(path: Path, content: str) -> str:
    """将内容写入输出文件（自动创建父目录）。返回结果描述字符串。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        logger.info("content_fetcher: 输出文件已写入 %s (%d 字节)", path, len(content))
        return f"[成功] 已将摘要保存至: {path}"
    except Exception as e:
        logger.warning("write_output_file 失败 path=%s: %s", path, e)
        return f"[警告] 文件保存失败: {e}"
