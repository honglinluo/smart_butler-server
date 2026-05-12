"""
【模块说明】Agent Skill（技能）系统 — 让 AI Agent 拥有可成长的专属能力

普通 AI Agent 每次对话都从零开始。Skill 系统让 Agent 拥有"记录在文件里的经验"——
把它擅长的任务模式、常用的解决思路、特定领域的知识，以 Markdown 文件的形式存储起来，
每次 Agent 被调用时自动加载这些"经验包"，提升回答质量和任务成功率。

【文件存储结构】
  {项目根目录}/skills/{agent名称}/{技能名}.md       ← 当前有效技能
  {项目根目录}/skills/{agent名称}/{技能名}.bak1.md  ← 最新备份
  {项目根目录}/skills/{agent名称}/{技能名}.bak2.md  ← 次新备份

【Skill 的生命周期】
  1. 生成（generate）：当 Agent 积累了足够多的成功案例，AI 自动从中提炼经验生成 Skill 文件
  2. 使用（load）：Agent 每次被调用时，自动加载对应的 Skill 文件内容注入提示词
  3. 优化（optimize）：随着更多使用数据积累，AI 自动优化现有 Skill 内容
  4. 备份/回滚（backup/rollback）：每次修改前自动备份，出错时回滚到上一版本

【这个包包含什么】
  manager.py  — Skill 文件读写、备份轮转、回滚管理
  loader.py   — 加载 Skill 并注入到 Agent 提示词（支持 URL 引用自动抓取）
  evolver.py  — 自演进引擎：自动从使用数据中生成/优化 Skill

Agent 文件型 Skill 系统

目录结构：
  {PROJECT_ROOT}/skills/{agent_name}/{skill_name}.md       ← 当前 skill
  {PROJECT_ROOT}/skills/{agent_name}/{skill_name}.bak1.md  ← 最新备份
  {PROJECT_ROOT}/skills/{agent_name}/{skill_name}.bak2.md  ← 次新备份

每个 agent 最多 3 个 skill（建议只保留 1 个）。
"""
from app.skills.manager import SkillFileManager, skill_manager
from app.skills.loader import load_skills_text
from app.skills.evolver import SkillEvolver, skill_evolver

__all__ = [
    "SkillFileManager", "skill_manager",
    "load_skills_text",
    "SkillEvolver", "skill_evolver",
]
