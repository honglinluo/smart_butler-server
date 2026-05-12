"""
【模块说明】Skill 加载器（Loader）— 在 Agent 回答前把"经验包"装入提示词

每次 Agent 被调用时，这个模块负责：
  1. 读取该 Agent 的 Skill 文件
  2. 如果 Skill 文件中引用了外部 URL（如参考文档、API文档链接），自动抓取内容
  3. 把 Skill 内容格式化后注入到 Agent 的提示词中

【URL 抓取策略（避免等待）】
  - 如果这个 URL 之前抓取过（缓存 1 小时有效）：直接用缓存，不等待
  - 如果缓存未命中：本次先跳过这个 URL，后台异步预热缓存，下次调用时才带入内容
  这样做是为了不让网络请求拖慢 Agent 的响应速度

Skill 加载器 — 读取 skill 文件、解析 URL 引用、构建注入提示词块

URL 解析策略（避免首次调用阻塞）：
  - 命中内存缓存（TTL 1h）：直接使用
  - 未命中：不等待，触发后台协程异步预热缓存；下次调用才带入内容
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from app.skills.manager import skill_manager

logger = logging.getLogger(__name__)

# ── URL 解析配置 ──────────────────────────────────────────────────────────────
_URL_RE = re.compile(r'https?://[^\s\)\]"\'><,；，。]+')
_MAX_URLS_PER_SKILL = 3
_FETCH_TIMEOUT = 5.0         # 单 URL 抓取超时（秒）
_MAX_URL_CHARS = 2000        # 每个 URL 最多截取字符数
_CACHE_TTL = 3600            # URL 内容缓存有效期（秒）

# 进程级 URL 内容缓存：url → (content, fetched_at)
_url_cache: Dict[str, Tuple[str, float]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# URL 抓取
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_url(url: str) -> str:
    """抓取 URL 内容，截断到 _MAX_URL_CHARS。失败时返回空字符串。"""
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HermesBot/1.0)"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            text = resp.text
            # 简单去除 HTML 标签
            text = re.sub(r'<[^>]{1,200}>', '', text)
            text = re.sub(r'\s{3,}', '\n\n', text).strip()
            return text[:_MAX_URL_CHARS]
    except Exception as e:
        logger.debug("[SkillLoader] URL 抓取失败 url=%s: %s", url, e)
        return ""


async def _fetch_and_cache(url: str) -> None:
    """后台任务：抓取 URL 并写入缓存。"""
    content = await _fetch_url(url)
    if content:
        _url_cache[url] = (content, time.monotonic())
        logger.debug("[SkillLoader] URL 缓存更新 url=%s len=%d", url, len(content))


def _get_cached(url: str) -> Optional[str]:
    """从缓存读取 URL 内容，过期则返回 None。"""
    entry = _url_cache.get(url)
    if entry and (time.monotonic() - entry[1]) < _CACHE_TTL:
        return entry[0]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 核心：加载 agent 的所有 skill 并构建提示词块
# ─────────────────────────────────────────────────────────────────────────────

async def load_skills_text(agent_name: str) -> str:
    """加载 agent 所有 skill 文件，返回格式化的 Markdown 块供注入 system prompt。

    - 最多加载 3 个 skill（建议只有 1 个）
    - skill 内容中的 URL 优先从缓存读取；未缓存时不等待，触发后台预热
    - 首次加载返回不含 URL 内容的版本（不阻塞 agent 执行），后续调用会含 URL 内容

    Returns:
        格式化的技能块字符串；无 skill 时返回空字符串
    """
    skill_names = skill_manager.list_skills(agent_name)
    if not skill_names:
        return ""

    blocks: List[str] = []
    for skill_name in skill_names:
        content = skill_manager.read_skill(agent_name, skill_name)
        if not content:
            continue

        # 解析 skill 中的 URL
        urls = list(dict.fromkeys(_URL_RE.findall(content)))
        if urls:
            url_sections: List[str] = []
            bg_tasks: List[str] = []

            for url in urls[:_MAX_URLS_PER_SKILL]:
                cached = _get_cached(url)
                if cached:
                    url_sections.append(f"**{url}**\n\n{cached}")
                else:
                    # 未命中缓存：触发后台预热，本次跳过
                    bg_tasks.append(url)

            if bg_tasks:
                for u in bg_tasks:
                    asyncio.create_task(_fetch_and_cache(u))

            if url_sections:
                content += "\n\n### 链接参考内容\n\n" + "\n\n---\n\n".join(url_sections)

        blocks.append(content.strip())

    if not blocks:
        return ""

    joined = "\n\n---\n\n".join(blocks)
    return f"<skill-knowledge>\n{joined}\n</skill-knowledge>"
