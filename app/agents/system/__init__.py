"""
【模块说明】系统 Agent 包 — 负责系统内部维护的幕后智能体

这些 Agent 不对用户开放，只由系统在后台自动调用，用于维护记忆系统的健康运转：

  memory_archiver.py  — 记忆压缩 Agent：把过多的对话轮次压缩成摘要
  monthly_archiver.py — 月度归档 Agent：把 1 年前的历史按月汇总
  yearly_archiver.py  — 年度归档 Agent：把 3 年前的月度摘要按年汇总

这三个 Agent 不注册到 AgentRegistry（用户无法直接调用），
只通过 MemoryManager 和定时任务调度器在特定时机触发。
"""