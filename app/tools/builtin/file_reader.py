"""文件读取工具 — 读取任意格式文件，输出模型输入通用内容格式（Content Parts）。

支持格式：
  文本类   : .txt .md .rst .html .xml .log .csv .tsv
  结构化数据: .json .yaml .yml .toml .ini .env
  Office   : .docx .xlsx .xls .pptx .ppt
  PDF      : .pdf（提取文本 + 可选原始 base64 供 Claude 原生解析）
  图片     : .png .jpg .jpeg .gif .webp .bmp .svg
  数据帧   : .csv .tsv .parquet .feather .jsonl

输出格式（Content Parts）为多模态模型消息 content 字段的直接可插入块：

  文本块：   {"type": "text",     "text": "..."}
  图片块：   {"type": "image",    "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
  文档块：   {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "..."}}

所有格式最终都会生成至少一个文本块；图片和 PDF 在原始块之外还额外附加文本块（元信息 / 提取文本），
以保证在不支持多模态的模型下也可降级使用。
"""

from __future__ import annotations

import base64
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.paths import PROJECT_ROOT
from app.tools.base import BaseTool, EXEC_CLIENT, EXEC_SERVER, VIS_PUBLIC

logger = logging.getLogger(__name__)

# 用户上传数据根目录
DATA_ROOT = PROJECT_ROOT / "data"

# 单次读取文本最大字符数（避免超长文件撑爆上下文）
MAX_TEXT_CHARS = 100_000

# 表格类文件默认最大行数
DEFAULT_MAX_ROWS = 200

# ── 敏感信息遮盖正则 ──────────────────────────────────────────────────────────
# 匹配：敏感关键词 + 分隔符（=、:、：）+ 由字母/数字/常见符号组成的值（6~256 位）
# 捕获组 1 = 关键词+分隔符（保留），捕获组 2 = 值（替换为 ***）
_SENSITIVE_RE = re.compile(
    r'(?i)'
    r'('
    # ── 英文关键词（需 \b 词边界，分隔符不跨行）──────────────────
    r'\b(?:'
        r'api[-_ ]?(?:key|secret|token)'          # api_key / api-key / api key / api_token
        r'|access[-_ ]?(?:key|token|secret)'      # access_key / access token
        r'|auth(?:[-_]token|orization|[-_]key)'   # auth_token / authorization / auth_key（bare auth 太泛）
        r'|private[-_ ]?key'                      # private_key / private key
        r'|secret(?:[-_]?key)?'                   # secret / secret_key
        r'|bearer'                                # bearer
        r'|passw(?:or)?d|passwd'                  # password / passwd
        r'|pass(?:phrase|word)?'                  # pass / passphrase / password
        r'|session(?:[-_](?:id|key|token))?'      # session / session_id / session_key / session_token
        r'|token'                                 # token
        r'|credential(?:s)?'                      # credential / credentials
    r')\b'
    r'["\']?'                                     # JSON "key": 闭合引号（可选）
    r'[^\S\n]*[=:：][^\S\n]*'                     # 分隔符（仅同行水平空白，不跨行）
    r'["\']?'                                     # 值的开头引号（可选）
    # ── 中文关键词（无 \b，因汉字均为 \w，词边界检测无效）───────
    r'|(?:密码|口令|令牌|密钥|会话(?:密钥)?)'
    r'[^\S\n]*[=:：][^\S\n]*'
    r')'
    r'([A-Za-z0-9+/=\-_.!@#$%^&*]{6,256}'          # 值：字母/数字/符号，6~256 位
    r'(?:[ \t]+[A-Za-z0-9+/=\-_.!@#$%^&*]{10,256})?)',  # 可选第二段（Bearer <token> 场景）
    re.IGNORECASE | re.UNICODE,
)


# ══════════════════════════════════════════════════════════════════
# 软导入辅助 — 缺少库时给出可读提示
# ══════════════════════════════════════════════════════════════════

def _try_import(module: str, package: Optional[str] = None):
    """尝试导入，失败返回 None。"""
    try:
        import importlib
        return importlib.import_module(module)
    except ImportError:
        pkg = package or module
        logger.debug("可选依赖 %s 未安装（pip install %s）", module, pkg)
        return None


# ══════════════════════════════════════════════════════════════════
# Content Part 构造辅助
# ══════════════════════════════════════════════════════════════════

def _text_part(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(data: bytes, media_type: str) -> Dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type":       "base64",
            "media_type": media_type,
            "data":       base64.standard_b64encode(data).decode(),
        },
    }


def _document_part(data: bytes, media_type: str = "application/pdf") -> Dict[str, Any]:
    return {
        "type": "document",
        "source": {
            "type":       "base64",
            "media_type": media_type,
            "data":       base64.standard_b64encode(data).decode(),
        },
    }


def _truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[...内容已截断，原始长度 {len(text)} 字符]"


def _mask_secrets(text: str) -> Tuple[str, int]:
    """将文本中所有匹配 _SENSITIVE_RE / _SENSITIVE_RE_ENV 的密文值替换为 ***。

    返回 (遮盖后的文本, 遮盖次数)。
    """
    count = [0]

    def _replace(m: re.Match) -> str:
        count[0] += 1
        return m.group(1) + "***"

    masked = _SENSITIVE_RE.sub(_replace, text)

    # 补充：覆盖 DJANGO_SECRET_KEY / DB_PASSWORD 等 _KEYWORD 前缀模式
    def _replace_env(m: re.Match) -> str:
        count[0] += 1
        val_start = m.start(1) - m.start(0)
        return m.group(0)[:val_start] + "***"

    masked = _SENSITIVE_RE_ENV.sub(_replace_env, masked)
    return masked, count[0]


def _mask_text_parts(parts: List[Dict]) -> Tuple[List[Dict], int]:
    """对所有文本块执行敏感信息遮盖；图片/文档块保持不变。

    返回 (处理后的 parts 列表, 累计遮盖次数)。
    """
    result  = []
    total   = 0
    for part in parts:
        if part.get("type") == "text":
            masked_text, n = _mask_secrets(part["text"])
            total += n
            result.append({"type": "text", "text": masked_text})
        else:
            result.append(part)
    return result, total


# ══════════════════════════════════════════════════════════════════
# 编码检测
# ══════════════════════════════════════════════════════════════════

def _detect_encoding(raw: bytes) -> str:
    chardet = _try_import("chardet")
    if chardet:
        detected = chardet.detect(raw[:8192])
        enc = detected.get("encoding") or "utf-8"
        return enc if enc else "utf-8"
    # 简单启发式
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb2312", "latin-1"):
        try:
            raw[:4096].decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _decode(raw: bytes, hint: str = "") -> str:
    enc = hint or _detect_encoding(raw)
    try:
        return raw.decode(enc, errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


# ══════════════════════════════════════════════════════════════════
# 格式分类映射
# ══════════════════════════════════════════════════════════════════

_EXT_CATEGORY = {
    # 纯文本
    ".txt": "text", ".md": "text", ".rst": "text", ".log": "text",
    ".html": "text", ".htm": "text", ".xml": "text", ".svg": "text",
    # 结构化文本
    ".json": "json", ".jsonl": "jsonl",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini", ".env": "ini", ".cfg": "ini", ".conf": "ini",
    # 表格
    ".csv": "table", ".tsv": "table",
    # 数据帧（需要 pandas）
    ".parquet": "dataframe", ".feather": "dataframe", ".orc": "dataframe",
    # Office
    ".docx": "word", ".doc": "word",
    ".xlsx": "excel", ".xls": "excel",
    ".pptx": "ppt", ".ppt": "ppt",
    # PDF
    ".pdf": "pdf",
    # 图片
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".bmp": "image",
    ".tiff": "image", ".tif": "image",
    # 代码（当纯文本处理）
    ".py": "code", ".js": "code", ".ts": "code", ".go": "code",
    ".rs": "code", ".java": "code", ".cpp": "code", ".c": "code",
    ".sh": "code", ".bash": "code", ".sql": "code", ".r": "code",
    ".rb": "code", ".php": "code", ".kt": "code", ".swift": "code",
    ".css": "code", ".scss": "code", ".less": "code",
}

_IMAGE_MEDIA_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff",
    ".svg": "image/svg+xml",
}


# ══════════════════════════════════════════════════════════════════
# 各格式读取器
# ══════════════════════════════════════════════════════════════════

# ── 纯文本 / 代码 ─────────────────────────────────────────────────

def _read_text(raw: bytes, ext: str, filename: str) -> Tuple[List[Dict], Dict]:
    enc  = _detect_encoding(raw)
    text = _decode(raw, enc)
    lang_hint = ext.lstrip(".")
    header = f"# 文件：{filename}  ({len(raw)} bytes, 编码: {enc})\n\n"
    if ext in (".py", ".js", ".ts", ".go", ".rs", ".java", ".cpp", ".c",
               ".sh", ".bash", ".sql", ".rb", ".php"):
        body = f"```{lang_hint}\n{_truncate(text)}\n```"
    else:
        body = _truncate(text)
    return (
        [_text_part(header + body)],
        {"encoding": enc, "chars": len(text), "lines": text.count("\n") + 1},
    )


# ── JSON ──────────────────────────────────────────────────────────

def _read_json(raw: bytes, filename: str) -> Tuple[List[Dict], Dict]:
    enc  = _detect_encoding(raw)
    text = _decode(raw, enc)
    meta: Dict[str, Any] = {}
    try:
        obj = json.loads(text)
        pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        if isinstance(obj, list):
            meta = {"type": "array", "length": len(obj)}
        elif isinstance(obj, dict):
            meta = {"type": "object", "keys": list(obj.keys())[:20]}
        body = f"```json\n{_truncate(pretty)}\n```"
    except json.JSONDecodeError as e:
        body = f"[JSON 解析失败: {e}]\n\n原始内容：\n{_truncate(text)}"
    header = f"# 文件：{filename}  (JSON)\n\n"
    return [_text_part(header + body)], meta


# ── JSONL ─────────────────────────────────────────────────────────

def _read_jsonl(raw: bytes, filename: str, max_rows: int) -> Tuple[List[Dict], Dict]:
    enc   = _detect_encoding(raw)
    text  = _decode(raw, enc)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    total = len(lines)
    shown = lines[:max_rows]
    objects = []
    errors  = 0
    for line in shown:
        try:
            objects.append(json.loads(line))
        except Exception:
            errors += 1
    sample = json.dumps(objects, ensure_ascii=False, indent=2)
    body = (
        f"# 文件：{filename}  (JSONL, 共 {total} 行)\n\n"
        f"```json\n{_truncate(sample)}\n```"
    )
    if total > max_rows:
        body += f"\n\n[已截取前 {max_rows} 行，共 {total} 行]"
    return [_text_part(body)], {"total_lines": total, "shown": len(shown), "parse_errors": errors}


# ── YAML ──────────────────────────────────────────────────────────

def _read_yaml(raw: bytes, filename: str) -> Tuple[List[Dict], Dict]:
    enc  = _detect_encoding(raw)
    text = _decode(raw, enc)
    yaml = _try_import("yaml")
    meta: Dict = {}
    if yaml:
        try:
            obj = yaml.safe_load(text)
            pretty = yaml.dump(obj, allow_unicode=True, default_flow_style=False)
            if isinstance(obj, dict):
                meta = {"type": "object", "keys": list(obj.keys())[:20]}
            body = f"```yaml\n{_truncate(pretty)}\n```"
        except Exception as e:
            body = f"[YAML 解析失败: {e}]\n\n原始内容：\n{_truncate(text)}"
    else:
        body = f"```yaml\n{_truncate(text)}\n```"
    return [_text_part(f"# 文件：{filename}  (YAML)\n\n" + body)], meta


# ── TOML ──────────────────────────────────────────────────────────

def _read_toml(raw: bytes, filename: str) -> Tuple[List[Dict], Dict]:
    enc  = _detect_encoding(raw)
    text = _decode(raw, enc)
    try:
        if sys.version_info >= (3, 11):
            import tomllib
            obj = tomllib.loads(text)
        else:
            import tomli  # type: ignore
            obj = tomli.loads(text)
        pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        body   = f"```toml\n{_truncate(text)}\n```\n\n解析结果（JSON）：\n```json\n{_truncate(pretty)}\n```"
        meta   = {"keys": list(obj.keys())[:20]}
    except Exception as e:
        body = f"[TOML 解析失败: {e}]\n\n原始内容：\n{_truncate(text)}"
        meta = {}
    return [_text_part(f"# 文件：{filename}  (TOML)\n\n" + body)], meta


# ── INI / ENV ─────────────────────────────────────────────────────

def _read_ini(raw: bytes, filename: str) -> Tuple[List[Dict], Dict]:
    import configparser
    enc  = _detect_encoding(raw)
    text = _decode(raw, enc)
    # .env 文件：隐藏值（可能含密钥）
    ext = Path(filename).suffix.lower()
    if ext in (".env",):
        lines   = text.splitlines()
        masked  = []
        for line in lines:
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                if any(s in k.upper() for s in ("KEY", "SECRET", "TOKEN", "PASS", "PWD")):
                    masked.append(f"{k}=***")
                else:
                    masked.append(line)
            else:
                masked.append(line)
        body = "```ini\n" + _truncate("\n".join(masked)) + "\n```"
        return [_text_part(f"# 文件：{filename}  (.env — 敏感字段已遮盖)\n\n" + body)], {}
    # 普通 INI
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text)
        sections = {s: dict(cfg[s]) for s in cfg.sections()}
        pretty   = json.dumps(sections, ensure_ascii=False, indent=2)
        body     = f"```ini\n{_truncate(text)}\n```\n\n解析结果：\n```json\n{_truncate(pretty)}\n```"
        meta     = {"sections": cfg.sections()}
    except Exception:
        body = f"```\n{_truncate(text)}\n```"
        meta = {}
    return [_text_part(f"# 文件：{filename}  (INI)\n\n" + body)], meta


# ── CSV / TSV ─────────────────────────────────────────────────────

# 敏感列名关键词（用于 Excel 逐格遮盖）
_SENSITIVE_HEADER_RE = re.compile(
    r'(?i)(?:token|session|passw(?:or)?d|passwd|pass(?:phrase)?|secret|'
    r'api[-_ ]?(?:key|secret|token)|access[-_ ]?(?:key|token|secret)|'
    r'credential|auth|bearer|密码|口令|令牌|密钥)',
)

# 用于 .env / 大写 KEY 风格遮盖的补充正则（前置非字母数字）
_SENSITIVE_RE_ENV = re.compile(
    r'(?<![a-zA-Z0-9])'
    r'(?:password|passwd|pass(?:phrase|word)?|secret(?:[-_]?key)?|'
    r'api[-_]?(?:key|secret|token)|access[-_]?(?:key|token|secret)|'
    r'token|session(?:[-_](?:id|key|token))?|auth(?:[-_]token|orization|[-_]key)|'
    r'credential(?:s)?|private[-_]?key|bearer|'
    r'密码|口令|令牌|密钥|会话(?:密钥)?)'
    r'(?![a-zA-Z0-9])'
    r'["\']?[^\S\n]*[=:：][^\S\n]*["\']?'
    r'([A-Za-z0-9+/=\-_.!@#$%^&*]{6,256})',
    re.IGNORECASE | re.UNICODE,
)


def _mask_excel_rows(
    headers: List[str],
    rows: List[List[str]],
) -> List[List[str]]:
    """对 Excel 数据行中属于敏感列的单元格值进行遮盖。

    判断方式：列名包含 token/session/password/secret 等关键词，则该列所有值遮盖为 ***。
    同时对每个单元格也用正则扫描一遍，覆盖列名判断漏掉的情况。
    """
    sensitive_cols = {
        i for i, h in enumerate(headers) if _SENSITIVE_HEADER_RE.search(h)
    }
    result = []
    for row in rows:
        new_row = []
        for i, cell in enumerate(row):
            if i in sensitive_cols and len(cell) >= 6:
                new_row.append("***")
            else:
                # 对格内文本也做正则扫描（如格内含 key=value 对）
                masked, _ = _mask_secrets(cell)
                new_row.append(masked)
        result.append(new_row)
    return result


def _table_to_markdown(rows: List[List[str]], headers: Optional[List[str]] = None) -> str:
    if not rows:
        return "(空表格)"
    cols    = headers or rows[0]
    data    = rows[1:] if not headers else rows
    widths  = [max(len(str(c)), max((len(str(r[i])) if i < len(r) else 0) for r in data[:50]), 1)
               for i, c in enumerate(cols)]
    sep     = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    header  = "|" + "|".join(f" {str(c):<{widths[i]}} " for i, c in enumerate(cols)) + "|"
    lines   = [header, sep]
    for row in data:
        lines.append("|" + "|".join(
            f" {str(row[i]) if i < len(row) else '':<{widths[i]}} "
            for i in range(len(cols))
        ) + "|")
    return "\n".join(lines)


def _read_table(raw: bytes, ext: str, filename: str, max_rows: int) -> Tuple[List[Dict], Dict]:
    enc  = _detect_encoding(raw)
    text = _decode(raw, enc)
    delimiter = "\t" if ext == ".tsv" else ","
    try:
        reader  = csv.reader(StringIO(text), delimiter=delimiter)
        all_rows = list(reader)
        total    = len(all_rows) - 1  # 不含表头
        headers  = all_rows[0] if all_rows else []
        data     = all_rows[1: max_rows + 1]
        table_md = _table_to_markdown(data, headers=headers)
        body = (
            f"# 文件：{filename}  ({ext.upper()}, 共 {total} 行 × {len(headers)} 列)\n\n"
            f"{table_md}"
        )
        if total > max_rows:
            body += f"\n\n[已显示前 {max_rows} 行，共 {total} 行]"
        meta = {"rows": total, "columns": len(headers), "column_names": headers}
    except Exception as e:
        body = f"[CSV/TSV 解析失败: {e}]\n\n原始内容：\n{_truncate(text)}"
        meta = {}
    return [_text_part(body)], meta


# ── DataFrame（Parquet / Feather）────────────────────────────────

def _read_dataframe(path: Path, ext: str, filename: str, max_rows: int) -> Tuple[List[Dict], Dict]:
    pd = _try_import("pandas")
    if not pd:
        return (
            [_text_part(f"# 文件：{filename}\n\n读取 {ext} 需要安装 pandas。")],
            {"error": "pandas not installed"},
        )
    try:
        if ext == ".parquet":
            df = pd.read_parquet(path)
        elif ext == ".feather":
            df = pd.read_feather(path)
        else:
            df = pd.read_orc(path)

        meta = {
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "column_names": list(df.columns),
            "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        }
        sample_md = df.head(max_rows).to_markdown(index=True)
        body = (
            f"# 文件：{filename}  ({ext.upper()}, {df.shape[0]} 行 × {df.shape[1]} 列)\n\n"
            f"**列信息：** {', '.join(str(c) for c in df.columns)}\n\n"
            f"**统计摘要：**\n```\n{df.describe().to_string()}\n```\n\n"
            f"**前 {min(max_rows, len(df))} 行：**\n\n{sample_md}"
        )
        return [_text_part(_truncate(body))], meta
    except Exception as e:
        return [_text_part(f"# 文件：{filename}\n\n[读取失败: {e}]")], {"error": str(e)}


# ── Excel ─────────────────────────────────────────────────────────

def _read_excel(path: Path, filename: str, max_rows: int, sheet_name: Optional[str]) -> Tuple[List[Dict], Dict]:
    openpyxl = _try_import("openpyxl")
    if not openpyxl:
        # 尝试 pandas（若已安装 openpyxl 作为引擎）
        pd = _try_import("pandas")
        if pd:
            try:
                xf = pd.ExcelFile(path)
                sheets = xf.sheet_names
                parts: List[Dict] = []
                for sn in ([sheet_name] if sheet_name else sheets):
                    df = pd.read_excel(path, sheet_name=sn, nrows=max_rows)
                    md = df.to_markdown(index=False)
                    parts.append(_text_part(
                        f"### 工作表：{sn}  ({df.shape[0]} 行 × {df.shape[1]} 列)\n\n{md}"
                    ))
                meta_x = {"sheets": sheets, "shown_rows": max_rows}
                header = f"# 文件：{filename}  (Excel, {len(sheets)} 个工作表)\n\n"
                parts.insert(0, _text_part(header))
                return parts, meta_x
            except Exception as e:
                return [_text_part(f"[Excel 读取失败: {e}]")], {"error": str(e)}
        return (
            [_text_part(f"# 文件：{filename}\n\n读取 Excel 需要安装 openpyxl。")],
            {"error": "openpyxl not installed"},
        )
    try:
        wb     = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheets = [sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.sheetnames
        meta_e = {"sheets": wb.sheetnames}
        parts  = [_text_part(f"# 文件：{filename}  (Excel, {len(wb.sheetnames)} 个工作表)\n\n")]

        for sn in sheets:
            ws    = wb[sn]
            rows  = list(ws.iter_rows(values_only=True))
            total = len(rows) - 1
            if not rows:
                parts.append(_text_part(f"### 工作表：{sn}  (空)\n"))
                continue
            str_rows = [[str(c) if c is not None else "" for c in r] for r in rows]
            headers  = str_rows[0]
            data_raw = str_rows[1: max_rows + 1]
            # 根据列名对敏感列的每个单元格值做遮盖
            data     = _mask_excel_rows(headers, data_raw)
            table_md = _table_to_markdown(data, headers=headers)
            note     = f"\n\n[已显示前 {max_rows} 行，共 {total} 行]" if total > max_rows else ""
            parts.append(_text_part(
                f"### 工作表：{sn}  ({total} 行 × {len(headers)} 列)\n\n{table_md}{note}\n"
            ))
        return parts, meta_e
    except Exception as e:
        return [_text_part(f"[Excel 读取失败: {e}]")], {"error": str(e)}


# ── Word (.docx) ──────────────────────────────────────────────────

def _read_word(path: Path, filename: str) -> Tuple[List[Dict], Dict]:
    docx_mod = _try_import("docx", package="python-docx")
    if not docx_mod:
        return (
            [_text_part(f"# 文件：{filename}\n\n读取 Word 文档需要安装 python-docx。")],
            {"error": "python-docx not installed"},
        )
    try:
        doc     = docx_mod.Document(path)
        lines   = []
        h_count = 0
        p_count = 0
        for para in doc.paragraphs:
            style = para.style.name if para.style else ""
            text  = para.text.strip()
            if not text:
                continue
            if style.startswith("Heading"):
                level = re.search(r"\d", style)
                lvl   = int(level.group()) if level else 1
                lines.append(f"\n{'#' * lvl} {text}\n")
                h_count += 1
            else:
                lines.append(text)
                p_count += 1

        # 表格
        tables_md = []
        for i, table in enumerate(doc.tables):
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            tables_md.append(f"\n#### 表格 {i + 1}\n\n{_table_to_markdown(rows[1:], headers=rows[0])}\n")

        body = (
            f"# 文件：{filename}  (Word 文档)\n\n"
            + "\n".join(lines)
            + ("\n\n---\n" + "\n".join(tables_md) if tables_md else "")
        )
        meta = {"paragraphs": p_count, "headings": h_count, "tables": len(doc.tables)}
        return [_text_part(_truncate(body))], meta
    except Exception as e:
        return [_text_part(f"[Word 读取失败: {e}]")], {"error": str(e)}


# ── PowerPoint (.pptx) ────────────────────────────────────────────

def _read_ppt(path: Path, filename: str) -> Tuple[List[Dict], Dict]:
    pptx_mod = _try_import("pptx", package="python-pptx")
    if not pptx_mod:
        return (
            [_text_part(f"# 文件：{filename}\n\n读取 PowerPoint 需要安装 python-pptx。")],
            {"error": "python-pptx not installed"},
        )
    try:
        prs    = pptx_mod.Presentation(path)
        slides_text: List[str] = []
        for idx, slide in enumerate(prs.slides, 1):
            title = ""
            texts = []
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                for para in shape.text_frame.paragraphs:
                    t = para.text.strip()
                    if not t:
                        continue
                    if shape.shape_type == 13:  # 标题占位符
                        title = t
                    else:
                        texts.append(t)
            slide_md = f"### 第 {idx} 张幻灯片" + (f"：{title}" if title else "") + "\n\n"
            if texts:
                slide_md += "\n".join(f"- {t}" for t in texts)
            slides_text.append(slide_md)

        body = (
            f"# 文件：{filename}  (PowerPoint, {len(prs.slides)} 张幻灯片)\n\n"
            + "\n\n".join(slides_text)
        )
        meta = {"slides": len(prs.slides)}
        return [_text_part(_truncate(body))], meta
    except Exception as e:
        return [_text_part(f"[PowerPoint 读取失败: {e}]")], {"error": str(e)}


# ── PDF ───────────────────────────────────────────────────────────

def _read_pdf(
    path: Path,
    raw: bytes,
    filename: str,
    native_doc: bool,
) -> Tuple[List[Dict], Dict]:
    pypdf = _try_import("pypdf")
    parts: List[Dict] = []

    # 原始 base64 文档块（供 Claude 原生解析，优先级最高）
    if native_doc:
        parts.append(_document_part(raw, "application/pdf"))

    # 文字提取（降级文本块）
    if pypdf:
        try:
            from io import BytesIO
            reader    = pypdf.PdfReader(BytesIO(raw))
            num_pages = len(reader.pages)
            page_texts: List[str] = []
            for i, page in enumerate(reader.pages):
                t = page.extract_text() or ""
                if t.strip():
                    page_texts.append(f"### 第 {i + 1} 页\n\n{t.strip()}")

            extracted = "\n\n".join(page_texts)
            header    = (
                f"# 文件：{filename}  (PDF, {num_pages} 页)\n\n"
                f"*以下为文字提取内容，建议配合原始文档块使用以获得更准确解析。*\n\n"
            )
            parts.append(_text_part(header + _truncate(extracted)))
            meta = {"pages": num_pages, "has_native_doc": native_doc}
        except Exception as e:
            parts.append(_text_part(f"[PDF 文字提取失败: {e}]"))
            meta = {"error": str(e), "has_native_doc": native_doc}
    else:
        msg = "读取 PDF 文字需要安装 pypdf。已返回原始 base64 块。" if native_doc else "需要安装 pypdf。"
        parts.append(_text_part(f"# 文件：{filename}  (PDF)\n\n{msg}"))
        meta = {"error": "pypdf not installed", "has_native_doc": native_doc}

    return parts, meta


# ── 图片 ──────────────────────────────────────────────────────────

def _read_image(
    path: Path,
    raw: bytes,
    ext: str,
    filename: str,
    as_base64: bool,
) -> Tuple[List[Dict], Dict]:
    media_type = _IMAGE_MEDIA_TYPE.get(ext, "image/jpeg")
    meta: Dict[str, Any] = {"media_type": media_type, "size_bytes": len(raw)}

    # 尝试获取图片尺寸
    pil = _try_import("PIL.Image", package="pillow")
    if pil:
        try:
            from PIL import Image
            from io import BytesIO
            img  = Image.open(BytesIO(raw))
            meta["width"]  = img.width
            meta["height"] = img.height
            meta["format"] = img.format
            meta["mode"]   = img.mode
        except Exception:
            pass

    parts: List[Dict] = []
    if as_base64:
        parts.append(_image_part(raw, media_type))

    info = (
        f"# 文件：{filename}  (图片)\n\n"
        f"- 格式：{media_type}\n"
        f"- 大小：{len(raw):,} bytes"
    )
    if "width" in meta:
        info += f"\n- 尺寸：{meta['width']} × {meta['height']} px"
    if not as_base64:
        info += "\n\n[图片未转换为 base64，如需多模态输入请将 image_as_base64 设为 true]"
    parts.append(_text_part(info))
    return parts, meta


# ══════════════════════════════════════════════════════════════════
# 路径解析与安全校验
# ══════════════════════════════════════════════════════════════════

def _resolve_path(path_str: str, user_id: str) -> Path:
    """
    将用户输入的路径解析为绝对路径，并确保在允许范围内。

    允许范围：
      1. DATA_ROOT / user_id / **  （用户上传目录）
      2. DATA_ROOT / **             （管理员视图，路径必须以 data/ 开头）

    解析规则：
      - 绝对路径：直接使用，但必须在 DATA_ROOT 下
      - 以 data/ 开头：相对 PROJECT_ROOT 解析
      - 其他相对路径：相对 DATA_ROOT / user_id 解析
    """
    p = Path(path_str)
    if p.is_absolute():
        resolved = p.resolve()
    elif path_str.startswith("data/") or path_str.startswith("data\\"):
        resolved = (PROJECT_ROOT / p).resolve()
    else:
        resolved = (DATA_ROOT / user_id / p).resolve()

    # 安全边界：必须在 DATA_ROOT 内
    try:
        resolved.relative_to(DATA_ROOT.resolve())
    except ValueError:
        raise PermissionError(
            f"路径 '{path_str}' 超出允许的数据目录范围 ({DATA_ROOT})"
        )
    return resolved


# ══════════════════════════════════════════════════════════════════
# 主工具类
# ══════════════════════════════════════════════════════════════════

class FileReaderTool(BaseTool):
    """
    文件读取工具，将任意格式文件转换为模型输入通用的 Content Parts 格式。

    content_parts 中每个元素对应多模态消息的一个内容块：
      - text    → {"type": "text", "text": "..."}
      - image   → {"type": "image", "source": {"type": "base64", ...}}
      - document→ {"type": "document", "source": {"type": "base64", ...}}  (PDF)

    可直接将返回的 content_parts 插入 messages[].content 数组传给任意模型。
    """

    name          = "file_reader"
    description   = (
        "读取本地文件并将内容转换为模型输入通用格式（Content Parts）。"
        "支持 PDF、Word、Excel、PowerPoint、JSON、YAML、CSV、图片等各类格式。"
        "返回的 content_parts 可直接嵌入多模态对话消息。"
    )
    exec_location = EXEC_CLIENT
    visibility    = VIS_PUBLIC
    dangerous_ops = []

    parameters_schema = {
        "path": {
            "type":        "string",
            "description": (
                "文件路径。相对路径基于当前用户数据目录（data/{user_id}/）解析；"
                "以 'data/' 开头则基于项目根目录解析；绝对路径需在 data/ 目录下。"
            ),
            "required": True,
        },
        "sheet_name": {
            "type":        "string",
            "description": "Excel：指定工作表名称；不填则读取所有工作表。",
            "required":    False,
        },
        "max_rows": {
            "type":        "integer",
            "description": f"表格类文件最大读取行数（默认 {DEFAULT_MAX_ROWS}）。",
            "default":     DEFAULT_MAX_ROWS,
        },
        "encoding": {
            "type":        "string",
            "description": "文本文件强制指定编码（留空自动检测，支持 utf-8/gbk/latin-1 等）。",
            "required":    False,
        },
        "image_as_base64": {
            "type":        "boolean",
            "description": "图片是否返回 base64 内容块（默认 true）；false 时仅返回元信息。",
            "default":     True,
        },
        "pdf_native_doc": {
            "type":        "boolean",
            "description": (
                "PDF 是否同时返回原始 document 块（供 Claude 3.5+ 原生解析，默认 true）。"
                "false 时仅提取文字文本块。"
            ),
            "default":     True,
        },
    }

    async def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        path_str     = params["path"]
        sheet_name   = params.get("sheet_name")
        max_rows     = int(params.get("max_rows") or DEFAULT_MAX_ROWS)
        enc_hint     = params.get("encoding", "")
        as_base64    = bool(params.get("image_as_base64", True))
        native_doc   = bool(params.get("pdf_native_doc", True))
        user_id      = context.get("user_id", "anonymous")

        # ── 路径解析与安全校验 ──────────────────────────────────────
        try:
            file_path = _resolve_path(path_str, user_id)
        except PermissionError as e:
            return {"result": str(e), "success": False, "metadata": {}}

        if not file_path.exists():
            return {
                "result":  f"文件不存在：{path_str}",
                "success": False,
                "metadata": {"resolved_path": str(file_path)},
            }
        if not file_path.is_file():
            return {"result": f"路径不是文件：{path_str}", "success": False, "metadata": {}}

        # ── 基础元信息 ─────────────────────────────────────────────
        stat    = file_path.stat()
        ext     = file_path.suffix.lower()
        fname   = file_path.name
        category= _EXT_CATEGORY.get(ext, "text")
        base_meta: Dict[str, Any] = {
            "filename":     fname,
            "file_type":    category,
            "extension":    ext,
            "size_bytes":   stat.st_size,
            "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "resolved_path": str(file_path.relative_to(PROJECT_ROOT)),
        }

        # ── 读取原始字节 ───────────────────────────────────────────
        try:
            raw = file_path.read_bytes()
        except Exception as e:
            return {"result": f"文件读取失败：{e}", "success": False, "metadata": base_meta}

        # ── 按分类分派 ─────────────────────────────────────────────
        content_parts: List[Dict] = []
        fmt_meta: Dict            = {}

        try:
            if category == "json":
                content_parts, fmt_meta = _read_json(raw, fname)

            elif category == "jsonl":
                content_parts, fmt_meta = _read_jsonl(raw, fname, max_rows)

            elif category == "yaml":
                content_parts, fmt_meta = _read_yaml(raw, fname)

            elif category == "toml":
                content_parts, fmt_meta = _read_toml(raw, fname)

            elif category == "ini":
                content_parts, fmt_meta = _read_ini(raw, fname)

            elif category == "table":
                content_parts, fmt_meta = _read_table(raw, ext, fname, max_rows)

            elif category == "dataframe":
                content_parts, fmt_meta = _read_dataframe(file_path, ext, fname, max_rows)

            elif category == "excel":
                content_parts, fmt_meta = _read_excel(file_path, fname, max_rows, sheet_name)

            elif category == "word":
                content_parts, fmt_meta = _read_word(file_path, fname)

            elif category == "ppt":
                content_parts, fmt_meta = _read_ppt(file_path, fname)

            elif category == "pdf":
                content_parts, fmt_meta = _read_pdf(file_path, raw, fname, native_doc)

            elif category == "image":
                content_parts, fmt_meta = _read_image(file_path, raw, ext, fname, as_base64)

            elif category in ("text", "code"):
                content_parts, fmt_meta = _read_text(raw, ext, fname)
                if enc_hint:
                    fmt_meta["encoding"] = enc_hint

            else:
                # 未知格式：尝试当文本读取
                enc = enc_hint or _detect_encoding(raw)
                text = _decode(raw, enc)
                content_parts = [_text_part(
                    f"# 文件：{fname}  (未知格式 {ext})\n\n{_truncate(text)}"
                )]
                fmt_meta = {"encoding": enc}

        except Exception as e:
            logger.exception("文件读取工具异常 path=%s", path_str)
            return {
                "result":  f"文件解析时发生异常：{e}",
                "success": False,
                "metadata": base_meta,
            }

        # ── 敏感信息遮盖（对所有文本块统一后处理）────────────────────
        # 图片类型的文本块只含元信息，不含用户数据，跳过遮盖
        if category != "image":
            content_parts, masked_count = _mask_text_parts(content_parts)
            if masked_count:
                logger.info(
                    "文件读取工具遮盖了 %d 处敏感字段 file=%s user=%s",
                    masked_count, fname, user_id,
                )
        else:
            masked_count = 0

        metadata = {**base_meta, **fmt_meta, "masked_secrets": masked_count}
        return {
            "result": {
                "content_parts": content_parts,
                "metadata":      metadata,
            },
            "success":  True,
            "metadata": {"tool": self.name, "parts_count": len(content_parts)},
        }


# ── 自动注册 ──────────────────────────────────────────────────────

def _register() -> None:
    try:
        from app.tools.registry import registry
        tool_instance = FileReaderTool()
        if not registry.get(tool_instance.name):
            registry.register(tool_instance)
            logger.debug("已注册内置工具: %s", tool_instance.name)
    except Exception as e:
        logger.warning("注册 FileReaderTool 失败: %s", e)


_register()
