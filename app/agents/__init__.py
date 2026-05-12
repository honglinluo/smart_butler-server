"""
【模块说明】智能体包（Agents）— 系统中所有 AI Agent 的集合

这个包包含了系统中所有 AI Agent 的定义和执行机制：
  base.py       — BaseAgent 基类：所有 Agent 共享的能力（LLM 调用、工具绑定、技能加载）
  router.py     — RouterAgent：分析用户意图，决定由哪个专家 Agent 处理
  registry.py   — AgentRegistry：Agent 注册表，记录系统中所有可用的 Agent
  decorators.py — @agent 装饰器：声明式注册一个新 Agent
  loop/         — 事件循环：驱动 Agent 完成需要工具构建的复杂任务
  workers/      — 工作 Agent：通用助手、数据分析师、代码助手等专家团队
  system/       — 系统 Agent：记忆归档等后台维护 Agent

智能体模块 - 定义各类智能体
"""
