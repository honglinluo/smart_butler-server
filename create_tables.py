"""数据库 DDL 执行脚本

扫描 dataset/ 目录下的所有 .sql 文件，提取 CREATE / ALTER / DROP
TABLE|DATABASE|INDEX 语句，依次在配置的 MySQL 数据库中执行。

用法：
    python create_tables.py
"""

import os
import re
import sys
import logging
from pathlib import Path

os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).parent.resolve()))

import yaml
from sqlalchemy import create_engine, text


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _read_log_cfg() -> dict:
    try:
        p = Path(os.environ["PROJECT_ROOT"]) / "config" / "system_config.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("logging", {})
    except Exception:
        pass
    return {}


_log_cfg = _read_log_cfg()
logging.basicConfig(
    level=getattr(logging, _log_cfg.get("level", "INFO").upper(), logging.INFO),
    format=_log_cfg.get(
        "format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ),
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"])
DATASET_DIR = PROJECT_ROOT / "dataset"

# 匹配需要执行的 DDL 语句类型
_DDL_RE = re.compile(
    r"^\s*("
    r"CREATE\s+(?:DATABASE|TABLE|UNIQUE\s+INDEX|INDEX)\b"
    r"|ALTER\s+TABLE\b"
    r"|DROP\s+(?:TABLE|DATABASE|INDEX)\b"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_mysql_url() -> str:
    """从 system_config.yaml 读取 MySQL URL；占位符则退回环境变量 MYSQL_URL。"""
    try:
        p = PROJECT_ROOT / "config" / "system_config.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            url: str = cfg.get("database", {}).get("mysql", {}).get("url", "")
            if url and not url.startswith("${"):
                return url
    except Exception as exc:
        logger.warning("读取配置文件失败: %s", exc)
    return os.environ.get("MYSQL_URL", "")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _split_url(url: str) -> tuple[str, str]:
    """
    将 mysql+pymysql://user:pass@host:port/db_name[?...] 拆分为
    (server_url, db_name)，其中 server_url 不含数据库路径段。
    """
    m = re.match(r"^(mysql[^:]*://[^/]+)/([^?#]+)((?:\?[^#]*)?)$", url)
    if not m:
        return url, ""
    server_url = m.group(1) + "/" + m.group(3)  # scheme+authority + query, no db
    return server_url, m.group(2)


def _ensure_database(server_url: str, db_name: str) -> None:
    """连接 MySQL 服务器（不指定数据库），确保目标数据库存在。"""
    logger.info("确保数据库 `%s` 存在...", db_name)
    engine = create_engine(server_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))
            conn.commit()
        logger.info("数据库 `%s` 就绪", db_name)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# SQL parsing
# ---------------------------------------------------------------------------

def _strip_comments(sql: str) -> str:
    """去除 /* ... */ 块注释和 -- 行注释。"""
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql


def _parse_ddl(content: str) -> list[str]:
    """从 SQL 文件内容中提取所有 DDL 语句。"""
    clean = _strip_comments(content)
    stmts = []
    for raw in clean.split(";"):
        s = raw.strip()
        if s and _DDL_RE.match(s):
            stmts.append(s)
    return stmts


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def _collect_files() -> list[Path]:
    """按路径排序收集 dataset/ 下所有 .sql 文件（递归）。"""
    if not DATASET_DIR.exists():
        logger.error("dataset/ 目录不存在: %s", DATASET_DIR)
        return []
    files = sorted(DATASET_DIR.rglob("*.sql"))
    logger.info("找到 %d 个 SQL 文件", len(files))
    return files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. 获取连接 URL
    url = _load_mysql_url()
    if not url:
        logger.error(
            "未找到 MySQL URL。请设置环境变量 MYSQL_URL，"
            "或在 config/system_config.yaml 的 database.mysql.url 中配置。"
        )
        sys.exit(1)

    # 2. 确保数据库存在
    server_url, db_name = _split_url(url)
    if db_name:
        _ensure_database(server_url, db_name)

    # 3. 收集 SQL 文件
    files = _collect_files()
    if not files:
        logger.error("dataset/ 中没有 .sql 文件，退出")
        sys.exit(1)

    # 4. 解析 DDL 语句
    all_stmts: list[tuple[str, str]] = []  # (文件名, 语句)
    for f in files:
        content = f.read_text(encoding="utf-8")
        stmts = _parse_ddl(content)
        logger.info("  %-35s → %d 条 DDL", f.name, len(stmts))
        all_stmts.extend((f.name, s) for s in stmts)

    if not all_stmts:
        logger.warning("所有文件中未解析到任何 DDL 语句，退出")
        return

    # 5. 执行
    logger.info("共 %d 条 DDL 语句，开始执行...", len(all_stmts))
    engine = create_engine(url, pool_pre_ping=True)
    ok = fail = 0
    try:
        with engine.connect() as conn:
            for filename, stmt in all_stmts:
                preview = stmt[:80].replace("\n", " ")
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                    logger.info("  ✓  [%s] %s", filename, preview)
                    ok += 1
                except Exception as exc:
                    logger.error("  ✗  [%s] %s\n      %s", filename, preview, exc)
                    fail += 1
    finally:
        engine.dispose()

    logger.info("执行完成：成功 %d 条 / 失败 %d 条", ok, fail)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
