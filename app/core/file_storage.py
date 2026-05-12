"""
【模块说明】文件存储配置（FileStorage）— 定义用户文件的存放位置和清理规则

用户上传的文件和 AI 生成的文件都存放在服务器上，这个模块统一管理存放规则：
  - UPLOAD_ROOT：所有用户文件的根目录（路径来自配置文件）
  - 每位用户有独立的子目录（按 user_id 隔离）
  - 上传文件和 AI 生成文件分开存放（uploads/ vs generated/）
  - CLEANUP_DAYS：文件保留天数（-1 表示永不自动清理）

文件存储配置 — 统一的上传/生成文件根目录，供各模块共享。

目录结构（UPLOAD_ROOT 为根）：
  {UPLOAD_ROOT}/
  └── {user_id}/
      ├── uploads/    ← 用户通过 /chat/upload 上传的文件
      │   ├── code/
      │   ├── images/
      │   ├── files/
      │   └── text/
      └── generated/  ← AI 工具生成并保存的文件（file_writer 工具）
          └── （任意子目录/文件名）

配置来源：config/system_config.yaml → file_storage 节
"""

from pathlib import Path

from app.core.paths import PROJECT_ROOT


def _read_config():
    try:
        import yaml
        with open(PROJECT_ROOT / "config" / "system_config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        s = cfg.get("file_storage", {})
        root      = (PROJECT_ROOT / s.get("upload_dir", "data/uploads")).resolve()
        max_bytes = int(s.get("max_file_size_mb", 50)) * 1024 * 1024
        cleanup   = int(s.get("cleanup_days", 30))
        return root, max_bytes, cleanup
    except Exception:
        return (PROJECT_ROOT / "data" / "uploads").resolve(), 50 * 1024 * 1024, 30


UPLOAD_ROOT:   Path = None   # type: ignore[assignment]
MAX_FILE_SIZE: int  = 0
CLEANUP_DAYS:  int  = 30

UPLOAD_ROOT, MAX_FILE_SIZE, CLEANUP_DAYS = _read_config()

# ── 子目录常量 ─────────────────────────────────────────────────────────────────
SUBDIR_UPLOADS   = "uploads"    # 用户上传目录名
SUBDIR_GENERATED = "generated"  # AI 生成目录名
