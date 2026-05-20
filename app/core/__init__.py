"""
【模块说明】核心逻辑包（Core）— 系统最重要的业务引擎

这个包包含了智能管家最核心的业务逻辑：
  hermes_engine.py    — Hermes 主引擎：处理用户消息的总调度器
  vector_store.py     — 向量存储：管理 Elasticsearch 的向量搜索索引
  （embedding_service.py 已迁移至 app/rag/embedding_service.py）
  （chat_history_store.py 已迁移至 app/memory/backends/ 下各后端实现）
  task_planner.py     — 任务规划器：拆解复杂多步骤任务
  exec_collector.py   — 执行记录收集器：追踪本轮工具调用情况
  file_storage.py     — 文件存储配置：用户文件目录管理
  config_loader.py    — 配置加载器：读取 YAML 配置文件
  paths.py            — 路径常量：定义项目根目录
  redis_keys.py       — Redis 键名管理：统一管理所有 Redis 缓存键

核心逻辑模块 - Hermes 引擎、记忆管理、配置加载
"""
