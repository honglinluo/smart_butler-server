"""
【模块说明】工具函数包（Utils）— 各模块共用的通用工具函数

这个包提供了系统各处都会用到的通用工具：
  progress_bus.py   — 进度事件总线：流式对话中实时推送进度事件给前端
  log_bus.py        — 日志总线：AI 处理流程的结构化彩色日志
  log_setup.py      — 日志文件配置：三级日志文件（系统/对话/调度器）
  crypto.py         — 加密工具：RSA 密钥管理、密码加密解密
  headers.py        — HTTP 请求/响应头管理
  client_env.py     — 客户端类型定义（Web/移动/桌面/微信等）
  content_fetcher.py — 内容获取器：自动从输入中识别文件路径和 URL 并获取内容

工具函数模块 - ES 客户端、Redis 客户端等
"""
