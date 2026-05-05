"""数据库模块 - 导出所有数据库相关的类和工具"""

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
