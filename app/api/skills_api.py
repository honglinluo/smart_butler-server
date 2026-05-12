"""
【模块说明】Agent Skill 管理 API — 让管理员查看和管理 AI 的技能文件

Skill 文件是 Agent 的"经验文档"，管理员可以通过这些接口对 Skill 进行管理。

【可用接口】
  GET    /skills                              — 查看所有有 Skill 的 Agent 列表
  GET    /skills/{agent}                      — 查看某个 Agent 的所有 Skill（含元数据）
  GET    /skills/{agent}/{skill}              — 读取某个 Skill 的完整内容
  GET    /skills/{agent}/{skill}/backups      — 查看该 Skill 的备份历史
  POST   /skills/{agent}/{skill}              — 手动新建一个 Skill
  PUT    /skills/{agent}/{skill}              — 更新 Skill 内容（自动备份旧版本）
  DELETE /skills/{agent}/{skill}              — 删除 Skill
  POST   /skills/{agent}/{skill}/rollback     — 回滚到上一个备份版本
  POST   /skills/{agent}/evolve               — 手动触发该 Agent 的 Skill 自动演进

Agent Skill 管理 API

接口列表：
  GET    /skills                          查看所有有 skill 的 agent
  GET    /skills/{agent}                  查看指定 agent 的所有 skill（元数据）
  GET    /skills/{agent}/{skill}          读取 skill 完整内容
  GET    /skills/{agent}/{skill}/backups  查看备份列表
  POST   /skills/{agent}/{skill}          新建 skill
  PUT    /skills/{agent}/{skill}          更新 skill（自动备份轮转）
  DELETE /skills/{agent}/{skill}          删除 skill
  POST   /skills/{agent}/{skill}/rollback 回滚到上一个备份
  POST   /skills/{agent}/evolve           手动触发该 agent 的 skill 演进
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user
from app.skills.manager import skill_manager, _validate_skill_content, MAX_SKILLS_PER_AGENT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["Agent Skills"])


# ─────────────────────────────────────────────────────────────────────────────
# 请求 / 响应模型
# ─────────────────────────────────────────────────────────────────────────────

class SkillMeta(BaseModel):
    agent_name:  str
    skill_name:  str
    name:        str = ""
    description: str = ""
    version:     str = ""
    created_at:  str = ""
    last_updated: str = ""
    size:        int = 0
    modified_at: str = ""
    backups:     List[Dict[str, Any]] = []


class SkillDetail(SkillMeta):
    content: str = ""


class SkillWriteRequest(BaseModel):
    content: str = Field(..., description="Markdown 格式的 skill 内容，必须含 YAML frontmatter")


class EvolveRequest(BaseModel):
    force: bool = Field(False, description="忽略调用次数阈值，强制演进")


# ─────────────────────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────────────────────

def _meta_from_skill(agent_name: str, skill_name: str) -> SkillMeta:
    raw = skill_manager.get_skill_meta(agent_name, skill_name)
    return SkillMeta(
        agent_name=agent_name,
        skill_name=skill_name,
        name=raw.get("name", skill_name),
        description=raw.get("description", ""),
        version=str(raw.get("version", "")),
        created_at=str(raw.get("created_at", "")),
        last_updated=str(raw.get("last_updated", "")),
        size=raw.get("size", 0),
        modified_at=raw.get("modified_at", ""),
        backups=raw.get("backups", []),
    )


async def _get_engine(request: Request):
    engine = getattr(getattr(request, "app", None), "state", None)
    return getattr(engine, "hermes_engine", None) if engine else None


# ─────────────────────────────────────────────────────────────────────────────
# GET  /skills
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", summary="列出所有有 skill 文件的 agent")
async def list_agents(
    _user: dict = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """返回所有在 skills/ 目录下有 skill 的 agent 列表，含各 agent skill 数量。"""
    agents = skill_manager.list_all_agents()
    result = []
    for ag in agents:
        skills = skill_manager.list_skills(ag)
        result.append({"agent_name": ag, "skill_count": len(skills), "skills": skills})
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET  /skills/{agent}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{agent_name}", summary="列出 agent 的所有 skill 元数据")
async def list_agent_skills(
    agent_name: str,
    _user: dict = Depends(get_current_user),
) -> List[SkillMeta]:
    skills = skill_manager.list_skills(agent_name)
    if not skills:
        return []
    return [_meta_from_skill(agent_name, s) for s in skills]


# ─────────────────────────────────────────────────────────────────────────────
# GET  /skills/{agent}/{skill}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{agent_name}/{skill_name}", summary="读取 skill 完整内容")
async def get_skill(
    agent_name: str,
    skill_name: str,
    include_url_content: bool = Query(False, description="是否等待并内联 URL 引用内容"),
    _user: dict = Depends(get_current_user),
) -> SkillDetail:
    content = skill_manager.read_skill(agent_name, skill_name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"skill '{skill_name}' 不存在")

    if include_url_content:
        from app.skills.loader import load_skills_text
        # 只加载目标 skill，临时只让该 skill 存在于 context
        # 直接组装文本
        from app.skills.loader import _URL_RE, _fetch_and_cache, _get_cached, _FETCH_TIMEOUT
        import asyncio
        urls = list(dict.fromkeys(_URL_RE.findall(content)))
        if urls:
            # 同步等待所有 URL（API 调用可以等）
            async def fetch_all(us):
                tasks = [asyncio.ensure_future(_fetch_and_cache(u)) for u in us[:3]]
                await asyncio.gather(*tasks, return_exceptions=True)
            await fetch_all(urls)
            sections = []
            for u in urls[:3]:
                c = _get_cached(u)
                if c:
                    sections.append(f"**{u}**\n\n{c}")
            if sections:
                content += "\n\n### 链接参考内容\n\n" + "\n\n---\n\n".join(sections)

    meta = _meta_from_skill(agent_name, skill_name)
    return SkillDetail(**meta.model_dump(), content=content)


# ─────────────────────────────────────────────────────────────────────────────
# GET  /skills/{agent}/{skill}/backups
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{agent_name}/{skill_name}/backups", summary="查看备份列表")
async def get_backups(
    agent_name: str,
    skill_name: str,
    include_content: bool = Query(False, description="是否同时返回备份内容"),
    _user: dict = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    backups = skill_manager.list_backups(agent_name, skill_name)
    if include_content:
        for bk in backups:
            bk["content"] = skill_manager.read_backup(agent_name, skill_name, bk["idx"]) or ""
    return backups


# ─────────────────────────────────────────────────────────────────────────────
# POST /skills/{agent}/{skill}  — 新建
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{agent_name}/{skill_name}", status_code=status.HTTP_201_CREATED,
             summary="新建 skill（不备份旧内容）")
async def create_skill(
    agent_name: str,
    skill_name: str,
    body: SkillWriteRequest,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    existing = skill_manager.list_skills(agent_name)
    if len(existing) >= MAX_SKILLS_PER_AGENT and skill_name not in existing:
        raise HTTPException(
            status_code=409,
            detail=f"agent '{agent_name}' 已有 {len(existing)} 个 skill（上限 {MAX_SKILLS_PER_AGENT}）",
        )

    if skill_manager.read_skill(agent_name, skill_name) is not None:
        raise HTTPException(status_code=409, detail=f"skill '{skill_name}' 已存在，请用 PUT 更新")

    result = skill_manager.write_skill(agent_name, skill_name, body.content, backup=False)
    if not result["success"]:
        raise HTTPException(status_code=422, detail=result["error"])
    return {"agent_name": agent_name, "skill_name": skill_name, "path": result["path"]}


# ─────────────────────────────────────────────────────────────────────────────
# PUT /skills/{agent}/{skill}  — 更新（备份）
# ─────────────────────────────────────────────────────────────────────────────

@router.put("/{agent_name}/{skill_name}", summary="更新 skill 内容（自动备份轮转）")
async def update_skill(
    agent_name: str,
    skill_name: str,
    body: SkillWriteRequest,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    result = skill_manager.write_skill(agent_name, skill_name, body.content, backup=True)
    if not result["success"]:
        raise HTTPException(status_code=422, detail=result["error"])
    return {"agent_name": agent_name, "skill_name": skill_name, "path": result["path"]}


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /skills/{agent}/{skill}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{agent_name}/{skill_name}", status_code=status.HTTP_204_NO_CONTENT,
               summary="删除 skill（保留备份）")
async def delete_skill(
    agent_name: str,
    skill_name: str,
    delete_backups: bool = Query(False, description="同时删除所有备份"),
    _user: dict = Depends(get_current_user),
):
    deleted = skill_manager.delete_skill(agent_name, skill_name, delete_backups=delete_backups)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"skill '{skill_name}' 不存在")


# ─────────────────────────────────────────────────────────────────────────────
# POST /skills/{agent}/{skill}/rollback
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{agent_name}/{skill_name}/rollback", summary="回滚到最新备份（bak1）")
async def rollback_skill(
    agent_name: str,
    skill_name: str,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    if skill_manager.read_skill(agent_name, skill_name) is None:
        raise HTTPException(status_code=404, detail=f"skill '{skill_name}' 不存在")
    result = skill_manager.rollback_skill(agent_name, skill_name)
    if not result["success"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return {
        "agent_name": agent_name,
        "skill_name": skill_name,
        "message": "回滚成功",
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /skills/{agent}/evolve  — 手动触发演进
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{agent_name}/evolve", summary="手动触发 agent skill 演进")
async def evolve_skill(
    agent_name: str,
    body: EvolveRequest,
    request: Request,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    engine = await _get_engine(request)
    if engine is None:
        raise HTTPException(status_code=503, detail="HermesEngine 未就绪")

    llm = getattr(getattr(engine, "memory_manager", None), "_default_llm", None)
    if llm is None:
        # 降级：用默认用户 LLM
        try:
            from app.core.hermes_engine import LLMInfo
            llm_info = await LLMInfo.load("0")
            if llm_info:
                llm = await llm_info.build_chat_model()
        except Exception:
            pass

    if llm is None:
        raise HTTPException(status_code=503, detail="LLM 未配置，无法执行演进")

    from app.skills.evolver import skill_evolver
    result = await skill_evolver.evolve_agent(agent_name, llm, force=body.force)
    if not result["success"] and result["action"] != "skip":
        raise HTTPException(status_code=500, detail=result["message"])
    return result
