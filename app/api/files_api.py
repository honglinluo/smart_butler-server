"""文件管理 API — 列出用户文件、按 file_id 下载。

目录结构（均位于 UPLOAD_ROOT/{user_id}/ 下）：
  uploads/   — 用户通过 /chat/upload 上传的文件
  generated/ — AI 工具（file_writer）生成的文件

端点：
  GET  /files/list        — 列出当前用户所有文件，可按 file_type 过滤
  GET  /files/download/{file_id}  — 按 file_id 下载文件
  DELETE /files/{file_id} — 删除指定文件

file_id 编码规则：URL-safe base64（去填充）of  "{user_id}/{uploads|generated}/..."
（路径相对于 UPLOAD_ROOT）
下载时校验解码后路径必须以 "{current_user_id}/" 开头，防止越权访问。
"""

from __future__ import annotations

import base64
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from app.api.dependencies import get_current_user
from app.core.file_storage import UPLOAD_ROOT, SUBDIR_UPLOADS, SUBDIR_GENERATED
from app.core.headers import ResponseHeaders
from app.core.paths import PROJECT_ROOT

router = APIRouter(prefix="/files", tags=["Files"])


# ══════════════════════════════════════════════════════════════════
# file_id 编解码
# ══════════════════════════════════════════════════════════════════

def _encode_file_id(rel_path: str) -> str:
    """将相对于 UPLOAD_ROOT 的路径编码为 URL-safe base64 file_id（无填充）。"""
    return base64.urlsafe_b64encode(rel_path.encode()).decode().rstrip("=")


def _decode_file_id(file_id: str) -> str:
    """将 file_id 解码回相对路径，失败时抛出 ValueError。"""
    padding = "=" * (-len(file_id) % 4)
    try:
        return base64.urlsafe_b64decode(file_id + padding).decode()
    except Exception as e:
        raise ValueError(f"无效的 file_id: {e}")


def _path_from_file_id(file_id: str, user_id: str) -> Path:
    """
    解码 file_id 并验证归属权，返回绝对路径。

    rel_path 形如 "{user_id}/uploads/images/photo.png"
    若解码后 user_id 段不匹配则拒绝（403）。
    """
    try:
        rel = _decode_file_id(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="file_id 格式无效")

    # 安全校验：路径必须以当前用户 ID 开头
    expected_prefix = user_id + "/"
    if not rel.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="无权访问该文件")

    abs_path = Path(os.path.normpath(UPLOAD_ROOT / rel))

    # 二次校验：绝对路径必须在 UPLOAD_ROOT/{user_id}/ 内
    try:
        abs_path.relative_to(UPLOAD_ROOT / user_id)
    except ValueError:
        raise HTTPException(status_code=403, detail="无权访问该文件")

    return abs_path


# ══════════════════════════════════════════════════════════════════
# 文件信息构造
# ══════════════════════════════════════════════════════════════════

def _file_info(abs_path: Path, user_id: str) -> Dict[str, Any]:
    """构造单个文件的元信息 dict。"""
    rel_to_user  = abs_path.relative_to(UPLOAD_ROOT / user_id)
    rel_to_root  = abs_path.relative_to(UPLOAD_ROOT)
    parts        = rel_to_user.parts

    # 判断文件类型：uploads 或 generated
    file_type = parts[0] if parts else "unknown"

    stat = abs_path.stat()
    mime, _ = mimetypes.guess_type(abs_path.name)

    return {
        "file_id":    _encode_file_id(str(rel_to_root).replace("\\", "/")),
        "filename":   abs_path.name,
        "file_type":  file_type,            # "uploads" | "generated"
        "rel_path":   str(rel_to_user).replace("\\", "/"),
        "size_bytes": stat.st_size,
        "mime_type":  mime or "application/octet-stream",
        "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def _scan_user_files(user_id: str, file_type: Optional[str]) -> List[Dict[str, Any]]:
    """扫描用户目录下的所有文件，按需过滤 file_type。"""
    user_root = UPLOAD_ROOT / user_id
    if not user_root.exists():
        return []

    subdirs_to_scan = []
    for name in (SUBDIR_UPLOADS, SUBDIR_GENERATED):
        if file_type and name != file_type:
            continue
        d = user_root / name
        if d.exists():
            subdirs_to_scan.append(d)

    results: List[Dict[str, Any]] = []
    for subdir in subdirs_to_scan:
        for p in sorted(subdir.rglob("*")):
            if p.is_file():
                try:
                    results.append(_file_info(p, user_id))
                except Exception:
                    pass  # 跳过无法访问的文件

    # 按修改时间降序排列（最新在前）
    results.sort(key=lambda x: x["modified_at"], reverse=True)
    return results


# ══════════════════════════════════════════════════════════════════
# 端点
# ══════════════════════════════════════════════════════════════════

@router.get("/list", summary="列出用户所有文件")
async def list_files(
    response: Response,
    file_type: Optional[str] = Query(
        None,
        description="过滤文件类型：uploads（上传文件）或 generated（生成文件），不填则返回全部",
        pattern="^(uploads|generated)$",
    ),
    current_user: dict = Depends(get_current_user),
):
    """
    返回当前用户的所有文件列表，包含 file_id、文件名、大小、MIME 类型等元信息。

    - **file_type=uploads**: 仅返回用户上传的文件
    - **file_type=generated**: 仅返回 AI 生成的文件
    - 不传 file_type：返回全部
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]
    files   = _scan_user_files(user_id, file_type)
    return {
        "user_id":    user_id,
        "file_type":  file_type or "all",
        "total":      len(files),
        "files":      files,
    }


@router.get("/download/{file_id}", summary="下载文件")
async def download_file(
    file_id: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """
    按 file_id 下载文件。file_id 由 `/files/list` 接口返回。

    - 只能下载当前登录用户自己的文件（跨用户访问返回 403）
    - 文件不存在返回 404
    """
    ResponseHeaders().apply(response)
    user_id  = current_user["user_id"]
    abs_path = _path_from_file_id(file_id, user_id)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在或已被删除")
    if not abs_path.is_file():
        raise HTTPException(status_code=400, detail="指定路径不是文件")

    mime, _ = mimetypes.guess_type(abs_path.name)
    return FileResponse(
        path         = str(abs_path),
        filename     = abs_path.name,
        media_type   = mime or "application/octet-stream",
    )


@router.delete("/{file_id}", summary="删除文件")
async def delete_file(
    file_id: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """
    按 file_id 删除文件。只能删除当前用户自己的文件。
    """
    ResponseHeaders().apply(response)
    user_id  = current_user["user_id"]
    abs_path = _path_from_file_id(file_id, user_id)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在或已被删除")
    if not abs_path.is_file():
        raise HTTPException(status_code=400, detail="指定路径不是文件")

    try:
        abs_path.unlink()
        # 删除后若目录为空则一并清理（最多两层）
        for parent in (abs_path.parent, abs_path.parent.parent):
            try:
                if parent != UPLOAD_ROOT / user_id and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                break
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")

    return {
        "success":  True,
        "file_id":  file_id,
        "filename": abs_path.name,
        "message":  "文件已删除",
    }
