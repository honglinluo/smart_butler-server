"""
【模块说明】智能管家应用包（App）— 系统的整体入口

这个包包含了智能管家服务端的所有代码，按功能划分为以下子模块：

  api/       — HTTP 接口层：所有对外暴露的 REST API 接口
  core/      — 核心引擎层：Hermes 引擎、记忆管理、RAG 检索、任务规划
  agents/    — AI 智能体层：各类专业 Agent、路由器、事件循环
  tools/     — 工具层：AI 可调用的功能工具（内置+动态+用户自定义）
  database/  — 数据库层：MySQL / Redis / Elasticsearch 连接和操作
  scheduler/ — 定时任务层：定时任务调度、存储、通知
  sandbox/   — 沙箱层：安全执行代码、文件上传处理
  rag/       — 检索增强层：向量检索、混合检索、对话索引
  skills/    — 技能层：Agent 技能文件管理和自演进
  utils/     — 工具函数层：日志、加密、请求头、内容获取等通用工具

智能管家 - App Package
"""
