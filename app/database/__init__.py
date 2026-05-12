"""
【模块说明】数据库包（Database）— 系统三种数据库的统一访问入口

系统使用三种数据库，各有分工：
  MySQLDatabase        — 关系型数据库，存用户账号、任务记录等结构化数据
  RedisDatabase        — 缓存数据库，存实时对话历史、在线状态、速率限制
  ElasticsearchDatabase — 搜索引擎，存聊天历史全文、向量索引（长期记忆）

外部代码通过这里统一导入，不需要知道各数据库类在哪个文件里：
  pool_manager  — 连接池管理器（整个应用生命周期内复用连接，避免每次请求重新建连）

数据库模块 - 导出所有数据库相关的类和工具
"""

from .base import DatabaseBase
from .mysql import MySQLDatabase
from .redis import RedisDatabase
from .elasticsearch import ElasticsearchDatabase
from .pool import (
    DatabaseConnectionPool,
    ConnectionPoolManager,
    PooledConnection,
    ConnectionStatus,
    pool_manager,
)

__all__ = [
    # 基类
    "DatabaseBase",
    
    # 具体实现
    "MySQLDatabase",
    "RedisDatabase",
    "ElasticsearchDatabase",
    
    # 连接池
    "DatabaseConnectionPool",
    "ConnectionPoolManager",
    "PooledConnection",
    "ConnectionStatus",
    "pool_manager",
]
