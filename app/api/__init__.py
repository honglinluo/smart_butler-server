"""
【模块说明】API 接口包（API）— 系统对外提供的所有 HTTP 接口

这个包包含了所有面向前端和客户端的 REST API 接口：
  auth.py          — 认证接口：注册、登录、获取公钥、修改密码
  chat.py          — 聊天接口：发送消息（流式/非流式）、上传文件、授权确认
  agents_api.py    — Agent 管理接口：查询、注册、更新、评分 Agent
  tools_api.py     — 工具管理接口：查询、创建、删除工具
  models.py        — 模型管理接口：查看、切换 AI 模型
  scheduler_api.py — 定时任务接口：创建、查看、取消定时任务和通知
  files_api.py     — 文件管理接口：列出、下载、删除用户文件
  skills_api.py    — Skill 管理接口：管理 Agent 技能文件
  decision_api.py  — 决策接口：工具构建授权确认、策略配置
  dependencies.py  — 共用依赖：登录验证、用户信息注入

FastAPI 应用 - API 路由模块
"""
