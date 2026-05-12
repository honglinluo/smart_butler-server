"""
【模块说明】Skill 文件管理器（SkillFileManager）— 管理 Agent 技能文件的读写和版本

Skill 文件是 Markdown 格式的文本文件，存储在服务器的 skills/ 目录下。
这个模块负责：
  - 读取某个 Agent 的所有 Skill 文件内容
  - 写入新的 Skill（写入前自动备份旧版本）
  - 在出错时回滚到上一个备份版本

【备份策略（最多保留 2 份备份）】
  每次更新 Skill 时：旧的 bak1 变成 bak2，当前版本变成 bak1，然后写入新内容
  回滚时：当前版本变成 bak2，bak1 恢复为当前版本

Skill 文件管理器 — 读写、备份轮转、回滚

备份策略（最多 2 份）：
  写入时：bak2 ← bak1，bak1 ← 当前，再写新内容
  回滚时：当前 → bak2，bak1 → 当前
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

SKILLS_ROOT: Path = PROJECT_ROOT / "skills"
MAX_SKILLS_PER_AGENT = 3
_MAX_BACKUPS = 2


# ─────────────────────────────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> Dict[str, Any]:
    """解析 YAML frontmatter（--- ... ---）。解析失败时返回空字典。"""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(content[3:end]) or {}
    except Exception:
        return {}


def _validate_skill_content(content: str) -> Optional[str]:
    """校验 skill 文件结构。返回错误描述，None 表示校验通过。"""
    if not content or not content.strip():
        return "skill 内容为空"
    fm = _parse_frontmatter(content)
    if not fm:
        return "缺少 YAML frontmatter（--- ... ---）"
    if not fm.get("name"):
        return "frontmatter 缺少 name 字段"
    if not fm.get("description"):
        return "frontmatter 缺少 description 字段"
    return None


# ─────────────────────────────────────────────────────────────────────────────

class SkillFileManager:
    """Agent skill 文件管理器，负责文件 I/O 和备份轮转。"""

    # ── 路径辅助 ─────────────────────────────────────────────────────────────

    def get_skill_dir(self, agent_name: str) -> Path:
        return SKILLS_ROOT / agent_name

    def skill_path(self, agent_name: str, skill_name: str) -> Path:
        return self.get_skill_dir(agent_name) / f"{skill_name}.md"

    def backup_path(self, agent_name: str, skill_name: str, idx: int) -> Path:
        return self.get_skill_dir(agent_name) / f"{skill_name}.bak{idx}.md"

    # ── 读取 ─────────────────────────────────────────────────────────────────

    def list_skills(self, agent_name: str) -> List[str]:
        """返回 agent 所有 skill 名（无扩展名），按文件名排序，最多 MAX_SKILLS_PER_AGENT 个。"""
        d = self.get_skill_dir(agent_name)
        if not d.exists():
            return []
        skills = sorted(
            f.stem for f in d.iterdir()
            if f.suffix == ".md" and ".bak" not in f.name
        )
        return skills[:MAX_SKILLS_PER_AGENT]

    def list_all_agents(self) -> List[str]:
        """返回所有有 skill 的 agent 名称列表。"""
        if not SKILLS_ROOT.exists():
            return []
        return sorted(d.name for d in SKILLS_ROOT.iterdir() if d.is_dir())

    def read_skill(self, agent_name: str, skill_name: str) -> Optional[str]:
        """读取 skill 文件内容，不存在时返回 None。"""
        p = self.skill_path(agent_name, skill_name)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def read_backup(self, agent_name: str, skill_name: str, idx: int = 1) -> Optional[str]:
        """读取备份内容（idx=1 最新，idx=2 次新）。"""
        p = self.backup_path(agent_name, skill_name, idx)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def list_backups(self, agent_name: str, skill_name: str) -> List[Dict[str, Any]]:
        """返回备份信息列表（按备份序号升序）。"""
        result = []
        for idx in range(1, _MAX_BACKUPS + 1):
            p = self.backup_path(agent_name, skill_name, idx)
            if p.exists():
                stat = p.stat()
                content = p.read_text(encoding="utf-8")
                fm = _parse_frontmatter(content)
                result.append({
                    "idx": idx,
                    "filename": p.name,
                    "version": fm.get("version", ""),
                    "last_updated": fm.get("last_updated", ""),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        return result

    def get_skill_meta(self, agent_name: str, skill_name: str) -> Dict[str, Any]:
        """返回 skill 的 frontmatter 元数据 + 文件统计。"""
        p = self.skill_path(agent_name, skill_name)
        if not p.exists():
            return {}
        content = p.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        stat = p.stat()
        return {
            **fm,
            "skill_name": skill_name,
            "agent_name": agent_name,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "backups": self.list_backups(agent_name, skill_name),
        }

    # ── 写入 ─────────────────────────────────────────────────────────────────

    def write_skill(
        self,
        agent_name: str,
        skill_name: str,
        content: str,
        backup: bool = True,
        validate: bool = True,
    ) -> Dict[str, Any]:
        """写入 skill 文件（写前备份轮转）。

        Returns:
            {"success": bool, "path": str, "error": str}
        """
        if validate:
            err = _validate_skill_content(content)
            if err:
                return {"success": False, "path": "", "error": f"skill 格式校验失败: {err}"}

        d = self.get_skill_dir(agent_name)
        d.mkdir(parents=True, exist_ok=True)
        p = self.skill_path(agent_name, skill_name)

        if backup and p.exists():
            self._rotate_backups(d, skill_name)

        p.write_text(content, encoding="utf-8")
        logger.info("[SkillMgr] 写入 agent=%s skill=%s", agent_name, skill_name)
        return {"success": True, "path": str(p), "error": ""}

    # ── 回滚 ─────────────────────────────────────────────────────────────────

    def rollback_skill(self, agent_name: str, skill_name: str) -> Dict[str, Any]:
        """从 bak1 还原 skill 文件。

        回滚流程：current → bak2（丢弃旧 bak2），bak1 → current。
        Returns:
            {"success": bool, "error": str}
        """
        d = self.get_skill_dir(agent_name)
        current = self.skill_path(agent_name, skill_name)
        bak1 = self.backup_path(agent_name, skill_name, 1)
        bak2 = self.backup_path(agent_name, skill_name, 2)

        if not bak1.exists():
            msg = f"无备份可回滚 agent={agent_name} skill={skill_name}"
            logger.warning("[SkillMgr] %s", msg)
            return {"success": False, "error": msg}

        try:
            if bak2.exists():
                bak2.unlink()
            if current.exists():
                current.rename(bak2)
            bak1.rename(current)
            logger.info("[SkillMgr] 回滚成功 agent=%s skill=%s", agent_name, skill_name)
            return {"success": True, "error": ""}
        except Exception as e:
            logger.error("[SkillMgr] 回滚失败 agent=%s skill=%s: %s", agent_name, skill_name, e)
            return {"success": False, "error": str(e)}

    # ── 删除 ─────────────────────────────────────────────────────────────────

    def delete_skill(
        self, agent_name: str, skill_name: str, delete_backups: bool = False
    ) -> bool:
        """删除 skill 文件，可选同时删除备份。"""
        p = self.skill_path(agent_name, skill_name)
        deleted = False
        if p.exists():
            p.unlink()
            deleted = True
        if delete_backups:
            for idx in range(1, _MAX_BACKUPS + 1):
                bp = self.backup_path(agent_name, skill_name, idx)
                if bp.exists():
                    bp.unlink()
        if deleted:
            logger.info("[SkillMgr] 删除 skill agent=%s skill=%s", agent_name, skill_name)
        return deleted

    # ── 备份轮转（内部）──────────────────────────────────────────────────────

    def _rotate_backups(self, skill_dir: Path, skill_name: str) -> None:
        """bak2 ← bak1，bak1 ← current（copy，不移动 current，write 后覆盖）。"""
        current = skill_dir / f"{skill_name}.md"
        bak1 = skill_dir / f"{skill_name}.bak1.md"
        bak2 = skill_dir / f"{skill_name}.bak2.md"

        if bak2.exists():
            bak2.unlink()
        if bak1.exists():
            bak1.rename(bak2)
        if current.exists():
            shutil.copy2(current, bak1)


skill_manager = SkillFileManager()
